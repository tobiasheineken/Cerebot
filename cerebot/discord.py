"""Creating and managing the Discord connection."""

import asyncio
if hasattr(asyncio, "async"):
    ensure_future = asyncio.async
else:
    ensure_future = asyncio.ensure_future

from beem.chat import ChatWatcher, bot_help_command
import discord
import logging
import os
import re
import signal
import time

from .version import version as Version

_log = logging.getLogger()
_url_regexp = (r'(https?://(?:\S+(?::\S*)?@)?(?:(?:[1-9]\d?|1\d\d|2[01]\d|22'
               r'[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?'
               r'|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]+-?)*'
               r'[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]+-?)*'
               r'[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,})))'
               r'(?::\d{2,5})?(?:/[^\s]*)?)')

class DiscordChannel(ChatWatcher):
    def __init__(self, manager, channel, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.manager = manager
        self.channel = channel
        self.bot_source_desc = "Private Message"
        self.admin_target_prefix = "^"

    # Set to the bot only if we're in PM, otherwise None.
    @property
    def user(self):
        if self.channel.is_private:
            return self.login_user
        else:
            return None

    @property
    def login_user(self):
        return self.manager.user

    def describe(self):
        channel_name = None
        if self.channel.is_private or not self.channel.name:
            channel_name = 'PM:{}'.format(self.channel.id)
        else:
            channel_name = '{}:#{}'.format(self.channel.server.name,
                    self.channel.name)
        return channel_name

    def get_chat_name(self, user, sanitize=False):
        return super().get_chat_name(user.name, sanitize)

    def get_dcss_nick(self, user):
        return self.get_chat_name(user, True)

    def get_vanity_roles(self):
        if self.channel.is_private:
            return

        server = self.channel.server
        bot_role = None
        for r in server.roles:
            if r.name == "Bot" and r in server.me.roles:
                bot_role = r
                break
        if not bot_role:
            return

        roles = []
        for r in server.roles:
            # Only give roles with default permissions.
            if (r.position < bot_role.position
                and not r.is_everyone
                and r.permissions == server.default_role.permissions):
                roles.append(r)

        return roles

    def bot_command_allowed(self, user, command):
        entry = self.manager.bot_commands[command]
        if (entry["source_restriction"] == "channel"
            and self.channel.is_private):
            return (False, "This command must be run in a channel.")

        return super().bot_command_allowed(user, command)

    def handle_timeout(self):
        if self.manager.handle_timeout():
            _log.info("%s: Command ignored due to command limit (channel: %s, "
                      "requester: %s): %s", self.manager.service,
                      self.describe(), sender, message)
            return True

        return False

    def get_source_ident(self):
        """Get a unique identifier hash of the discord channel."""

        # Channels are uniquely identified by ID.
        return {"service" : self.manager.service, "id" : self.channel.id}

    @asyncio.coroutine
    def send_chat(self, message, message_type="normal"):
        # Clean up any markdown we don't want.
        if message_type == "monster":
            message = message.replace('```', r'\`\`\`')
        else:
            parts = re.split(_url_regexp, message)
            message = ""
            for i, p in enumerate(parts):
                # URLs parts will always be at an odd index. These are
                # unmodified. Remove markdown characters from non-urls parts.
                if not i % 2:
                    for c in "*_~":
                        p = p.replace(c, "\\" + c)
                message += p

        if message_type == "action":
            message = '_' + message + '_'
        elif message_type == "monster":
            message = '```\n' + message + '\n```'
        elif self.message_needs_escape(message):
            message = "]" + message
        yield from self.manager.send_message(self.channel, message)


class DiscordManager(discord.Client):
    def __init__(self, conf, dcss_manager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ping_task = None
        self.shutdown = False
        self.service = "Discord"
        self.conf = conf
        self.single_user = False
        self.dcss_manager = dcss_manager
        dcss_manager.managers["Discord"] = self
        self.bot_commands = bot_commands
        self.message_times = []

    def log_exception(self, e, error_msg):
        error_reason = type(e).__name__
        if e.args:
            error_reason = "{}: {}".format(error_reason, e.args[0])
        _log.error("Discord Error: %s: %s", error_msg, error_reason)

    @asyncio.coroutine
    def start_ping(self):
        while True:
            if self.is_closed:
                return

            try:
                yield from self.ws.ping()

            except asyncio.CancelledError:
                return

            except Exception as e:
                self.log_exception(e, "Unable to send ping")
                ensure_future(self.disconnect())
                return

            yield from asyncio.sleep(10)

    @asyncio.coroutine
    def on_message(self, message):
        if not self.is_logged_in:
            return

        source = DiscordChannel(self, message.channel)
        yield from source.read_chat(message.author, message.content)

    @asyncio.coroutine
    def on_ready(self):
        self.ping_task = ensure_future(self.start_ping())

    def get_source_by_ident(self, source_ident):
        channel = self.get_channel(source_ident["id"])
        if not channel:
            return None
        return DiscordChannel(self, channel)

    def user_is_admin(self, user):
        """Return True if the user is a admin."""

        admins = self.conf.get("admins")
        if not admins:
            return False

        for u in admins:
            if u == str(user):
                return True
        return False

    @asyncio.coroutine
    def start(self):
        yield from self.login(self.conf['token'])
        yield from self.connect()

    @asyncio.coroutine
    def disconnect(self, shutdown=False):
        """Disconnect from Discord. This will log any disconnection error, but
        never raise.

        """

        if self.ping_task and not self.ping_task.done():
            self.ping_task.cancel()

        if self.conf.get("fake_connect") or self.is_closed:
            return

        try:
            yield from self.close()
        except Exception as e:
            self.log_exception(e, "Error when disconnecting")
        self.shutdown = shutdown

    def handle_timeout(self):
        current_time = time.time()
        for timestamp in list(self.message_times):
            if current_time - timestamp >= self.conf["command_period"]:
                self.message_times.remove(timestamp)
        if len(self.message_times) >= self.conf["command_limit"]:
            return True

        self.message_times.append(current_time)
        return False


@asyncio.coroutine
def bot_version_command(source, user):
    """!botstatus chat command"""

    report = "Version {}".format(Version)
    mgr = source.manager
    names = []
    for s in mgr.servers:
        names.append(s.name)
    names.sort()
    report = "Version: {}; Listening to servers: {}".format(Version,
            ", ".join(names))
    yield from source.send_chat(report)

@asyncio.coroutine
def bot_debugmode_command(source, user, state=None):
    """!debugmode chat command"""

    if state is None:
        state_desc = "on" if _log.isEnabledFor(logging.DEBUG) else "off"
        yield from source.send_chat(
                "DEBUG level logging is currently {}.".format(state_desc))
        return

    state_val = "DEBUG" if state == "on" else "INFO"
    _log.setLevel(state_val)
    yield from source.send_chat("DEBUG level logging set to {}.".format(state))

@asyncio.coroutine
def bot_listroles_command(source, user):
    """!listroles chat command"""

    roles = source.get_vanity_roles()
    if not roles:
        yield from source.send_chat("No available roles found.")
        return

    yield from source.send_chat(', '.join(r.name for r in roles))

@asyncio.coroutine
def bot_addrole_command(source, user, rolename):
    """!addrole chat command"""

    roles = source.get_vanity_roles()
    for r in roles:
        if rolename != r.name:
            continue

        yield from source.manager.add_roles(user, r)
        yield from source.send_chat(
                "Member {} has been given role {}".format(user.name, rolename))
        return

    yield from source.send_chat("Unknown role: {}".format(rolename))

@asyncio.coroutine
def bot_removerole_command(source, user, rolename):
    """!removerole chat command"""

    roles = source.get_vanity_roles()
    for r in roles:
        if rolename != r.name:
            continue

        if r not in user.roles:
            yield from source.send_chat(
                    "Member {} does not have role {}".format(user.name,
                        rolename))
            return

        yield from source.manager.remove_roles(user, r)
        yield from source.send_chat(
                "Member {} has lost role {}".format(user.name, rolename))
        return

    yield from source.send_chat("Unknown role: {}".format(rolename))

@asyncio.coroutine
def bot_glasses_command(source, user):
    """!glasses chat command"""

    message = yield from source.manager.send_message(source.channel, '( •_•)')
    yield from asyncio.sleep(0.5)
    yield from source.manager.edit_message(message, '( •_•)>⌐■-■')
    yield from asyncio.sleep(0.5)
    yield from source.manager.edit_message(message, '(⌐■_■)')

@asyncio.coroutine
def bot_deal_command(source, user):
    """!deal chat command"""

    glasses = '    ⌐■-■    '
    glasson = '   (⌐■_■)   '
    dealwith = 'deal with it'
    lines = ['            ',
             '            ',
             '            ',
             '    (•_•)   ']
    mgr = source.manager
    message = yield from mgr.send_message(source.channel,
            '```{}```'.format('\n'.join(lines)))
    yield from asyncio.sleep(0.5)
    for i in range(3):
        yield from mgr.edit_message(message, '```{}```'.format(
            '\n'.join(lines[:i] + [glasses]+lines[i + 1:])))
        yield from asyncio.sleep(0.5)
    yield from mgr.edit_message(message, '```{}```'.format(
        '\n'.join(lines[:1] + [dealwith] + lines[2:3] + [glasson])))

@asyncio.coroutine
def bot_dance_command(source, user):
    """!dance chat command"""

    mgr = source.manager
    figures = [':D|-<', ':D/-<', ':D|-<', r':D\\-<']
    message = yield from mgr.send_message(source.channel, figures[0])
    yield from asyncio.sleep(0.25)
    for n in range(2):
        for f in figures[0 if n else 1:]:
            yield from mgr.edit_message(message, f)
            yield from asyncio.sleep(0.25)
    yield from mgr.edit_message(message, figures[0])

@asyncio.coroutine
def bot_zxcdance_command(source, user):
    """!zxcdance chat command"""

    mgr = source.manager
    figures = ['└[^_^]┐', '┌[^_^]┘']
    message = yield from mgr.send_message(source.channel, figures[0])
    yield from asyncio.sleep(0.25)
    for n in range(2):
        for f in figures[0 if n else 1:]:
            yield from mgr.edit_message(message, f)
            yield from asyncio.sleep(0.25)
    yield from mgr.edit_message(message, figures[0])

@asyncio.coroutine
def bot_say_command(source, user, server, channel, message):
    """!say chat command"""

    mgr = source.manager
    dest_server = None
    for s in mgr.servers:
        if server.lower() in s.name.lower():
            dest_server = s
            break

    if not dest_server:
        yield from source.send_chat("Can't find server match for {}, must "
                "match one of: {}".format(server, ", ".join(
                    sorted([s.name for s in mgr.servers]))))
        return

    dest_channel = None
    chan_filt = lambda c: c.type == discord.ChannelType.text
    channels = list(filter(chan_filt, dest_server.channels))
    for c in channels:
        if channel.lower() in c.name.lower():
            dest_channel = s
            break

    if not dest_channel:
        yield from source.send_chat("Can't find channel match for {}, must "
                "match one of: {}".format(channel,
                    ", ".join(sorted([c.name for c in channels]))))

    yield from mgr.send_message(dest_channel, message)


# Discord bot commands
bot_commands = {
    "version" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : "admin",
        "function" : bot_version_command,
    },
    "debugmode" : {
        "args" : [
            {
                "pattern" : r"(on|off)$",
                "description" : "on|off",
                "required" : False
            } ],
        "single_user_allowed" : True,
        "source_restriction" : "admin",
        "function" : bot_debugmode_command,
    },
    "bothelp" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : None,
        "function" : bot_help_command,
    },
    "listroles" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : "channel",
        "function" : bot_listroles_command,
    },
    "addrole" : {
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "ROLE",
                "required" : True
            } ],
        "single_user_allowed" : True,
        "source_restriction" : "channel",
        "function" : bot_addrole_command,
    },
    "removerole" : {
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "ROLE",
                "required" : True
            } ],
        "single_user_allowed" : True,
        "source_restriction" : "channel",
        "function" : bot_removerole_command,
    },
    "glasses" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : None,
        "function" : bot_glasses_command,
    },
    "deal" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : None,
        "function" : bot_deal_command,
    },
    "dance" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : None,
        "function" : bot_dance_command,
    },
    "zxcdance" : {
        "args" : None,
        "single_user_allowed" : True,
        "source_restriction" : None,
        "function" : bot_zxcdance_command,
    },
    "say" : {
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "SERVER",
                "required" : True
            },
            {
                "pattern" : r".+$",
                "description" : "CHANNEL",
                "required" : True
            },
            {
                "pattern" : r".+$",
                "description" : "MESSAGE",
                "required" : True
            },
        ],
        "single_user_allowed" : True,
        "source_restriction" : None,
        "function" : bot_say_command,
    },
}

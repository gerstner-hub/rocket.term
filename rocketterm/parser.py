# vim: ts=4 et sw=4 sts=4 :

import functools
import logging
import os
import shlex
import subprocess
from enum import Enum

import rocketterm.types
import rocketterm.utils


class Command(Enum):
    """All supported commands and their text labels."""
    SendMessage = "send"
    DeleteMessage = "delmsg"
    EditMessage = "editmsg"
    RepeatMessage = "repeat"
    ReplyInThread = "reply"
    HideRoom = "hide"
    OpenRoom = "open"
    SelectRoom = "select"
    ListCommands = "commands"
    Help = "help"
    WhoIs = "whois"
    SetUserStatus = "setstatus"
    GetUserStatus = "getstatus"
    SetRoomTopic = "topic"
    SelectThread = "thread"
    LeaveThread = "nothread"
    ChatWith = "chatwith"
    JumpToMessage = "jump"
    ListDiscussions = "discussions"
    LeaveRoom = "leave"
    DestroyRoom = "destroy"
    JoinChannel = "join"
    OpenDebugger = "debugger"
    SetDefaultLogLevel = "setdefaultloglevel"
    SetLogLevel = "setloglevel"
    AddLogfile = "addlogfile"
    SetReaction = "react"
    SetStar = "star"
    DelStar = "unstar"
    GetServerInfo = "serverinfo"
    UrlOpen = "urlopen"
    FetchMessage = "fetchmsg"
    CallRestAPIGet = "restget"
    CallRestAPIPost = "restpost"
    CallRealtimeAPI = "rtapi"
    UploadFile = "upload"
    DownloadFile = "download"
    OpenFile = "openfile"
    ShowUnread = "unread"
    MarkAsRead = "markasread"
    GetRoomRoles = "roles"
    CreateRoom = "createroom"
    InviteUser = "invite"
    KickUser = "kick"
    ShowRoomBox = "showroombox"
    RoomActivity = "roomactivity"


# the first format placeholder will receive the actual command name
USAGE = {
    Command.SendMessage: "[/{}] text...: creates a new message with the given text in the currently selected room",
    Command.DeleteMessage: "/{} #MSGSPEC: deletes the given message from the currently selected room.",
    Command.EditMessage: "/{} #MSGSPEC text...: edits the text of an existing message in the currently selected room.",
    Command.RepeatMessage: "/{} COUNT text: repeatedly send a message. "
                           "The optional placeholder {MSGNUM} in the text will be replaced by the iterator count.",
    Command.ReplyInThread: "/{} #MSGSPEC text...: replies to another message or thread in the currently selected room",
    Command.HideRoom: "/{} [ROOMSPEC]: hides the current or specified room without leaving / unsubscribing from it",
    Command.OpenRoom: "/{} ROOMSPEC: re-adds the specified room that was previously hidden",
    Command.SelectRoom: "/{} ROOMSPEC: jumps to the named room",
    Command.ListCommands: "/{}: lists the names of all supported commands",
    Command.Help: "/{} COMMAND: prints a short usage of the given command",
    Command.WhoIs: "/{} @USERSPEC: prints detailed user information",
    Command.SetUserStatus:
        "/{} STATUS [MESSAGE]: sets the current user status (away,online,etc.) and an optional status message.",
    Command.GetUserStatus: "/{} @USERSPEC: gets the given user's current status and status text",
    Command.SetRoomTopic: "/{} TOPIC: Changes the topic of the current room",
    Command.SelectThread: "/{} #MSGSPEC: selects a thread to participate in by default. Leave again with /nothread.",
    Command.LeaveThread: "/{}: leaves a previously selected thread.",
    Command.ChatWith: "/{} @USERSPEC: create and select a new direct chat with the given user.",
    Command.JumpToMessage: "/{} #MSGSPEC: jumps/scrolls to the select message number in the current room.",
    Command.ListDiscussions: "/{}: lists available discussions in this room",
    Command.LeaveRoom: "/{} [ROOMSPEC]: leave the current or specified room permanently",
    Command.DestroyRoom: "/{} ROOMSPEC [--force]: destroy the given room permanently",
    Command.JoinChannel: "/{} #channel: joins the specified open chat room",
    Command.OpenDebugger: "/{}: opens an interactive python debugger to inspect program state. This requires urxvt.",
    Command.SetDefaultLogLevel: "/{} LOGLEVEL: adjusts the default Python loglevel.",
    Command.SetLogLevel: "/{} LOGGER=LOGLEVEL: adjusts the logleven of the given Python logger.",
    Command.AddLogfile: "/{} PATH: adds a logfile path to output Python logging to.",
    Command.SetReaction: "/{} #MSGSPEC [+|-]EMOJI: add or removes a reaction to/from a message.",
    Command.SetStar: "/{} #MSGSPEC: stars a message for later reference.",
    Command.DelStar: "/{} #MSGSPEC: removes a star previously added to a message.",
    Command.GetServerInfo: "/{}: retrieves remote server information.",
    Command.UrlOpen: "/{} URLSPEC: opens the given URL in the configured browser.",
    Command.FetchMessage: "/{} [MSGID|#MSGSPEC]: explicitly fetch the given message from REST API.",
    Command.CallRestAPIGet: "/{} endpoint: issue a raw REST API GET call. Result will be logged.",
    Command.CallRestAPIPost: "/{} endpoint data: issue a raw REST API POST call passing the givin data. "
                             "Result will be logged.",
    Command.CallRealtimeAPI: "/{} method JSON: call a realtime API method. Result will be logged.",
    Command.UploadFile: "/{} [--thread #MSGSPEC] path description message: "
                        "upload a local file, optionally to a specific thread.",
    Command.DownloadFile: "/{} FILESPEC PATH: download a file to a local path.",
    Command.OpenFile: "/{} FILESPEC PROGRAM: open a file in a program. "
                      "A local file path will be passed as first parameter.",
    Command.ShowUnread: "/{}: shows how many unread messages you have in this room.",
    Command.MarkAsRead: "/{}: marks any unread messages in the selected room as read.",
    Command.GetRoomRoles: "/{}: retrieve a list of special user roles in the selected room.",
    Command.CreateRoom: "/{} ROOMSPEC [@USERSPEC ...]: create a new open chat room or private group "
                        "with optional initial users.",
    Command.InviteUser: "/{} @USERSPEC: invites the given user into the currently selected room.",
    Command.KickUser: "/{} @USERSPEC: kicks the given user from the currently selected room.",
    Command.ShowRoomBox: "/{} BOOL: controls the visibility of the room box view.",
    Command.RoomActivity: "/{}: show open rooms with activity or attention status."
}

HIDDEN_COMMANDS = set([
    Command.OpenDebugger,
    Command.SetDefaultLogLevel,
    Command.AddLogfile,
    Command.SetLogLevel,
    Command.RepeatMessage,
    Command.GetServerInfo,
    Command.FetchMessage,
    Command.CallRestAPIGet,
    Command.CallRestAPIPost,
    Command.CallRealtimeAPI,
    Command.ShowUnread
])


class ParseError(Exception):

    def __init__(self, text):
        self.msg = text
        super().__init__(text)

    def getMessage(self):
        return self.msg


class _CompletionContext:
    """Command line completion context used within the Parser class."""

    def __init__(self, line, command, args, quotechar):
        self.candidates = []
        self.line = line
        self.command = command
        self.args = args
        self.quotechar = quotechar
        self.argindex = len(args) - 1
        if not args:
            self.word = ""
            self.prefix = self.line
            return
        elif self.args[-1]:
            self.word = args[-1].split()[-1]
        else:
            self.word = args[-1]

        # this contains the unmodified input line up to the to-be-completed
        # word
        self.prefix = self.line[:line.rfind(self.word)]

        # if there is no prefix word for completion and we added a quote
        # character, then remove that from the prefix line to avoid that being
        # added as part of the completion.
        if self.prefix == self.line and quotechar:
            self.prefix = self.line[:-1]

        self.is_file_completion = False

    def setCandidates(self, candidates):
        self.candidates = candidates

    def getCandidates(self):
        self.candidates.sort()
        return self.candidates

    def getOriginalLine(self):
        if not self.quotechar:
            return self.line
        else:
            return self.line[:-1]


def _filterRoomType(room, type_prefix):
    return room.typePrefix() == type_prefix


def _filterHiddenRooms(room):
    return not room.isOpen()


def _filterOpenRooms(room):
    return room.isOpen()


class Parser:
    """Command input parser class.

    This call receives command line input and performs the acutal execution of
    commands and completion of commands. It interacts mostly with the
    Controller and Comm to perform actions.
    """

    # the prefix character to initiate a command
    CMD_INIT = '/'

    def __init__(self, global_objects):

        self.m_global_objects = global_objects
        self.m_comm = global_objects.comm
        self.m_controller = global_objects.controller
        self.m_logger = logging.getLogger("parser")
        self.m_screen = global_objects.screen

        self.m_special_completers = {
            Command.SetUserStatus: self._getUserStatusCompletionCandidates,
            Command.Help: self._getHelpCompletionCandidates,
            Command.JoinChannel: self._getChannelCompletionCandidates,
            Command.SetReaction: self._getReactionCompletionCandidates,
            Command.UploadFile: self._getFileCompletionCandidates,
            Command.DownloadFile: self._getFileCompletionCandidates,
            Command.OpenFile: self._getFileCompletionCandidates
        }

        if self.m_global_objects.cmd_args.no_hidden_commands:
            global HIDDEN_COMMANDS
            HIDDEN_COMMANDS = set()

    def commandEntered(self, line):
        """Processes the given command input line and executes whatever is
        needed to.

        :return: an optional (might be None) status or error string to display
                 as a response to the input.
        """
        self.m_logger.debug("command input: " + line)

        cmd, args = self._splitCommand(line)

        if "-h" in args or "--help" in args:
            return self._getUsage(cmd)

        # call a member function _handle<SubCommand>(args)
        camel = cmd.value[0].upper() + cmd.value[1:]
        memfunc = "_handle{}".format(camel)
        try:
            handle_func = getattr(self, memfunc)
        except AttributeError:
            return "internal error: no such command handler: " + memfunc

        try:
            self.m_logger.debug("Running command {} ({})".format(
                cmd.value, memfunc
            ))
            ret = handle_func(args)
            self.m_logger.debug("{} command returned '{}'".format(cmd.value, ret))
            return ret
        except rocketterm.types.ActionNotAllowed:
            return "Server responded with 'action now allowed'"
        except rocketterm.types.MethodCallError as e:
            return "Server responded with '{}'".format(e.getErrorReason())
        except Exception as e:
            reason = str(e)
            if not reason:
                reason = str(type(e))
            try:
                reason += ": " + e.getErrorReason()
            except AttributeError:
                pass

            ret = "{} command failed with: {}".format(cmd.value, reason)
            self.m_logger.info(ret)
            import traceback
            et = traceback.format_exc()
            self.m_logger.info(et)
            return ret

    def completeCommand(self, line):
        """Attempts to perform command completion on the given input
        command line.

        :return: a tuple of ([candidate1, candidate2, ...], completed_line).
        """

        if not line:
            return [], None

        if self._isPartialCommand(line):
            return self._completeParialCommand(line)

        # if we are about to complete a partially quoted argument like:
        # '"where is # @a<tab>' then we'll get parsing errors from the shlex
        # module ... therefore attempt to split the argument with added
        # quotes to fix this situation. After splitting the quotes will be
        # removed anyway. The _CompletionContext.prefix will contain the
        # unmodified input line up to the to-be-completed word.
        for quote in ('', '"', '\''):
            try:
                line += quote
                cmd, args = self._splitCommand(line)
                break
            except ParseError:
                if quote:
                    line = line[:-1]
        else:
            return [], None

        context = _CompletionContext(line, cmd, args, quote)

        special_completer = self.m_special_completers.get(cmd, None)

        if special_completer:
            cands = special_completer(context)
        # for generic room/user completion only do this if
        # there are actually arguments
        elif args:
            cands = self._getParameterCompletionCandidates(context)
        else:
            cands = []

        context.setCandidates(cands)

        self.m_logger.debug(
            "completion context: command = {}, prefix = {}, args = {}, word = {}\n\tcandidates = {}".format(
                context.command, context.prefix, context.args, context.word, context.candidates
            )
        )
        return context.getCandidates(), self._completeLine(context)

    def _completeParialCommand(self, line):
        self.m_logger.debug("completing partial command {}".format(line))
        candidates = self._getCommandCompletionCandidates(line[1:])
        candidates = [self.CMD_INIT + cand for cand in candidates]
        context = _CompletionContext(line, None, [line], '')
        context.setCandidates(candidates)
        return context.getCandidates(), self._completeLine(context)

    def _isPartialCommand(self, line):
        """If line contains only a partial command to complete then this is
        returned, otherwise an empty string."""

        if not line.startswith(self.CMD_INIT):
            return False

        args = line.split()
        if len(args) == 1:
            try:
                _ = Command(args[0][1:])
            except ValueError:
                # we need to complete the command itself
                return True

        return False

    def _completeLine(self, context):
        """This performs the actual completion of the input line based on the
        available completion candidates.

        :return: The completed string, which might be unmodified if no
                 completion is possible.
        """

        if not context.candidates:
            return context.getOriginalLine()

        # there seems to be no python std. library function around that can do
        # this for us ... but it's not even that hard.

        # the characters that we can try to add to the base word, if
        # they share a common prefix with all the candidate words
        left_chars = context.candidates[0][len(context.word):]
        # the actual characters that we're appending to line
        to_add = ""

        while left_chars:
            tester = context.word + to_add + left_chars[0]

            # checks whether this prefix is still shared with all the
            # candidates
            matches = all([cand.startswith(tester) for cand in context.candidates])

            if matches:
                to_add += left_chars[0]
                left_chars = left_chars[1:]
            else:
                break

        new_word = context.word + to_add

        ret = context.prefix + new_word

        if len(context.candidates) == 1 and not ret.endswith(' '):
            if not context.is_file_completion or not ret.endswith(os.path.sep):
                # if this is the only possible completion also add whitespace
                # to allow adding another word right away
                ret += " "

        return ret

    def _getUsage(self, cmd):
        return USAGE[cmd].format(cmd.value)

    def _splitCommand(self, line):
        """Attempts to extract the base command and its arguments.

        The individual arguments are extracted shell style i.e. quoting is
        recognized. A special case is a regular chat message which will be
        extracted verbatim if not explicit /send command was used.

        :return: A tuple of (Command(), [arg1, arg2]).
        """

        if not line.strip():
            return None, []
        elif not line.lstrip().startswith(self.CMD_INIT):
            # a regular chat message, treat verbatim
            return Command.SendMessage, [line]

        try:
            parts = shlex.split(line)
        except ValueError as e:
            raise ParseError("syntax error: {}".format(str(e)))

        command = parts[0].lstrip(self.CMD_INIT)

        if not command:
            raise ParseError("Incomplete command '{}'".format(command))

        try:
            return Command(command), parts[1:]
        except ValueError:
            raise ParseError("unknown command '{}'".format(command))

    def _getCommandCompletionCandidates(self, prefix):
        possible = [c.value for c in Command.__members__.values() if c not in HIDDEN_COMMANDS]
        return [cmd for cmd in possible if cmd.startswith(prefix)]

    def _getUserStatusCompletionCandidates(self, context):
        states = [s.value for s in rocketterm.types.UserPresence]

        if not context.args:
            return states
        elif len(context.args) != 1:
            return []

        prefix = context.args[0]

        return [s for s in states if s.startswith(prefix)]

    def _getHelpCompletionCandidates(self, context):
        if len(context.args) > 1:
            return []

        prefix = context.word

        return self._getCommandCompletionCandidates(prefix)

    def _getChannelCompletionCandidates(self, context):
        if len(context.args) > 1:
            return []

        prefix = context.word

        channel_prefix = rocketterm.types.ChatRoom.typePrefix()

        if not prefix:
            prefix = channel_prefix
        elif not prefix.startswith(channel_prefix):
            return []

        ret = []

        for channel in self.m_controller.getChannels().values():

            label = channel.getLabel()

            if label.startswith(prefix):
                ret.append(label)

        return ret

    def _getReactionCompletionCandidates(self, context):
        if len(context.args) != 2:
            return []

        # syntax: #msgnr [+|-]:<emoji>:
        # TODO: for removal we could check the msg argument for existing
        # reactions and offer only them.

        emoji = context.word

        operator = emoji[0]
        if operator in ('+', '-'):
            if len(emoji) > 1:
                prefix = emoji[1]
                base = emoji[1:]
            else:
                return []
        else:
            operator = '+'
            prefix = emoji[0]
            base = emoji

        if prefix != ':':
            return []

        candidates = self._getEmojiCompletionCandidates(context, base)

        if emoji.startswith(operator):
            candidates = [operator + cand for cand in candidates]

        return candidates

    def _getFileCompletionCandidates(self, context):
        """Performs file name completion for the /upload command."""
        if context.command == Command.UploadFile:
            if context.argindex == 2 and context.args[0] == "--thread":
                # /upload --thread tmid /file
                pass
            elif context.argindex == 0:
                # /upload /file
                pass
            else:
                return []
        elif context.command in (Command.DownloadFile, Command.OpenFile):
            if context.argindex != 1:
                return []
        else:
            return []

        # this tells the word completion algorithm not to automatically append
        # a space for completed directory components.
        context.is_file_completion = True

        # expand possible ~ tilde occurences
        word = os.path.expanduser(context.word)

        # find out the directory we need to look into
        endsep = word.rfind(os.path.sep)
        if endsep != -1:
            searchdir = word[:endsep+1]
            prefix = word[endsep+1:]
        else:
            # the current directory
            prefix = word
            searchdir = "."

        try:
            candidates = os.listdir(searchdir)
        except Exception:
            # invalid path or access issues etc.
            return []

        if prefix:
            # filter candidates starting with the desired basename prefix
            candidates = [c for c in candidates if c.startswith(prefix)]

        if searchdir != ".":
            # rebuild the complete paths
            candidates = [os.path.join(searchdir, cand) for cand in candidates]

        # add trailing slashes for directory candidates
        candidates = [c + os.path.sep if os.path.isdir(c) else c for c in candidates]

        if word != context.word:
            # user expansion occured, so unexpand it again to make the
            # candidates match the actual prefix on the command line
            candidates = [c.replace(word, context.word, 1) for c in candidates]

        return candidates

    def _getEmojiCompletionCandidates(self, context, base):
        emojis_dict = self.m_controller.getEmojiData()
        emojis = sum(emojis_dict.values(), [])

        emoji_names = [':{}:'.format(emoji) for emoji in emojis]
        candidates = [emoji for emoji in emoji_names if emoji.startswith(base)]
        return candidates

    def _getParameterCompletionCandidates(self, context):
        """Performs generic parameter completion for rooms, users etc."""
        command = context.command

        to_complete = context.word

        if not to_complete:
            return []

        room_prefixes = [rt.typePrefix() for rt in rocketterm.types.ROOM_TYPES]
        user_prefix = rocketterm.types.UserInfo.typePrefix()

        this_prefix = to_complete[0]

        if this_prefix not in room_prefixes and this_prefix != user_prefix:
            # nothing we know how to complete
            return []

        expect_room = command in (
            Command.HideRoom, Command.OpenRoom, Command.SendMessage,
            Command.SelectRoom, Command.LeaveRoom, Command.DestroyRoom,
            Command.ReplyInThread, Command.EditMessage
        )
        expect_user = command in (
            Command.SendMessage, Command.WhoIs, Command.GetUserStatus,
            Command.ChatWith, Command.ReplyInThread, Command.EditMessage,
            Command.KickUser, Command.InviteUser
        )
        arg_indices = {
            Command.ReplyInThread: [1],
            Command.EditMessage: [1]
        }

        arg_limits = arg_indices.get(command, [])

        if arg_limits and context.argindex not in arg_limits:
            return []

        room_filters = []

        if expect_room:
            # make the completion context sensitive by only suggesting rooms
            # in states that make sense for the command
            type_filter = functools.partial(
                    _filterRoomType,
                    type_prefix=this_prefix
            )
            room_filters.append(type_filter)
            if command in (Command.HideRoom, Command.SelectRoom):
                room_filters.append(_filterOpenRooms)
            elif command == Command.OpenRoom:
                room_filters.append(_filterHiddenRooms)

        # since the room and user prefix are equal, prefer user completion
        # over room complection, it's probably uncommon to refer to a direct
        # chat
        if expect_user and this_prefix == user_prefix:
            return self._getUserCompletionCandidates(command, to_complete)
        elif expect_room:
            return self._getRoomCompletionCandidates(command, to_complete, room_filters)

        return []

    def _getUserCompletionCandidates(self, command, base):
        if command in (Command.ChatWith,):
            # consider all known users
            users = self.m_controller.getKnownUsers(load_all_users=True)
        else:
            # only consider users in the current room
            users = self.m_controller.getKnownRoomMembers(self.m_controller.getSelectedRoom())
        usernames = [user.typePrefix() + user.getUsername() for user in users]
        candidates = [user for user in usernames if user.startswith(base)]

        return candidates

    def _getRoomCompletionCandidates(self, command, base, room_filters=[]):
        rooms = self.m_controller.getJoinedRooms(filter_hidden=False)
        for _filter in room_filters:
            rooms = filter(_filter, rooms)
        room_names = [room.getLabel() for room in rooms]
        candidates = [room for room in room_names if room.startswith(base)]

        return candidates

    def _checkBooleanArg(self, arg):

        word = arg.lower().strip()

        if word in ("yes", "true", "1", "on", "enable"):
            return True
        elif word in ("no", "false", "0", "off", "disable"):
            return False

        raise ParseError("Expected boolean argument like 'yes' or 'no'")

    def _checkUsernameArg(self, username):

        if not username.startswith(rocketterm.types.UserInfo.typePrefix()):
            raise ParseError("Expected username with '@' prefix")

        return username[1:]

    def _resolveUsername(self, username):
        try:
            ret = self.m_controller.getBasicUserInfoByName(username)
            if ret is None:
                raise Exception("no such user")
            return ret
        except Exception:
            raise ParseError("Unknown user {}".format(username))

    def _handleHelp(self, args):
        if len(args) != 1:
            raise ParseError("Invalid number of arguments. Try /help help or /commands")

        target_cmd, _ = self._splitCommand(self.CMD_INIT + args[0])

        return self._getUsage(target_cmd)

    def _checkRoomArg(self, args):
        if len(args) != 1 or len(args[0]) < 2:
            raise ParseError("Expected exactly one room argument like #channel, $group or @user")

        room = args[0]

        prefix = room[0]
        prefixes = [rt.typePrefix() for rt in rocketterm.types.ROOM_TYPES]

        if prefix not in prefixes:
            raise ParseError("room argument does not start with a type prefix like {}".format(', '.join(prefixes)))

        return room

    def _getRoomFromArg(self, label, consider_unsubscribed=False):
        rooms = self.m_controller.getJoinedRooms(filter_hidden=False)

        for room in rooms:
            if room.getLabel() != label:
                continue

            return room

        if consider_unsubscribed:
            return self.m_controller.getRoomInfoByLabel(label)

        raise ParseError("No such room {}".format(label))

    def _getOptionalRoomArg(self, args):
        if args:
            label = self._checkRoomArg(args)
            room = self._getRoomFromArg(label)
        else:
            # hide the current room
            room = self.m_controller.getSelectedRoom()
            label = room.typePrefix() + room.getName()

        return room, label

    def _resolveMsgNr(self, msg_nr):
        try:
            return self.m_screen.getMsgIDForNr(msg_nr)
        except Exception:
            raise Exception("Error: message #{} not yet cached".format(msg_nr))

    def _getMsgObjFromNr(self, msg_nr_arg):
        msg_nr = self._processMsgNrArg(msg_nr_arg)
        msg_id = self._resolveMsgNr(msg_nr)
        return self.m_controller.getMessageFromID(msg_id)

    def _handleHide(self, args):
        room, room_label = self._getOptionalRoomArg(args)

        if not room.isOpen():
            return "Room {} is already hidden".format(room_label)

        self.m_controller.hideRoom(room)
        return "Hiding room {}".format(room_label)

    def _handleOpen(self, args):
        room_label = self._checkRoomArg(args)
        room = self._getRoomFromArg(room_label)
        if room.isOpen():
            return "Room {} is already open".format(room_label)

        self.m_controller.openRoom(room)
        return "Opening room {}".format(room_label)

    def _handleSelect(self, args):
        room_label = self._checkRoomArg(args)
        room = self._getRoomFromArg(room_label)

        if not room.isOpen():
            return "Room {} is hidden".format(room_label)

        self.m_controller.selectRoom(room)
        return "Selecting room {}".format(room_label)

    def _handleWhois(self, args):
        if len(args) != 1:
            raise ParseError("Expected exactly one argument @username")

        username = self._checkUsernameArg(args[0])
        basic_info = self._resolveUsername(username)

        full_info = self.m_comm.getUserInfo(basic_info)

        return "{}: {}, active = {}, status = {}, utc-offset = {}".format(
            username, full_info.getFriendlyName(),
            "1" if full_info.isActive() else "0",
            full_info.getStatus().value,
            full_info.getUTCOffset()
        )

    def _handleSetstatus(self, args):
        if not args or len(args) > 2:
            raise ParseError(
                "Invalid number of arguments, example: /{} away 'gone for a while'".format(
                    Command.SetUserStatus.value
                )
            )

        state = args[0]
        supported = [s.value for s in rocketterm.types.UserPresence]

        if state not in supported:
            raise ParseError("Invalid user status. Use one of: {}".format(', '.join(supported)))

        new_presence = rocketterm.types.UserPresence(state)

        if len(args) == 2:
            message = args[1]
        else:
            # the status message is a required API argument so let's fetch the
            # current one if it should stay as is
            our_user = self.m_comm.getLoggedInUserInfo()
            our_status = self.m_comm.getUserStatus(our_user)
            message = our_status.getMessage()

        self.m_controller.setUserStatus(new_presence, message)

        return "Changed user status to {} ({})".format(
            state, message if message else "<no status message>"
        )

    def _handleGetstatus(self, args):
        if len(args) != 1:
            raise ParseError("Expected exactly one argument")

        username = self._checkUsernameArg(args[0])
        basic_info = self._resolveUsername(username)

        status = self.m_comm.getUserStatus(basic_info)

        msg = status.getMessage()
        if msg:
            msg = '"{}"'.format(msg)
        else:
            msg = '<no status message>'

        return "Status of {}: {} {}".format(
            args[0], status.getStatus().value, msg
        )

    def _handleCommands(self, args):
        return ' '.join(sorted([c.value for c in Command if c not in HIDDEN_COMMANDS]))

    def _handleSend(self, args):
        if len(args) != 1:
            return "Invalid number of arguments"

        self.m_controller.sendMessage(args[0])

    def _handleDelmsg(self, args):
        if len(args) != 1:
            return "Expected exactly one argument: #msgnr. Example /delmsg #811"

        msg_nr = self._processMsgNrArg(args[0])
        msg_id = self._resolveMsgNr(msg_nr)

        self.m_comm.deleteMessage(self.m_controller.getMessageFromID(msg_id))

        return "Deleted message #{}".format(msg_nr)

    def _handleEditmsg(self, args):
        if len(args) != 2:
            return "Expected two arguments: #msgnr TEXT. Example: /editmsg #811 'new message text'"

        msg_nr = self._processMsgNrArg(args[0])
        msg_id = self._resolveMsgNr(msg_nr)

        self.m_comm.updateMessage(
            self.m_controller.getMessageFromID(msg_id),
            args[1]
        )

    def _handleRepeat(self, args):
        if len(args) != 2:
            return "Expected two arguments: COUNT text..."

        try:
            count = int(args[0])
        except ValueError:
            return "COUNT '{}' is not an integer".format(args[0])

        if count > 1000 or count < 0:
            return "Refusing to repeat with excess or negative count {}".format(count)

        for i in range(count):
            text = args[1].replace("{MSGNUM}", str(i+1))
            while True:
                try:
                    self.m_controller.sendMessage(text)
                    break
                except rocketterm.types.TooManyRequests:
                    import time
                    self.m_screen.commandFeedback(
                        "{}/{} messages sent. Server slows us down, waiting ...".format(
                            i + 1, count
                        )
                    )
                    time.sleep(1.0)

    def _processMsgNrArg(self, arg):
        msg_count = self.m_controller.getRoomMsgCount()

        if msg_count == 0:
            raise ParseError("No messages existing in this room")

        msg_nr = arg
        if not msg_nr.startswith('#'):
            raise ParseError("Invalid #MSGSPEC, expected something like #4711")

        msg_nr = msg_nr.lstrip('#')

        if not msg_nr.isnumeric():
            raise ParseError("#MSGSPEC '{}' is not numerical".format(msg_nr))

        msg_nr = int(msg_nr)

        if msg_nr < 1 or msg_nr > msg_count:
            raise ParseError("Message #{} is out of range. The allowed range is #1 ... #{}".format(
                msg_nr, msg_count
            ))

        # since Screen handles the mapping between actual and displayed msg
        # IDs we need to consult it.
        return msg_nr

    def _handleReply(self, args):
        if len(args) != 2:
            return "Invalid number of arguments: Example: /reply #5 'my reply text'"

        thread_nr = self._processMsgNrArg(args[0])
        root_id = self._resolveMsgNr(thread_nr)

        self.m_comm.sendMessage(
            self.m_controller.getSelectedRoom(),
            args[1],
            thread_id=root_id
        )

        return "Replied in thread #{}".format(thread_nr)

    def _handleTopic(self, args):
        if len(args) != 1:
            raise ParseError("Expected exactly one argument")

        topic = args[0]

        current_room = self.m_controller.getSelectedRoom()
        self.m_comm.setRoomTopic(current_room, topic)

        return "Changing room topic to {}".format(topic)

    def _handleThread(self, args):
        if len(args) != 1:
            return "Invalid number of arguments. Example: /select #7"

        thread_nr = self._processMsgNrArg(args[0])

        self.m_screen.selectThread(thread_nr)
        return "Now writing in thread #{}".format(thread_nr)

    def _handleNothread(self, args):
        if args:
            return "Expected no arguments. Example: /nothread"

        msg_id = self.m_controller.getSelectedThreadID()

        if not msg_id:
            return "Currently no thread selected"

        msg_nrs = self.m_screen.getNrsForMsgID(msg_id)

        self.m_screen.leaveThread()
        return "Left thread #{}".format(msg_nrs[0])

    def _handleChatwith(self, args):
        if len(args) != 1:
            return "Invalid number of arguments. Example: /chatwith @user"

        user = self._checkUsernameArg(args[0])
        peer_info = self._resolveUsername(user)

        our_info = self.m_controller.getLoggedInUserInfo()

        for chat in self.m_controller.getJoinedDirectChats(filter_hidden=False):
            if chat.getPeerUserID(our_info) == peer_info.getID():
                if not chat.isOpen():
                    self.m_controller.openRoom(chat)
                    msg = "Opening existing direct chat {}".format(args[0])
                else:
                    msg = "Selecting existing direct chat {}".format(args[0])

                self.m_controller.selectRoom(chat)
                return msg

        self.m_comm.createDirectChat(peer_info)
        return "Created new direct chat {}".format(args[0])

    def _handleJump(self, args):
        if len(args) != 1:
            return "Invalid number of arguments. Example: /jump #815"

        nr = self._processMsgNrArg(args[0])

        try:
            self.m_screen.scrollToMessage(nr)
        except Exception as e:
            return "Failed with: {}".format(str(e))

        return "Jumped to #{}".format(nr)

    def _handleReact(self, args):

        if len(args) != 2:
            return "invalid number of arguments. Example: /react #432 :crying:"

        msg_nr = self._processMsgNrArg(args[0])
        msg_id = self._resolveMsgNr(msg_nr)

        emoji = args[1]

        add_reaction = True
        if emoji.startswith('+'):
            emoji = emoji[1:]
        elif emoji.startswith('-'):
            add_reaction = False
            emoji = emoji[1:]

        if not emoji.startswith(':') or not emoji.endswith(':'):
            return "invalid emoji syntax {}. needs to be surrounded with ':' like ':crying:'".format(emoji)

        msg = self.m_controller.getMessageFromID(msg_id)

        if add_reaction:
            self.m_comm.addReaction(msg, emoji)
            return "Added reaction {} to {}".format(emoji, args[0])
        else:
            self.m_comm.delReaction(msg, emoji)
            return "Removed reaction {} from {}".format(emoji, args[0])

    def _handleStar(self, args):
        if len(args) != 1:
            return "invalid number of arguments. expected only #MSGSPEC."

        msg = self._getMsgObjFromNr(args[0])
        self.m_comm.setMessageStar(msg)
        return "starred message {}".format(args[0])

    def _handleUnstar(self, args):
        if len(args) != 1:
            return "invalid number of arguments. expected only #MSGSPEC."

        msg = self._getMsgObjFromNr(args[0])
        self.m_comm.delMessageStar(msg)
        return "unstarred message {}".format(args[0])

    def _handleDiscussions(self, args):
        if len(args) != 0:
            return "Invalid number of arguments. Expected no arguments."

        discussions = self.m_comm.getRoomDiscussions(
                self.m_controller.getSelectedRoom()
        )

        if not discussions:
            return "No discussions existing in this room"

        return "{} existing discussions: {}".format(
            len(discussions),
            ', '.join([d.typePrefix() + d.getFriendlyName() for d in discussions])
        )

    def _handleLeave(self, args):
        room, label = self._getOptionalRoomArg(args)

        self.m_comm.leaveRoom(room)

        return "Left room " + label

    def _handleJoin(self, args):
        label = self._checkRoomArg(args)
        room = self._getRoomFromArg(label, consider_unsubscribed=True)

        if not room.isChatRoom():
            return "can only join open chat rooms, not " + room.typeLabel()

        self.m_controller.joinChannel(room)

        return "Joined room " + room.getLabel()

    def _handleDestroy(self, args):
        if len(args) == 2 and args[-1].lower() == "--force":
            force = True
            args.pop()
        else:
            force = False

        label = self._checkRoomArg(args)
        room = self._getRoomFromArg(label)

        if room.isDirectChat():
            return "direct chats cannot be erased"

        user_count = self.m_controller.getRoomUserCount(room)

        if user_count > 1 and not force:
            return "The room {} still has {} users in it. If you really want " \
                   "to destroy it add the --force parameter.".format(room.getLabel(), user_count)

        self.m_comm.eraseRoom(room)

        return "Destroyed room " + room.getLabel()

    def _handleDebugger(self, args):
        # we can either run the debugger in the same terminal, which will mess
        # up the screen an we won't have echo (didn't find a way to enable it
        # again from within urwid, ncurses claims it wasn't initialized yet)
        # ... or we use a PTY and connect the pdb to it. This also has some
        # downsides like no tab-completion etc. but as a last rest it works
        # okay.
        import os
        import bdb
        import pdb
        import pty

        try:
            master, slave = pty.openpty()
            proc = subprocess.Popen(
                    ["urxvt", "-pty-fd", str(master)],
                    pass_fds=[master],
                    stderr=subprocess.DEVNULL
            )

            pty_pdb = pdb.Pdb(
                    stdin=os.fdopen(slave, 'r'),
                    stdout=os.fdopen(slave, 'w')
            )
            pty_pdb.set_trace()
            # this pass instruction is important for the BdbQuit except clause
            # below to work. It seems pdb is skipping over one instruction
            # after set_trace() for some reason, causing the exception to be
            # raised in the outer context if we don't add this pass
            # instruction
            pass
        except bdb.BdbQuit:
            err = "success"
        except FileNotFoundError:
            return "error: the 'urxvt' terminal emulator was not found. It is required for debugging."
        except Exception as e:
            err = str(e)

        os.close(master)
        os.close(slave)
        proc.terminate()
        proc.wait()

        return "Returned from debugger with: " + err

    def _handleSetdefaultloglevel(self, args):

        if len(args) != 1:
            return "invalid number of arguments. expected only LOGLEVEL."

        level = args[0]

        self.m_global_objects.log_manager.setDefaultLogLevel(level)

        return "Default loglevel changed to " + level

    def _handleSetloglevel(self, args):

        if len(args) != 1:
            return "invalid number of arguments. expected only LOGGER=LEVEL."

        setting = args[0]

        self.m_global_objects.log_manager.applyLogLevels(setting)

        return "Applied loglevel setting " + setting

    def _handleAddlogfile(self, args):

        if len(args) != 1:
            return "invalid number of arguments. expected only PATH."

        path = args[0]

        self.m_global_objects.log_manager.addLogfile(path)

        return "Added logfile output in " + path

    def _handleServerinfo(self, args):

        if len(args) != 0:
            return "expected no parameters."

        info = self.m_comm.getServerInfo()

        return str(info.getVersion())

    def _handleUrlopen(self, args):

        if len(args) != 1:
            return "expected exactly one parameter: URLSPEC. Example: /urlopen [1]"

        urlspec = args[0]

        if len(urlspec) < 3 or not urlspec[1:-1].isnumeric():
            return "invalid URLSPEC syntax. Expected something like '[4]'"

        urlnum = int(urlspec[1:-1])

        url = self.m_screen.getURLForIndex(urlnum)
        if not url:
            return "Invalid URLSPEC [{}]. No such URL.".format(urlnum)

        browser = os.environ.get('BROWSER', None)

        if not browser:
            return "$BROWSER environment variable not set. Cannot open URL."

        subprocess.call([browser, url], stderr=subprocess.DEVNULL)

        # if this was a terminal app that took over control of graphics then
        # force redraw of the urwid screen to put everything back in place
        self.m_screen.refresh()

        return "Opened URL {} in {}".format(url, browser)

    def _handleFetchmsg(self, args):

        if len(args) != 1:
            return "expected exactly one parameter: MSGID."

        arg = args[0]

        if arg.startswith('#'):
            msg_nr = self._processMsgNrArg(arg)
            msg_id = self._resolveMsgNr(msg_nr)
        else:
            msg_id = arg

        msg = self.m_comm.getMessageByID(msg_id)

        self.m_logger.info(msg.getRaw())

        return "Fetched message {}".format(msg.getID())

    def _handleRestget(self, args):

        if len(args) != 1:
            return "expected exactly one parameter: endpoint. Example: '/restget channels.counters?roomName=myRoom'"

        reply = self.m_comm.callREST_Get(args[0])

        self.m_logger.info("REST GET result for {}: {}".format(args[0], reply))

        return "Performed REST GET request {}".format(args[0])

    def _handleRestpost(self, args):

        if len(args) != 2:
            return "expected two parameters: endpoint and data. "\
                   "Example: /restpost subscriptions.read '{\"rid\": \"GENERAL\"}'"

        endpoint, data = args

        reply = self.m_comm.callREST_Post(endpoint, data)

        self.m_logger.info(f"REST POST result for {endpoint} with data '{data}': {reply}")

        return f"Performed REST POST request {endpoint}"

    def _handleRtapi(self, args):

        if len(args) != 2:
            return "expected two parameters: 'method name' and 'JSON'. Example: /rtapi getRoomRoles '[ \"GENERAL\" ]'"

        import json
        import pprint

        method = args[0]
        try:
            params = json.loads(args[1])
        except Exception as e:
            return "failed to parse JSON parameters: {}".format(e)

        reply = self.m_comm.callRealtimeMethod(method, params)

        self.m_logger.info("RTAPI method call '{} {}' result = '{}'".format(
            method, pprint.pformat(params), pprint.pformat(reply)
        ))

        return "Performed realtime API method call {}".format(method)

    def _handleUpload(self, args):

        if len(args) not in (3, 5):
            return "expected at least three parameters. Example: "\
                    "/upload /etc/issue \"my issue file\" \"what do you think of my issue?\""

        if len(args) == 5:
            if args.pop(0) != "--thread":
                return "expected '--thread' as first parameter"
            msg_nr = args.pop(0)
            thread_id = self._getMsgObjFromNr(msg_nr)
        else:
            thread_id = None

        path, description, message = args

        # epxand possible tilde component
        path = os.path.expanduser(path)

        self.m_comm.uploadFileMessage(
                self.m_controller.getSelectedRoom(),
                path,
                message,
                description,
                thread_id=thread_id
        )

        return "Uploaded file {}".format(path)

    def _openOutputFile(self, info, path):
        """Safely opens an output file based on the given user specified path.

        The output path may be modified in case this becomes necessary for
        safety reasons (e.g. public /tmp directory).

        :return: a tuple of (path, file-like object)
        """

        path = os.path.expanduser(path)
        path = path.rstrip(os.path.sep)

        if not os.path.isdir(path):
            _file = open(path, 'xb')
            return path, _file

        import stat

        # derive a safe basename from the attachment name
        base = rocketterm.utils.getSafeFilename(info.getName())
        is_public_dir = os.stat(path).st_mode & stat.S_IWOTH

        if is_public_dir:
            _file = rocketterm.utils.openTempFile(
                base, dir=path, auto_delete=False
            )
            return _file.name, _file
        else:
            path = os.path.sep.join([path, base])
            _file = open(path, 'xb')
            return path, _file

    def _resolveFileSpec(self, arg):
        filespec = arg

        if len(filespec) < 4 or not filespec[2:-1].isnumeric():
            raise Exception("invalid FILESPEC syntax. Expected something like '[!4]'")

        filenum = int(filespec[2:-1])

        info = self.m_screen.getFileInfoForIndex(filenum)
        if not info:
            raise Exception("Invalid FILESPEC [!{}]. No such file attachment.".format(filenum))

        return info

    def _handleDownload(self, args):

        if len(args) != 2:
            return "expected two parameters: FILESPEC PATH. Example: /download [!1] ~/myfile.txt"

        info = self._resolveFileSpec(args[0])
        outpath, outfile = self._openOutputFile(info, args[1])

        try:
            self.m_comm.downloadFile(info, outfile)
        except Exception:
            try:
                os.remove(outpath)
            except Exception:
                pass
            raise
        finally:
            outfile.close()

        return "Saved file {} '{}' as {}".format(args[0], info.getName(), outpath)

    def _handleOpenfile(self, args):

        if len(args) != 2:
            return "expected two parameters: FILESPEC PROGRAM. Example: /openfile [!1] /usr/bin/vim"

        info = self._resolveFileSpec(args[0])

        prog = args[1]

        if os.path.isabs(prog):
            if not os.path.isfile(prog):
                return "Program '{}' does not exist".format(prog)
        else:
            import shutil
            path = shutil.which(prog)
            if path is None:
                return "Could not find program '{}'".format(prog)

            prog = path

        outfile = rocketterm.utils.openTempFile(info.getName())

        self.m_comm.downloadFile(info, outfile)

        try:
            res = subprocess.run([prog, outfile.name], shell=False, close_fds=True)
        finally:
            # if this was a terminal app that took over control of graphics then
            # force redraw of the urwid screen to put everything back in place
            self.m_screen.refresh()

        return "Opened attachment {} '{}' in {}. Exit code = {}".format(
            args[0], info.getName(), prog, res.returncode
        )

    def _handleUnread(self, args):

        if len(args) != 0:
            return "expected no parameters"

        room = self.m_controller.getSelectedRoom()
        subscription = room.getSubscription()

        num_unread = subscription.getUnread()
        threads = subscription.getUnreadThreads()
        thread_nrs = []
        for msg_id in threads:
            msg_nrs = self.m_screen.getNrsForMsgID(msg_id)
            nr = msg_nrs[0] if msg_nrs else '?uncached?'
            thread_nrs.append(f"#{nr}")

        ret = f"{num_unread} unread messages."

        if thread_nrs:
            ret += " {} unread threads: {}.".format(
                num_unread, ', '.join(thread_nrs)
            )

        return ret

    def _handleMarkasread(self, args):

        if len(args) != 0:
            return "expected no parameters"

        room = self.m_controller.getSelectedRoom()

        self.m_comm.markRoomAsRead(room)

        return "Marked room as read"

    def _handleRoles(self, args):

        if len(args) != 0:
            return "expected no parameters"

        room = self.m_controller.getSelectedRoom()

        roles = self.m_comm.getRoomRoles(room)

        if not roles:
            return "no special roles in this room"

        roles = ["{}({})".format(user.getLabel(), ",".join(roles)) for user, roles in roles]
        return " ".join(roles)

    def _handleCreateroom(self, args):

        if len(args) == 0:
            return "Expected ROOMSPEC argument. Example: '/createroom #mychannel @mybuddy1 @mybuddy2'."

        roomspec = args[0]
        users = args[1:]

        from rocketterm.types import PrivateChat, ChatRoom, UserInfo

        if roomspec[0] not in (PrivateChat.typePrefix(), ChatRoom.typePrefix()):
            return f"Invalid ROOMSPEC prefix in {roomspec}. Expected chat room or private group."

        user_prefix = UserInfo.typePrefix()

        for user in users:
            if not user.startswith(user_prefix):
                return f"Invalid username {user}. Missing {user_prefix} prefix."
        users = [user[1:] for user in users]

        self.m_controller.createRoom(roomspec, users)

        return f"Created new room {roomspec}"

    def _handleInvite(self, args):

        if len(args) != 1:
            return "Expected exactly one USERSPEC argument. Example: '/invite @mybuddy'"

        username = self._checkUsernameArg(args[0])
        user = self._resolveUsername(username)

        room = self.m_controller.getSelectedRoom()
        self.m_comm.inviteUserToRoom(room, user)

        return f"Invited {user.getLabel()} into {room.getLabel()}."

    def _handleKick(self, args):

        if len(args) != 1:
            return "Expected exactly one USERSPEC argument. Example: '/kick @myenemy'"

        username = self._checkUsernameArg(args[0])
        user = self._resolveUsername(username)

        room = self.m_controller.getSelectedRoom()
        self.m_comm.kickUserFromRoom(room, user)

        return f"Kicked {user.getLabel()} from {room.getLabel()}."

    def _handleShowroombox(self, args):

        if len(args) != 1:
            return "Expected exactly one BOOL argument. Example: '/showroombox off'"

        on_off = self._checkBooleanArg(args[0])

        self.m_screen.setRoomBoxVisible(on_off)

        return f"Switched roombox visibility to {on_off}"

    def _handleRoomactivity(self, args):

        if args:
            return "Expected no arguments for this command."

        from rocketterm.types import RoomState

        getRoomInfo = self.m_controller.getRoomInfoByID

        states = self.m_screen.getRoomStates()
        active_rooms = [getRoomInfo(rid) for rid, state in states.items() if state == RoomState.ACTIVITY]
        attention_rooms = [getRoomInfo(rid) for rid, state in states.items() if state == RoomState.ATTENTION]

        if active_rooms:
            active = "Rooms with activity: " + ", ".join([room.getLabel() for room in active_rooms])
        else:
            active = ""

        if attention_rooms:
            attention = "Rooms with attention flag: " + ", ".join([room.getLabel() for room in attention_rooms])
        else:
            attention = ""

        if active or attention:
            return '\n'.join((active, attention))
        else:
            return 'no rooms with activity or attention flag'

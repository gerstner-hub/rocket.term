# vim: ts=4 et sw=4 sts=4 :

from enum import Enum
import functools
import logging
import shlex

import rocketterm.types


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
    Command.SetReaction: "/{} #MSGSPEC [+|-]EMOJI: add or removes a reaction to/from a message."
}

HIDDEN_COMMANDS = set([
    Command.OpenDebugger,
    Command.SetDefaultLogLevel,
    Command.AddLogfile,
    Command.SetLogLevel,
    Command.RepeatMessage
])


class ParseError(Exception):

    def __init__(self, text):
        self.msg = text
        super().__init__(text)

    def getMessage(self):
        return self.msg


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
            Command.SetReaction: self._getReactionCompletionCandidates
        }

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
            return "Server responded with '{}'".format(e.reason())
        except Exception as e:
            reason = str(e)
            if not reason:
                reason = str(type(e))
            return "{} command failed with: {}".format(cmd.value, reason)

    def completeCommand(self, line):
        """Attempts to perform command completion on the given input
        command line.

        :return: a tuple of ([candidate1, candidate2, ...], completed_line).
        """

        if not line:
            return [], None

        partial_cmd = ""

        try:
            cmd, args = self._splitCommand(line)
        except Exception:
            # maybe we need to complete the command itself
            args = shlex.split(line)
            if len(args) == 1:
                partial_cmd = args[0]
                args = []
            else:
                return [], None

        candidates = []

        if partial_cmd.startswith(self.CMD_INIT):
            candidates = self._getCommandCompletionCandidates(partial_cmd[1:])
            candidates = [self.CMD_INIT + cand for cand in candidates]
        else:
            special_completer = self.m_special_completers.get(cmd, None)

            if special_completer:
                candidates = special_completer(cmd, args)
            # for generic room/user completion only do this if
            # there are actually arguments
            elif args:
                candidates = self._getParameterCompletionCandidates(cmd, args)

        candidates.sort()
        return candidates, self._completeLine(line, candidates)

    def _completeLine(self, line, candidates):
        """This performs the actual completion of the input line based on the
        available completion candidates.

        :return: The completed string, which might be unmodified if no
                 completion is possible.
        """

        if not candidates:
            return line

        # there seems to be no python std. library function around that can do
        # this for us ... but it's not even that hard.

        # the base word (whitespace separated) that we're trying to
        # tab-complete
        parts = shlex.split(line)
        word = parts[-1]
        # the characters that we can try to add to the base word, if
        # it they shared a common prefix with all the candidate words
        left_chars = candidates[0][len(word):]
        # the actual characters that we're appending to line
        to_add = ""

        while left_chars:
            tester = word + to_add + left_chars[0]

            # checks whether this prefix is still shared with all
            # the candidates
            matches = all([cand.startswith(tester) for cand in candidates])

            if matches:
                to_add += left_chars[0]
                left_chars = left_chars[1:]
            else:
                break

        new_word = word + to_add

        # the new word contains whitespace, so add quotes
        if len(new_word.split()) != 1:
            new_word = '"{}"'.format(new_word)

        ret = ' '.join(parts[:-1] + [new_word])

        if len(candidates) == 1 and not ret.endswith(' '):
            # if this is the only possible completion also add
            # whitespace to allow adding another word right away
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

    def _getUserStatusCompletionCandidates(self, command, args):
        states = [s.value for s in rocketterm.types.UserPresence]

        if not args:
            return states
        elif len(args) != 1:
            return []

        prefix = args[0]

        return [s for s in states if s.startswith(prefix)]

    def _getHelpCompletionCandidates(self, command, args):
        if not args:
            prefix = ""
        elif len(args) == 1:
            prefix = args[0]
        else:
            return []

        return self._getCommandCompletionCandidates(prefix)

    def _getChannelCompletionCandidates(self, command, args):
        if not args or len(args) != 1:
            return []

        prefix = args[0]

        if not prefix.startswith(rocketterm.types.ChatRoom.typePrefix()):
            return []

        ret = []

        for channel in self.m_controller.getChannels().values():

            label = channel.getLabel()

            if label.startswith(prefix):
                ret.append(label)

        return ret

    def _getReactionCompletionCandidates(self, command, args):
        if not args or len(args) != 2:
            return []

        # syntax: #msgnr [+|-]:<emoji>:

        emoji = args[1]

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

        candidates = self._getEmojiCompletionCandidates(command, base)

        if emoji.startswith(operator):
            candidates = [operator + cand for cand in candidates]

        return candidates

    def _getEmojiCompletionCandidates(self, command, base):
        emojis_dict = self.m_controller.getEmojiData()
        emojis = sum(emojis_dict.values(), [])

        emoji_names = [':{}:'.format(emoji) for emoji in emojis]
        candidates = [emoji for emoji in emoji_names if emoji.startswith(base)]
        return candidates

    def _getParameterCompletionCandidates(self, command, args):
        """Performs generic parameter completion for rooms, users etc."""
        to_complete = args[-1]

        room_prefixes = [rt.typePrefix() for rt in rocketterm.types.ROOM_TYPES]
        user_prefix = rocketterm.types.UserInfo.typePrefix()

        this_prefix = to_complete[0]

        if this_prefix not in room_prefixes and this_prefix != user_prefix:
            # nothing we know how to complete
            return []

        expect_room = command in (
                Command.HideRoom, Command.OpenRoom, Command.SendMessage,
                Command.SelectRoom, Command.LeaveRoom, Command.DestroyRoom
        )
        expect_user = command in (Command.SendMessage, Command.WhoIs, Command.GetUserStatus, Command.ChatWith)
        room_filters = []

        if expect_room:
            # make the completion context sensitive by only suggesting rooms
            # in states that make sense for the command
            type_filter = functools.partial(
                    _filterRoomType,
                    type_prefix=this_prefix
            )
            room_filters.append(type_filter)
            if command == Command.HideRoom:
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

    def _checkUsernameArg(self, username):

        if not username.startswith(rocketterm.types.UserInfo.typePrefix()):
            raise ParseError("Expected username with '@' prefix")

        return username[1:]

    def _resolveUsername(self, username):
        try:
            return self.m_controller.getBasicUserInfoByName(username)
        except Exception:
            pass

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
        return ' '.join([c.value for c in Command if c not in HIDDEN_COMMANDS])

    def _handleSend(self, args):
        if len(args) != 1:
            return "Invalid number of arguments"

        self.m_controller.sendMessage(args[0])

    def _handleDelmsg(self, args):
        if len(args) != 1:
            return "Expected exactly one argument: #msgnr. Example /delmsg #811"

        msg_nr = self._processMsgNrArg(args[0])
        try:
            msg_id = self.m_screen.getMsgIDForNr(msg_nr)
        except Exception:
            return "Error: message #{} not yet cached".format(msg_nr)

        self.m_comm.deleteMessage(self.m_controller.getMessageFromID(msg_id))

        return "Deleted message #{}".format(msg_nr)

    def _handleEditmsg(self, args):
        if len(args) != 2:
            return "Expected two arguments: #msgnr TEXT. Example: /editmsg #811 'new message text'"

        msg_nr = self._processMsgNrArg(args[0])
        try:
            msg_id = self.m_screen.getMsgIDForNr(msg_nr)
        except Exception:
            return "Error: message #{} not yet cached".format(msg_nr)

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
        try:
            root_id = self.m_screen.getMsgIDForNr(thread_nr)
        except Exception:
            return "Error: thread #{} not yet cached".format(thread_nr)

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
        msg_id = self.m_screen.getMsgIDForNr(msg_nr)

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
        import subprocess

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

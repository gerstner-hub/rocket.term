# vim: ts=4 et sw=4 sts=4 :

from enum import Enum
import functools
import logging
import shlex

import rocketterm.types


class Command(Enum):
    """All supported commands and their text labels."""
    SendMessage = "send"
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


# the first format placeholder will receive the actual command name
USAGE = {
    Command.SendMessage: "[/{}] text...: creates a new message with the given text in the currently selected room",
    Command.ReplyInThread: "/{} #MSGSPEC text...: replies to another message or thread in the currently selected room",
    Command.HideRoom: "/{} [ROOMSPEC]: hides the current or specified room without leaving / unsubscribing from it",
    Command.OpenRoom: "/{} ROOMSPEC: re-adds the specified room that was previously hidden",
    Command.SelectRoom: "/{} ROOMSPEC: jumps to the named room",
    Command.ListCommands: "/{}: lists the names of all supported commands",
    Command.Help: "/{} COMMAND: prints a short usage of the given command",
    Command.WhoIs: "/{} @USERSPEC: prints detailed user information",
    Command.SetUserStatus: "/{} STATUS [MESSAGE]: sets the current user status (away,online,etc.) and an optional status message.",
    Command.GetUserStatus: "/{} @USERSPEC: gets the given user's current status and status text",
    Command.SetRoomTopic: "/{} TOPIC: Changes the topic of the current room",
    Command.SelectThread: "/{} #MSGSPEC: selects a thread to participate in by default. Leave again with /nothread.",
    Command.LeaveThread: "/{}: leaves a previously selected thread.",
    Command.ChatWith: "/{} @USERSPEC: create and select a new direct chat with the given user.",
    Command.JumpToMessage: "/{} #MSGSPEC: jumps/scrolls to the select message number in the current room.",
    Command.ListDiscussions: "/{}: lists available discussions in this room"
}


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

    def __init__(self, comm, controller, screen):

        self.m_comm = comm
        self.m_controller = controller
        self.m_logger = logging.getLogger("parser")
        self.m_screen = screen

        self.m_special_completers = {
            Command.SetUserStatus: self._getUserStatusCompletionCandidates,
            Command.Help: self._getHelpCompletionCandidates
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
        handle_func = getattr(self, memfunc)
        try:
            return handle_func(args)
        except rocketterm.types.ActionNotAllowed:
            return "Server responded with 'action now allowed'"
        except rocketterm.types.MethodCallError as e:
            return "Server responded with '{}'".format(e.reason())
        except Exception as e:
            reason = str(e)
            if not reason:
                reason = str(type(e))
            return "Command failed with: {}".format(reason)

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
        base = shlex.split(line)[-1]
        # the characters that we can try to add to the base word, if
        # it they shared a common prefix with all the candidate words
        left_chars = candidates[0][len(base):]
        # the actual characters that we're appending to line
        to_add = ""

        while left_chars:
            tester = base + to_add + left_chars[0]

            # checks whether this prefix is still shared with all
            # the candidates
            matches = all([cand.startswith(tester) for cand in candidates])

            if matches:
                to_add += left_chars[0]
                left_chars = left_chars[1:]
            else:
                break

        ret = line + to_add

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
        possible = [c.value for c in Command.__members__.values()]
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

    def _getParameterCompletionCandidates(self, command, args):
        """Performs generic parameter completion for rooms, users etc."""
        to_complete = args[-1]

        room_prefixes = [rt.typePrefix() for rt in rocketterm.types.ROOM_TYPES]
        user_prefix = rocketterm.types.UserInfo.typePrefix()

        this_prefix = to_complete[0]

        if this_prefix not in room_prefixes and this_prefix != user_prefix:
            # nothing we know how to complete
            return []

        expect_room = command in (Command.HideRoom, Command.OpenRoom, Command.SendMessage, Command.SelectRoom)
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
            # NOTE: this can take a long time, we'd need some kind of UI
            # feedback to indicate that something is going on
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
        room_names = [room.typePrefix() + room.getName() for room in rooms]
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

    def _getRoomFromArg(self, label):
        rooms = self.m_controller.getJoinedRooms(filter_hidden=False)
        prefix = label[0]
        name = label[1:]

        for room in rooms:
            if room.typePrefix() != prefix:
                continue
            elif room.getName() != name:
                continue

            return room

        raise ParseError("No such room {}".format(label))

    def _handleHide(self, args):
        if args:
            room_label = self._checkRoomArg(args)
            room = self._getRoomFromArg(room_label)
        else:
            # hide the current room
            room = self.m_controller.getSelectedRoom()
            room_label = room.typePrefix() + room.getName()

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
        return ' '.join([c.value for c in Command])

    def _handleSend(self, args):
        if len(args) != 1:
            return "Invalid number of arguments"

        self.m_controller.sendMessage(args[0])

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

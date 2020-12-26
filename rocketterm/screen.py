# vim: ts=4 et sw=4 sts=4 :

from enum import Enum
import os
import logging
import textwrap

import urwid

import rocketterm.controller
import rocketterm.types
import rocketterm.parser
from rocketterm.widgets import CommandInput, SizedListBox

ScrollDirection = Enum('ScrollDirection', "OLDER NEWER NEWEST OLDEST")
RoomState = Enum('RoomState', "NORMAL ACTIVITY ATTENTION")
Direction = Enum('Direction', "PREV NEXT")


class Screen:
    """The Screen class takes are of all user interface display logic.

    Screen interacts tightly with the Controller to perform its tasks. It uses
    urwid to manage the terminal screen.
    """

    # an urwid palette that allows to reuse common colors for similar UI
    # items.
    palette = (
        ('text', 'white', 'black', '', '#ffa', '#60d'),
        ('selected_text', 'white,standout', 'black', '', '#ffa', '#60d'),
        ('activity_text', 'light magenta', 'black', '', '#ffa', '#60d'),
        ('attention_text', 'light red', 'black', '', '#ffa', '#60d'),
        ('bg1', 'black', 'black', '', 'g99', '#d06'),
        ('bg2', 'light green', 'black', '', 'g99', '#d06'),
        ('border', 'light magenta', 'white', '', 'g38', '#808'),
        ('topic_bar', 'brown', 'dark green', '', 'g38', '#808'),
        ('date_bar', 'white', 'dark gray', '', 'g38', '#808'),
        ('input', 'white', 'black')
    )

    def __init__(self, global_objects):
        """
        :param dict config: The preprocessed configuration data.
        :param comm: The comm instance to use to talk to the RC server.
        """
        self.m_logger = logging.getLogger("screen")
        self.m_comm = global_objects.comm
        self.m_global_objects = global_objects
        self.m_controller = rocketterm.controller.Controller(self, self.m_comm)
        self.m_global_objects.controller = self.m_controller
        # this is the chat / command input area
        self.m_cmd_input = CommandInput(self._commandEntered, self._completeCommand)
        # this will display the current room's messages
        self.m_chat_box = SizedListBox(urwid.SimpleListWalker([]), size_callback=self._chatBoxResized)
        # this will hold the list of open rooms
        self.m_room_box = SizedListBox(urwid.SimpleListWalker([]), size_callback=self._roomBoxResized)
        # this will display status messages mostly for responses to commands
        self.m_status_box = SizedListBox(urwid.SimpleListWalker([]))
        # username -> color name. A consecutively chosen color for each
        # username encountered.
        self.m_user_colors = {}
        # room ID -> RoomState. an abstract UI room state we keep track of for
        # different coloring of rooms in the room box.
        self.m_room_states = {}
        # the currently selected room object
        self.m_current_room = None
        self.m_loop_running = False

        # a frame that we use just for its header, which becomes a bar
        # displaying the room topic
        self.m_chat_frame = urwid.Frame(
            urwid.AttrMap(self.m_chat_box, 'bg1')
        )

        # columns for holding the room box and the chat frame 10/90 relation
        # regarding the width
        columns = urwid.Columns(
            [
                ('weight', 10, urwid.AttrMap(self.m_room_box, 'bg1')),
                ('weight', 90, self.m_chat_frame)
            ],
            min_width=20,
            dividechars=1
        )

        # this will be the main outer frame, containing a heading bar as
        # header (will be generated dynamically in _updateMainHeading()),
        # the columns with room box and chat box as main content and a pile
        # with status box and input box as footer.
        self.m_frame = urwid.Frame(
            urwid.AttrMap(columns, 'border'),
            footer=urwid.Pile([]),
            header=None,
            focus_part='footer'
        )

        footer_pile = self.m_frame.contents["footer"][0]
        footer_pile.contents.append((
            urwid.AttrMap(urwid.Text("Command Input", align='center'), 'border'),
            footer_pile.options()
        ))
        footer_pile.contents.append((
            urwid.AttrMap(self.m_status_box, 'bg2'),
            footer_pile.options(height_type='given', height_amount=1)
        ))
        footer_pile.contents.append((
            urwid.AttrMap(self.m_cmd_input, 'input'),
            footer_pile.options()
        ))
        footer_pile.focus_position = len(footer_pile.contents) - 1

        self.m_loop = urwid.MainLoop(
            self.m_frame,
            self.palette,
            unhandled_input=self._handleInput,
            # disable mouse handling to support usual copy/paste interaction
            handle_mouse=False
        )

    def _externalEvent(self, data):
        """Called from the urwid main loop when an event was caused by writing
        to the event pipe.

        Currently only the Controller writes to the pipe to wake the urwid
        main thread up, letting us process asychronous events.

        :param bytes data: The data that was written asynchronously to
                           self.m_urwid_pipe.
        """

        # now process asynchronous controller events in the urwid main thread,
        # thereby eleminating the need for complicated locking.
        self.m_controller.processEvents()

        # this indicates to urwid to continue handling the pipe events
        return True

    def _getUserColor(self, user):
        """Returns an urwid color name to be used for the given username
        throughout the runtime of the application.

        Each newly encountered user will receive another color, thereby
        allowing to differentiate different users in a best effort fashion.
        """

        try:
            # check whether we already assigned a color
            return self.m_user_colors[user]
        except KeyError:
            pass

        user_colors = (
            # 'black',
            'dark red',
            'dark green',
            'brown',
            'dark blue',
            'dark magenta',
            'dark cyan',
            'light gray',
            'dark gray',
            'light red',
            'light green',
            'yellow',
            'light blue',
            'light magenta',
            'light cyan',
            'white'
        )
        next_color = user_colors[len(self.m_user_colors) % len(user_colors)]
        self.m_user_colors[user] = next_color

        return next_color

    def _updateMainHeading(self):
        our_status = self.m_controller.getUserStatus(
                self.m_controller.getLoggedInUserInfo()
        )

        parts = []

        parts.append((
            'border',
            "Rocket.term {}@{} ({}, {}) ".format(
                self.m_comm.getUsername(),
                self.m_comm.getServerURI().getServerName(),
                self.m_comm.getFullName(), self.m_comm.getEmail(),
            )
        ))

        parts.append((
            urwid.AttrSpec(self._getUserStatusColor(our_status[0]), 'white'),
            "[{}]\n".format(our_status[0].value)
        ))

        parts.append((
            'border',
            "Status Message: {}".format(
                our_status[1] if our_status[1] else "<no status message>"
            )
        ))

        text = urwid.Text(parts, align='center')
        header = urwid.AttrMap(text, 'border')
        self.m_frame.set_header(header)

    def _commandEntered(self, command):
        """Called by the CommandInput widget when a command line has been
        entered."""
        try:
            response = self.m_cmd_parser.commandEntered(command)
            if not response:
                return
            self._setStatusMessage(response)
        except rocketterm.parser.ParseError as e:
            self._setStatusMessage(e.getMessage())

    def _completeCommand(self, prefix):
        """Called by the CommandInput widget when a command line should be
        completed."""
        try:
            candidates, new_line = self.m_cmd_parser.completeCommand(prefix)
        except Exception as e:
            self._setStatusMessage("completion failed with: {}".format(str(e)))
            return prefix

        if len(candidates) > 1:
            self._setStatusMessage(' '.join(candidates))
        else:
            self._setStatusMessage('')

        if new_line != prefix:
            return new_line

    def _handleInput(self, k):
        """Handles any input that is not handled by urwid itself.

        In our case this means any input that is not consumed by our only
        focused widget, the CommandInput widget. We use this to implement
        special control keys for scrolling the room box, the chat box and
        things like that.
        """
        self.m_logger.debug("User input received: {}".format(k))
        self._clearStatusMessages()

        if k == 'meta q':
            raise urwid.ExitMainLoop()
        elif k == 'meta up':
            self.m_controller.selectPrevRoom()
        elif k == 'meta down':
            self.m_controller.selectNextRoom()
        elif k == 'page up':
            self._scrollMessages(ScrollDirection.OLDER)
        elif k == 'page down':
            self._scrollMessages(ScrollDirection.NEWER)
        elif k == "meta page up":
            self._scrollMessages(ScrollDirection.OLDER, True)
        elif k == "meta page down":
            self._scrollMessages(ScrollDirection.NEWER, True)
        elif k == 'meta end':
            self._scrollMessages(ScrollDirection.NEWEST)
        elif k == 'meta home':
            self._scrollMessages(ScrollDirection.OLDEST)
        elif k == 'shift up':
            self._selectActiveRoom(Direction.PREV)
        elif k == 'shift down':
            self._selectActiveRoom(Direction.NEXT)
        else:
            self.m_logger.debug("Input unhandled")

    def _refreshRoomState(self, room):
        # XXX consider moving this state handling into the controller

        if room == self.m_current_room:
            # reset any special state if this room is currently
            # selected
            self.m_room_states[room.getID()] = RoomState.NORMAL
        else:
            # populate an initial normal state
            self.m_room_states.setdefault(room.getID(), RoomState.NORMAL)

    def _getRoomColor(self, room):
        room_state_colors = {
            RoomState.NORMAL: "text",
            RoomState.ATTENTION: "attention_text",
            RoomState.ACTIVITY: "activity_text"
        }

        if room == self.m_current_room:
            return 'selected_text'

        state = self.m_room_states.get(room.getID())

        return room_state_colors[state]

    def _getUserStatusColor(self, status):
        UserPresence = rocketterm.types.UserPresence

        status_colors = {
            UserPresence.Online: 'dark green',
            UserPresence.Offline: 'white',
            UserPresence.Busy: 'light red',
            UserPresence.Away: 'yellow'
        }

        return status_colors[status]

    def _getRoomPrefixColor(self, room):
        if not room.isDirectChat():
            return 'selected_text' if room == self.m_current_room else 'text'

        logged_in_user = self.m_controller.getLoggedInUserInfo()
        peer_uid = room.getPeerUserID(logged_in_user)
        peer_user = self.m_controller.getBasicUserInfoByID(peer_uid)
        status, _ = self.m_controller.getUserStatus(peer_user)
        color = self._getUserStatusColor(status)

        return urwid.AttrSpec(color, 'black')

    def _roomBoxResized(self, widget):
        self._updateRoomBox()

    def _updateRoomBox(self):
        """Rebuilds the room box from current status information."""

        self.m_room_box.body.clear()
        max_width = self.m_room_box.getNumCols()

        for room in self.m_controller.getJoinedRooms():
            name = room.getLabel()
            self._refreshRoomState(room)
            name_attr = self._getRoomColor(room)
            prefix_attr = self._getRoomPrefixColor(room)
            # truncate room names to avoid line breaks in list
            # items
            truncated_name = name[1:max_width - 2]
            parts = []
            parts.append((prefix_attr, name[0]))
            name = (name_attr, truncated_name)
            parts.append(name)
            self.m_room_box.body.append(urwid.Text(parts))

    def _chatBoxResized(self, widget):
        self._updateChatBox()

    def _updateChatBox(self):
        """Rebuilds the chat box from current status information."""

        self.m_chat_box.body.clear()
        self.m_oldest_chat_msg = None
        self.m_newest_chat_msg = None
        self.m_num_chat_msgs = 0
        # maps msg IDs to consecutive msg nr#
        self.m_msg_nr_map = {}
        # maps consecutive msg nr# to msg IDs
        self.m_msg_id_map = {}
        # maps message nrs. to listbox rows
        self.m_msg_nr_row_map = {}
        # offset to add to values stored in m_msg_nr_row_map,
        # see _recordMsgNr() for a detailed explanation
        self.m_row_offset = 0
        self.m_row_index_bottom = 0
        self.m_row_index_top = -1
        # a mapping of thread parent msg IDs to a list of consecutive
        # numbers of chat messages waiting for the parent message to
        # be resolved
        self.m_waiting_for_thread_parent = {}
        messages = self.m_controller.getCachedRoomMessages()
        self.m_room_msg_count = self.m_controller.getRoomMsgCount()

        self.m_logger.debug(
            "Updating chat box, currently cached: {}, complete count: {}".format(
                len(messages),
                self.m_room_msg_count
            )
        )

        self.m_chat_frame.contents["header"] = self._getRoomHeading()

        if not self.m_room_msg_count:
            return

        for nr, msg in enumerate(messages):
            self._addChatMessage(msg, at_end=False)

        self._resolveMessageReferences()
        self._scrollMessages(ScrollDirection.NEWEST)

        self.m_logger.debug(
                "Number of messages waiting for thread parent: {}".format(
                    len(self.m_waiting_for_thread_parent)
                )
        )

    def _resolveMessageReferences(self):
        # To resolve message references lazily we need to check whether any
        # unresolved messages remain when we load more messages on demand.
        #
        # If this is the case, load more chat history until no
        # unresolved messages remain. In the worst case this could mean we
        # need to load the complete chat history ... an alternative would be
        # to only resolve thread IDs once we actually display unknown threads
        # ... but the chat_box is not really helping much in getting to know
        # which messages are *actually* currently displayed.

        extra_msgs = 0

        while self.m_waiting_for_thread_parent:
            new_msgs = self._loadMoreChatHistory()

            if new_msgs == 0:
                self.m_logger.warning("Failed to resolve some thread messages")
                for parent, childs in self.m_waiting_for_thread_parent.items():
                    self.m_logger.warning("Waiting for #{}: {}".format(
                        parent, ', '.join(['#' + str(cid) for cid in childs])
                    ))
                break

            extra_msgs += new_msgs

        return extra_msgs

    def _getRoomHeading(self):

        room = self.m_current_room

        if not room:
            text = "no room available"
        elif room.isDirectChat():
            # display the friendly peer user name and user status
            our_info = self.m_controller.getLoggedInUserInfo()
            user_id = room.getPeerUserID(our_info)
            info = self.m_controller.getBasicUserInfoByID(user_id)
            if info:
                status = self.m_controller.getUserStatus(info)
                text = "Direct Chat with {} [{}: {}]".format(
                    info.getFriendlyName(),
                    status[0].value,
                    status[1] if status[1] else "<no status message>"
                )
            else:
                self.m_logger.warning("Failed to determine user for direct chat {}".format(room.getName()))
                text = "unknown user"
        elif room.supportsTopic():
            text = ""
            if room.isPrivateChat() and room.isDiscussion():
                parent_id = room.getDiscussionParentRoomID()
                parent = self.m_controller.getRoomInfoByID(parent_id)
                text += "This discussion belongs to room {}{}\n".format(
                    parent.typePrefix(), parent.getName()
                )
            text += self.m_current_room.getTopic()
            if room.supportsMembers():
                user_count = self.m_controller.getRoomUserCount(room)
                text += " ({} users)".format(user_count)
        else:
            # remove the heading
            return (None, None)

        return (urwid.AttrMap(urwid.Text(text, align='center'), 'topic_bar'), None)

    def _loadMoreChatHistory(self):
        """Attempts to fetch more chat history for the currently selected chat
        room.

        For each newly loaded chat message _addChatMessage() is invoked.

        :return int: Number of message that could be additionally loaded.
        """
        more_msgs = self.m_controller.loadMoreRoomMessages()

        for nr, msg in enumerate(more_msgs):
            self._addChatMessage(msg, at_end=False)

        return len(more_msgs)

    def _getThreadLabel(self, msg, consecutive_nr):
        max_width = self._getMaxMsgNrWidth()
        parent = msg.getThreadParent()

        if not parent:
            # three extra characters for the two spaces and the '#'
            return (max_width + 3) * ' '

        nr_list = self.m_msg_nr_map.get(parent, [])
        if not nr_list:
            # we don't know the thread parent yet ... fill in a placeholder
            # that we can later replace when we encounter the thread parent
            # message
            waiters = self.m_waiting_for_thread_parent.setdefault(parent, [])
            waiters.append(consecutive_nr)
            # use a marker that we can later
            # replace when we know the thread nr.
            parent_nr = '?' * max_width
        else:
            parent_nr = nr_list[0]

        return " #{} ".format(str(parent_nr).rjust(max_width))

    def _getMessageText(self, msg, consecutive_nr):
        """Transforms the message's text into a sensible message, if
        it is a special message type.

        This function handles various special situations and returns text that
        tries to be helpful to the user.
        """

        _type = msg.getMessageType()
        MessageType = rocketterm.types.MessageType
        raw_message = msg.getMessage()
        room = self.m_controller.getSelectedRoom()

        if _type == MessageType.RegularMessage:
            text = raw_message
            # NOTE: regular messages can have empty text but a 'file' attachment.

            if msg.isIncrementalUpdate():
                nrs = self.m_msg_nr_map.get(msg.getID())
                if nrs:
                    label = "[#{}]".format(nrs[0])
                else:
                    max_width = self._getMaxMsgNrWidth()
                    label = "[#{}]".format('?' * max_width)
                    waiters = self.m_waiting_for_thread_parent.setdefault(msg.getID(), [])
                    waiters.append(consecutive_nr)
                text = "{}: {}".format(label, msg.getMessage())

            else:
                # this is no special message type, just a RegularMessage with
                # an attribute
                if msg.wasEdited():
                    text = rocketterm.utils.getMessageEditContext(msg)

                for reaction, info in msg.getReactions().items():
                    prefixed_users = [rocketterm.types.BasicUserInfo.typePrefix() + user for user in info['usernames']]
                    text += "\n[reacted with {}]: {}".format(
                        reaction, ', '.join(prefixed_users)
                    )

                if msg.hasFile():
                    fi = msg.getFile()
                    attach_prefix = "[file attachment: {} ({})]".format(
                        fi.getName(),
                        fi.getMIMEType()
                    )

                    if text:
                        attach_prefix += ": "

                    text = attach_prefix + (text if text else "")

            return text
        elif _type in (MessageType.UserLeft, MessageType.UserJoined):
            uinfo = msg.getUserInfo()
            who = "{} ({})".format(uinfo.getFriendlyName(), uinfo.getUsername())
            if _type == MessageType.UserLeft:
                event = "has left the {}".format(room.typeLabel())
            else:
                event = "has joined the {}".format(room.typeLabel())
            return "[{} {}]".format(who, event)
        elif _type in (MessageType.UserAddedBy, MessageType.UserRemovedBy):
            uinfo = msg.getUserInfo()
            actor = uinfo.getFriendlyName()
            victim = raw_message
            place = self.m_current_room.typeLabel()
            if _type == MessageType.UserAddedBy:
                event = "{} has added user '{}' to this {}".format(actor, victim, place)
            else:
                event = "{} has removed user '{}' from this {}".format(actor, victim, place)

            return "[{}]".format(event)
        elif _type in (
                MessageType.RoomChangedTopic,
                MessageType.RoomChangedDescription,
                MessageType.RoomChangedAnnouncement
        ):
            if _type == MessageType.RoomChangedTopic:
                prefix = "Topic"
            elif _type == MessageType.RoomChangedDescription:
                prefix = "Description"
            else:
                prefix = "Announcement"

            return "[{} of the {} changed]: {}".format(
                prefix, room.typeLabel(), raw_message
            )
        elif _type in (MessageType.MessageRemoved,):
            uinfo = msg.getUserInfo()
            actor = uinfo.getFriendlyName()
            event = "{} has removed this message".format(actor)

            return "[{}]".format(event)
        elif _type == MessageType.DiscussionCreated:
            uinfo = msg.getUserInfo()
            actor = uinfo.getFriendlyName()
            event = "{} has created discussion {}{}".format(
                actor, rocketterm.types.PrivateChat.typePrefix(), raw_message
            )

            return "[{}]".format(event)
        else:
            return "unsupported special message type {}: {}".format(str(_type), raw_message)

    def _recordMsgNr(self, nr, msg, at_end):
        # we need to keep a list here, because with message editing
        # the same message ID can get multiple consecutive nrs#
        nr_list = self.m_msg_nr_map.setdefault(msg.getID(), [])
        if not msg.isIncrementalUpdate():
            nr_list.append(nr)
        self.m_msg_id_map[nr] = msg.getID()

        # the business of keeping track of into which row a msg nr# went is
        # quite complicated. Since we have a linear list and we start out with
        # only a part of the history and we don't know how many rows there
        # will be in the end, because there will be date bars added or dynamic
        # message updates during runtime, we can't use random access with
        # indices to find the correct row for a message.

        # using a simple map of msg nr# to row nr also doesn't work since we
        # append and prepend messages from both ends of the list, thus the
        # indices don't stay the same over time.

        # to deal with this we use a bit of complicated bookeeping here:
        # - row_index_bottom is the next index value to assign to newly added
        #   rows at the bottom
        # - row_index_top is the next index value to assign to newly prepended
        #   rows at the top of the list (thinking of the chat box top/bottom,
        #   not the list start/end here).
        # - each time we prepend a message, we need to increment the
        #   row_offset.
        #
        # Now to find the correct row nr. we need to lookup the index stored
        # in the map and add the current offset to it.

        if at_end:
            self.m_msg_nr_row_map[nr] = self.m_row_index_bottom
        else:
            self.m_msg_nr_row_map[nr] = self.m_row_index_top

    def _addedToChatBoxTopRow(self, num):
        self.m_row_index_top -= num
        self.m_row_offset += num

    def _addedToChatBoxBottomRow(self, num):
        self.m_row_index_bottom += num

    def _getMsgRowNr(self, nr):
        base_nr = self.m_msg_nr_row_map[nr]

        return base_nr + self.m_row_offset

    def _checkUpdateThreadChilds(self, thread_nr, thread_msg):
        if thread_msg.isIncrementalUpdate():
            return
        waiters = self.m_waiting_for_thread_parent.pop(thread_msg.getID(), [])

        for waiter_consecutive_nr in waiters:
            self._updateThreadChildMessage(thread_nr, waiter_consecutive_nr)

    def _updateThreadChildMessage(self, thread_nr, child_nr):
        """Replaces the placeholder added in _getThreadLabel() earlier by the
        actual thread ID that we now know."""
        child_index = self._getMsgRowNr(child_nr)
        row = self.m_chat_box.body[child_index]
        text = row.text

        if len(text) < 2 or not text.startswith("#"):
            self.m_logger.warning(
                "thread child msg has unexpected content: {}".format(text)
            )
            return

        try:
            msg_nr = int(text[1:].split(None, 1)[0])
            if msg_nr != child_nr:
                raise Exception("mismatched child #nr")
        except Exception:
            self.m_logger.warning(
                "couldn't verify child msg nr in: {}".format(text)
            )
            return

        max_width = self._getMaxMsgNrWidth()

        # okay we now know that this is the message we're looking for.
        replace_str = '#{}'.format(str(thread_nr).rjust(max_width))
        needle = '#{}'.format('?' * (len(replace_str) - 1))

        new_text = text.replace(needle, replace_str, 1)

        if new_text == text:
            self.m_logger.warning(
                "couldn't replace placeholder in child msg: {}".format(text)
            )

        # this messes with the internals of urwid.Text, but
        # there's no good other way, we'd need to reconstruct
        # all the attributes for coloring etc.
        # we make sure that the length of the text message is
        # not changing, otherwise the markup would not be
        # correct any more
        row._text = new_text

    def _getMaxMsgNrWidth(self):
        return len(str(self.m_room_msg_count + 1))

    def _formatChatMessage(self, msg, nr):
        """Returns an urwid widget representing the fully formatted chat
        messsage.

        :param RoomMessage msg: The message to format.
        :param int nr: The consecutive msg nr. for the message.
        """

        nr_label = '#{} '.format(str(nr).rjust(self._getMaxMsgNrWidth()))
        timestamp = msg.getCreationTimestamp().strftime("%X")
        username = msg.getUserInfo().getUsername()
        userprefix = " {}: ".format(username.rjust(15))
        thread_id = self._getThreadLabel(msg, nr)

        prefix_len = len(nr_label) + len(timestamp) + len(userprefix) + len(thread_id)
        messagewidth = max(self.m_chat_box.getNumCols(), 80) - prefix_len - 1
        indentation = (' ' * prefix_len)

        # handle special message types by creating sensible text to
        # display, does nothing for normal messages
        msg_text = self._getMessageText(msg, nr)

        # the textwrap module is a bit difficult to tune ... we want
        # to maintain newlines from the original string, but enforce a
        # maximum line length while prefixing an indentation string to
        # each line starting from the second one.
        #
        # maintaining the original newlines works via
        # `replace_whitespace = False`, however then we don't get the lines
        # split up in the result, when there are newlines contained in the
        # original text. Therefore replace remaining newlines by the prefix
        # afterwards.

        lines = textwrap.wrap(
            msg_text,
            width=messagewidth,
            replace_whitespace=False
        )

        if len(lines) > 1:
            lines = lines[0:1] + [indentation + line for line in lines[1:]]

        lines = [line.replace('\n', '\n' + indentation) for line in lines]

        message = '\n'.join(lines)

        user_color = self._getUserColor(username)

        text = urwid.Text([
            ('text', nr_label),
            ('text', timestamp),
            ('text', thread_id),
            (urwid.AttrSpec(user_color, 'black'), userprefix),
            ('text', message)
        ])

        return text

    def _addChatMessage(self, msg, at_end):
        """Adds a chat message to the current chat box.

        :param int nr: The consecutive message nr to use, if known, otherwise
                       pass -1 and it is assumed that this is a new message
                       with at_end == True.
        :params RoomMessage msg: The message object to process.
        :params bool at_end: Whether the message is to be appended at the end,
                             or prepended at the beginning of the box.
        """
        if at_end:
            msg_nr = self.m_room_msg_count
        else:
            msg_nr = self.m_room_msg_count - self.m_num_chat_msgs

        self._maybeInsertDateBar(msg, at_end)

        # update not yet resolved thread child numbers we may now be able to
        # resolve with this new message
        self._checkUpdateThreadChilds(msg_nr, msg)
        # remember which consecutive number this message has in our chat box
        # so that we can reference it later on if threaded messages occur
        self._recordMsgNr(msg_nr, msg, at_end)

        self.m_num_chat_msgs += 1

        text = self._formatChatMessage(msg, msg_nr)

        self._addRowToChatBox(text, at_end)

        if msg_nr == 1:
            # make sure if the oldest message is loaded that a date message is
            # prepended in any case, so we know when the conversation in the
            # room started
            date_text = self._getDateText(msg.getCreationTimestamp().date())
            self._addRowToChatBox(date_text, at_end=False)

    def _maybeInsertDateBar(self, msg, at_end):
        """If with the addition of the given message a date bar needs to be
        prepended / appended, then this will be done."""

        if not self.m_oldest_chat_msg and not self.m_newest_chat_msg:
            self.m_oldest_chat_msg = msg
            self.m_newest_chat_msg = msg
            compare_ts = msg.getCreationTimestamp()
        elif at_end:  # newer message
            compare_ts = self.m_newest_chat_msg.getCreationTimestamp()
            self.m_newest_chat_msg = msg
        else:  # older message
            compare_ts = self.m_oldest_chat_msg.getCreationTimestamp()
            self.m_oldest_chat_msg = msg

        this_ts = msg.getCreationTimestamp()

        if compare_ts.date() != this_ts.date():
            src_date = this_ts.date() if at_end else compare_ts.date()
            # the date of this message changed compared to the previous / next
            # message, thus add a date information message.
            date_text = self._getDateText(src_date)

            self._addRowToChatBox(date_text, at_end)

    def _addRowToChatBox(self, text, at_end):
        """This finally adds fully formatted text to the chat box."""

        # we need to adjust our bookkeeping if e.g. a date bar was added
        if at_end:
            self._addedToChatBoxBottomRow(1)
            self.m_chat_box.body.append(text)
        else:
            self._addedToChatBoxTopRow(1)
            self.m_chat_box.body[0:0] = [text]

    def _getDateText(self, date):
        centered_date = date.strftime("%x").center(self.m_chat_box.getNumCols())
        return urwid.Text([('date_bar', centered_date)])

    def _getRows(self):
        """Returns the number of rows the terminal currently has."""
        # TODO: this would also need to react on terminal size changes ...
        return self.m_loop.screen.get_cols_rows()[1]

    def _selectActiveRoom(self, direction):
        """Tries to select a new room with activie in the room box in the
        given direction.

        :param Direction direction: The search direction.
        """

        prev_active_room = None
        select_next_active_room = False

        for room in self.m_controller.getJoinedRooms():
            if room == self.m_current_room:
                if direction == Direction.PREV:
                    if prev_active_room:
                        self.m_controller.selectRoom(prev_active_room)
                        return
                    else:
                        # no active room before the
                        # selected one
                        break
                else:
                    select_next_active_room = True
            elif self.m_room_states[room.getID()] == RoomState.NORMAL:
                continue
            elif select_next_active_room:
                self.m_controller.selectRoom(room)
                return
            else:
                prev_active_room = room

        self._setStatusMessage("no room with activity in this direction")

    def _scrollMessages(self, direction, small_increments=False):
        """Scroll chat box messages.

        :param ScrollDirection direction: The scroll type to apply.
        :param bool small_increments: Whether a large scroll step (like
                                      page-up/down) or small scroll increments
                                      should be performed.
        """
        if len(self.m_chat_box.body) == 0:
            return
        curpos = self.m_chat_box.focus_position

        self.m_logger.debug(
            "scroll request direction = {} curpos = {} (#{})".format(
                direction, curpos, self._getFocusedMessageNr()
            )
        )

        if direction == ScrollDirection.NEWER:
            self.m_chat_box.scrollDown(small_increments)
            return

        elif direction == ScrollDirection.OLDER:
            if curpos == 0:
                # remember at which message we are, because when we load
                # additional chat history then the focus positions in the chat
                # box body somehow are not reliable any more
                focused_msg = self._getFocusedMessageNr()
                new_msgs = self._loadMoreChatHistory()
                new_msgs += self._resolveMessageReferences()
                self.scrollToMessage(focused_msg)
            self.m_chat_box.scrollUp(small_increments)
            return
        elif direction == ScrollDirection.NEWEST:
            curpos = len(self.m_chat_box.body) - 1
        elif direction == ScrollDirection.OLDEST:
            curpos = 0

            # load complete chat history
            while self._loadMoreChatHistory() != 0:
                pass

        self.m_logger.debug("Scrolling to {}".format(curpos))
        self.m_chat_box.set_focus(curpos)

    def _setStatusMessage(self, msg):
        """Sets a new status message in the status box."""
        self._clearStatusMessages()
        text = urwid.Text(('text', msg))
        self.m_status_box.body.append(text)

    def _clearStatusMessages(self):
        self.m_status_box.body.clear()

    def _updateCmdInputPrompt(self):
        """Chooses an appropriate command input prompt for the current
        application status."""
        msg_id = self.m_controller.getSelectedThreadID()
        if msg_id:
            nrs = self.m_msg_nr_map[msg_id]
            self.m_cmd_input.addPrompt("[#{}]".format(nrs[0]))
        else:
            self.m_cmd_input.resetPrompt()

    def _getFocusedMessageNr(self):
        """Returns the msg nr# of the message currently in chat box focus.

        If no matching message was found then None is returned.
        """
        boxpos = self.m_chat_box.focus_position

        for pos in range(boxpos, len(self.m_chat_box.body)):
            row = self.m_chat_box.body[pos]
            text = row.text
            if not text.startswith('#'):
                continue
            parts = text.lstrip('#').split(None, 1)
            if len(parts) != 2:
                continue
            msgnr = parts[0]
            if not msgnr.isnumeric():
                continue

            return int(msgnr)

    def _getOldestLoadedMsgNr(self):
        return self.m_controller.getRoomMsgCount() - self.m_num_chat_msgs + 1

    def getMsgIDForNr(self, msg_nr):
        """Returns the msg ID for a consecutive msg nr#."""
        return self.m_msg_id_map[msg_nr]

    def getNrsForMsgID(self, msg_id):
        """Returns a list of consecutive msg nrs# for a msg ID."""
        return self.m_msg_nr_map[msg_id]

    def newRoomSelected(self):
        """Called by the Controller when a new room was selected."""
        self.m_current_room = self.m_controller.getSelectedRoom()
        # possible new thread selection
        self._updateRoomBox()
        self._updateChatBox()
        self._updateCmdInputPrompt()

    def asyncEventOccured(self):
        """Called by the Controller when an asynchronous event
        occured. We will wake up the urwid main loop to process the event
        in a synchronous fashion."""
        os.write(self.m_urwid_pipe, b"new async event")

    def ownUserStatusChanged(self, status_event):
        """Called by the Controller when our own user status changed."""
        # update the status displayed in the main heading
        self._updateMainHeading()

    def handleNewRoomMessage(self, msg):
        """Called by the Controller when in a new message appeared in one of
        our visible rooms."""

        room_id = msg.getRoomID()

        if room_id == self.m_current_room.getID():
            self.m_room_msg_count = self.m_controller.getRoomMsgCount()
            self._addChatMessage(msg, at_end=True)
            if msg.isIncrementalUpdate():
                # if this related to an older message then make sure we
                # resolve any unresolved references
                self._resolveMessageReferences()
                self._scrollMessages(ScrollDirection.NEWEST)
        else:
            if self.m_controller.doesMessageMentionUs(msg):
                new_state = RoomState.ATTENTION
            else:
                new_state = RoomState.ACTIVITY

            self.m_room_states[room_id] = new_state
            self._updateRoomBox()

    def roomChanged(self, room):
        """Called by the Controller when the state of one of our subscribed
        rooms changed."""

        self.m_logger.debug("room changed: {}".format(room.getName()))

        if room == self.m_controller.getSelectedRoom():
            self._updateChatBox()
            self._updateRoomBox()

    def roomAdded(self, room):
        """Called by the Controller when a new room was subscribed to.

        This can result from a user interaction or from an external event e.g.
        when another user writes in a direct chat to us that wasn't
        visible/existing before.
        """

        self.m_logger.debug("room added: {}".format(room.getName()))
        # handle the same as an opened room for now
        self.roomOpened(room)

    def roomRemoved(self, room):
        """Called by the Controller when a subscribed to room was removed."""

        self.m_logger.debug("room removed: {}".format(room.getName()))
        # handle the same as a hidden room for now
        self.roomHidden(room)

    def roomHidden(self, room):
        """Called by the Controller when a room was hidden by the user."""
        self.m_logger.debug("room hidden: {}".format(room.getName()))
        self.m_room_states.pop(room.getID())
        self._updateRoomBox()

    def roomOpened(self, room):
        """Called by the Controller when a room was opened by the user."""
        self.m_logger.debug("room opened: {}".format(room.getName()))
        self.m_room_states[room.getID()] = RoomState.ATTENTION
        self._updateRoomBox()

    def newDirectChatUserStatus(self, status_event):
        """Called by the Controller when the user status for the peers of one
        of our visible direct chats changed."""
        self.m_logger.debug("direct chat user status changed: {}: {}".format(
            status_event.getUsername(),
            status_event.getUserPresenceStatus().value)
        )
        self._updateRoomBox()

    def selectThread(self, thread_nr):
        """Selects a new default thread to participate in.

        :param int thread_nr: The consecutive msg nr# of the thread to select.
        """
        root_id = self.getMsgIDForNr(thread_nr)
        self.m_controller.selectThread(root_id)
        self._updateCmdInputPrompt()

    def leaveThread(self):
        """Leave a previously selected default thread."""
        self.m_controller.leaveThread()
        self.m_cmd_input.resetPrompt()

    def scrollToMessage(self, msg_nr):
        """Scrolls the current chat box to the given msg nr#.

        This may involve the need to load additional chat history and thus
        introduce load times.
        """
        existing_msgs = self.m_controller.getRoomMsgCount()

        if msg_nr <= 0 or msg_nr > existing_msgs:
            raise Exception("msg #nr out of range")

        while msg_nr < self._getOldestLoadedMsgNr():
            # load more chat history
            new_msgs = self._loadMoreChatHistory()
            new_msgs += self._resolveMessageReferences()

        row_nr = self._getMsgRowNr(msg_nr)

        self.m_chat_box.set_focus_valign("top")
        self.m_chat_box.set_focus(row_nr)

    def commandFeedback(self, msg):

        self._setStatusMessage(msg)
        self.m_loop.draw_screen()

    def loadHistoryStarted(self, room):
        """Called by the controller when time intensive room history loads are
        about to be started.

        This allows to give some kind of visual user feedback so it is clear
        what is going on.
        """

        if not self.m_loop_running:
            return

        total_msgs = self.m_controller.getRoomMsgCount(room)
        cur_msgs = len(self.m_controller.getCachedRoomMessages(room))

        feedback = "Loading more chat history from {} ({}/{})".format(
                room.getLabel(), cur_msgs, "?" if total_msgs == -1 else total_msgs
        )

        self._setStatusMessage(feedback)
        self.m_loop.draw_screen()

    def loadHistoryEnded(self, room):
        """Like loadHistoryStarted(), but after a chunk of history was
        received."""
        self.loadHistoryStarted(room)

    def loadUsersInProgress(self, so_far, total):
        """Called by the controller when time intensive user loads are in
        progress.

        This allows to give some kind of visual user feedback so it is clear
        what is going on.
        """
        if not self.m_loop_running:
            return
        feedback = "Loading user list from server ({}/{})".format(
            so_far, total
        )
        self._setStatusMessage(feedback)
        self.m_loop.draw_screen()

    def getChannelsInProgress(self, so_far, total):
        """Called by the controller when time intensive room list loads are in
        progress.
        """

        if not self.m_loop_running:
            return

        feedback = "Loading channel list from server ({}/{})".format(
                so_far, "?" if total == -1 else total
        )
        self._setStatusMessage(feedback)
        self.m_loop.draw_screen()

    def mainLoop(self):
        """The urwid main loop that processes UI and Controller events."""

        self.m_urwid_pipe = self.m_loop.watch_pipe(self._externalEvent)
        self.m_cmd_parser = rocketterm.parser.Parser(self.m_global_objects)

        self.m_controller.start(self._getRows())
        default_room = self.m_global_objects.config["default_room"]

        if default_room:
            if not self.m_controller.selectRoomBySpec(default_room):
                self.m_logger.warning("Could not find default_room {}".format(default_room))
                default_room = None

        if not default_room:
            self.m_controller.selectAnyRoom()

        self._updateMainHeading()

        try:
            self.m_loop_running = True
            self.m_loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.m_loop_running = False

        self.m_controller.stop()
        os.close(self.m_urwid_pipe)

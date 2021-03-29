# vim: ts=4 et sw=4 sts=4 :

import copy
import logging
import os
import pprint
from enum import Enum

# rocket.term
import rocketterm.controller
import rocketterm.parser
import rocketterm.types
import rocketterm.utils
from rocketterm.types import RoomState
from rocketterm.widgets import CommandInput, SizedListBox

# 3rd party
import urwid


ScrollDirection = Enum('ScrollDirection', "OLDER NEWER NEWEST OLDEST")
Direction = Enum('Direction', "PREV NEXT")
WidgetPosition = Enum('WidgetPosition', "LEFT RIGHT TOP BOTTOM")


class Screen:
    """The Screen class takes are of all user interface display logic.

    Screen interacts tightly with the Controller to perform its tasks. It uses
    urwid to manage the terminal screen.
    """

    # an urwid palette that allows to reuse common colors for similar UI items.
    DEFAULT_PALETTE = {
        'text':            ('white',            'black'),
        'selected_text':   ('white,standout',   'black'),
        'activity_text':   ('light magenta',    'black'),
        'attention_text':  ('light red',        'black'),
        'box':             ('black',            'black'),
        'bar':             ('light magenta',    'white'),
        'room_topic':      ('brown',            'dark green'),
        'date_bar':        ('white',            'dark gray'),
        'input':           ('white',            'black'),
        'link_id':         ('light green',      'black'),
        'file_id':         ('brown',            'black'),
        # thread IDs use dynamic foreground colors
        'thread_id':       ('',                 'black'),
        'user_online':     ('dark green',       'black'),
        'user_offline':    ('white',            'black'),
        'user_busy':       ('light red',        'black'),
        'user_away':       ('yellow',           'black'),
    }

    # default value for dynamic user and thread colors
    DYNAMIC_COLORS = (
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
    )

    DEFAULT_KEYMAP = {
        'quit': 'meta q',
        'next_room': 'meta down',
        'prev_room': 'meta up',
        'next_active_room': 'shift down',
        'prev_active_room': 'shift up',
        'single_step_history_older': 'meta page up',
        'single_step_history_newer': 'meta page down',
        'scroll_history_older': 'page up',
        'scroll_history_newer': 'page down',
        'scroll_history_newest': 'meta end',
        'scroll_history_oldest': 'meta home',
        'cmd_history_older': 'up',
        'cmd_history_newer': 'down'
    }

    def __init__(self, global_objects):
        """
        :param dict config: The preprocessed configuration data.
        :param comm: The comm instance to use to talk to the RC server.
        """
        self.m_logger = logging.getLogger("screen")
        self.m_global_objects = global_objects
        self.m_comm = global_objects.comm
        self.m_controller = global_objects.controller
        self.m_keymap = copy.copy(self.DEFAULT_KEYMAP)
        # this is the chat / command input area
        self.m_cmd_input = CommandInput(self._commandEntered, self._completeCommand, self.m_keymap)
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
        self.m_dynamic_user_colors = self.DYNAMIC_COLORS
        self.m_dynamic_thread_colors = self.DYNAMIC_COLORS
        self.m_loop_running = False
        self.m_palette = copy.copy(self.DEFAULT_PALETTE)
        self.m_roombox_pos = WidgetPosition.LEFT
        self.m_show_roombox = True

    def _applyConfig(self):
        config = self.m_global_objects.config

        for user, color in config["color.users"].items():
            self._cacheUserColor(user, color)
        self.m_palette.update(config["color.palette"])
        self.m_keymap.update(config["keys"])

        colors = config["color"]

        if "own_user" in colors:
            our_user = self.m_comm.getUsername()
            self._cacheUserColor(our_user, colors["own_user"])

        dynamic_users = colors["dynamic_users"]
        if dynamic_users:
            self.m_dynamic_user_colors = dynamic_users

        dynamic_threads = colors["dynamic_threads"]
        if dynamic_threads:
            self.m_dynamic_thread_colors = dynamic_threads

        if config["roombox_pos"] == "right":
            self.m_roombox_pos = WidgetPosition.RIGHT

        if not config["show_roombox"]:
            self.m_show_roombox = False

    def _getMainFrameColumns(self, show_roombox=True):

        chat_col = ('weight', 90, self.m_chat_frame)
        columns = [chat_col]

        if show_roombox:
            box_col = ('weight', 10, urwid.AttrMap(self.m_room_box, 'box'))

            pos = 0 if self.m_roombox_pos == WidgetPosition.LEFT else len(columns)
            columns.insert(pos, box_col)

        # columns for holding the room box and the chat frame 10/90 relation
        # regarding the width
        columns = urwid.Columns(columns, min_width=20, dividechars=1)
        return urwid.AttrMap(columns, 'bar')

    def _setupWidgets(self):

        # a frame that we use just for its header, which becomes a bar
        # displaying the room topic
        self.m_chat_frame = urwid.Frame(
            urwid.AttrMap(self.m_chat_box, 'box')
        )

        columns = self._getMainFrameColumns(show_roombox=self.m_show_roombox)

        # this will be the main outer frame, containing a heading bar as
        # header (will be generated dynamically in _updateMainHeading()),
        # the columns with room box and chat box as main content and a pile
        # with status box and input box as footer.
        self.m_frame = urwid.Frame(
            columns,
            footer=urwid.Pile([]),
            header=None,
            focus_part='footer'
        )

        footer_pile = self.m_frame.contents["footer"][0]
        footer_pile.contents.append((
            urwid.AttrMap(urwid.Text("Command Input", align='center'), 'bar'),
            footer_pile.options()
        ))
        footer_pile.contents.append((
            urwid.AttrMap(self.m_status_box, 'box'),
            footer_pile.options(height_type='given', height_amount=2)
        ))
        footer_pile.contents.append((
            urwid.AttrMap(self.m_cmd_input, 'input'),
            footer_pile.options()
        ))
        footer_pile.focus_position = len(footer_pile.contents) - 1

    def _getPalette(self):
        # urwid expects an iterable of tuples ('label', 'fg', 'bg', ...)
        #
        # so contstruct this from our dictionary
        return ((key, value[0], value[1]) for key, value in self.m_palette.items())

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

        user_colors = self.m_dynamic_user_colors

        next_color = user_colors[len(self.m_user_colors) % len(user_colors)]
        return self._cacheUserColor(user, next_color)

    def _cacheUserColor(self, user, color):
        bg = self.m_palette['text'][1]
        val = urwid.AttrSpec(color, bg)
        self.m_user_colors[user] = val
        return val

    def _getMsgNrColor(self, msg, nr):
        if msg.getNumReplies() == 0 or msg.isIncrementalUpdate():
            return 'text'
        else:
            return self._getThreadColor(nr)

    def _getThreadColor(self, thread_nr):
        """Returns an urwid color name to be used for the given thread
        number."""

        thread_colors = self.m_dynamic_thread_colors
        fg = thread_colors[thread_nr % len(thread_colors)]
        return urwid.AttrSpec(fg, self.m_palette['thread_id'][1])

    def _updateMainHeading(self):
        our_status = self.m_controller.getUserStatus(
            self.m_controller.getLoggedInUserInfo(),
            need_text=True
        )

        parts = []

        email = self.m_comm.getEmail()

        parts.append((
            'bar',
            "Rocket.term {}@{} ({}, {}) ".format(
                self.m_comm.getUsername(),
                self.m_comm.getServerURI().getServerName(),
                self.m_comm.getFullName(), email if email else "<unknown email>",
            )
        ))

        user_status_color = self._getUserStatusColor(our_status[0])
        user_status_color = user_status_color[0], self.m_palette["bar"][1]

        parts.append((
            urwid.AttrSpec(*user_status_color),
            "[{}]\n".format(our_status[0].value)
        ))

        parts.append((
            'bar',
            "Status Message: {}".format(
                our_status[1] if our_status[1] else "<no status message>"
            )
        ))

        text = urwid.Text(parts, align='center')
        header = urwid.AttrMap(text, 'bar')
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

        keymap = self.m_keymap

        if k == keymap['quit']:
            raise urwid.ExitMainLoop()

        try:
            if k == keymap['prev_room']:
                self.m_controller.selectPrevRoom()
            elif k == keymap['next_room']:
                self.m_controller.selectNextRoom()
            elif k == keymap['prev_active_room']:
                self._selectActiveRoom(Direction.PREV)
            elif k == keymap['next_active_room']:
                self._selectActiveRoom(Direction.NEXT)
            elif k == keymap['scroll_history_older']:
                self._scrollMessages(ScrollDirection.OLDER)
            elif k == keymap['scroll_history_newer']:
                self._scrollMessages(ScrollDirection.NEWER)
            elif k == keymap['single_step_history_older']:
                self._scrollMessages(ScrollDirection.OLDER, True)
            elif k == keymap['single_step_history_newer']:
                self._scrollMessages(ScrollDirection.NEWER, True)
            elif k == keymap['scroll_history_oldest']:
                self._scrollMessages(ScrollDirection.OLDEST)
            elif k == keymap['scroll_history_newest']:
                self._scrollMessages(ScrollDirection.NEWEST)
            else:
                self.m_logger.debug("Input unhandled")
        except Exception as e:
            import traceback
            et = traceback.format_exc()
            self.m_logger.error("Input processing failed: {}\n{}\n".format(str(e), et))
            self.internalError("input handling failed: " + str(e))

    def _refreshRoomState(self, room):
        # XXX consider moving this state handling into the controller

        if room == self.m_current_room:
            # reset any special state if this room is currently
            # selected
            self.m_room_states[room.getID()] = RoomState.NORMAL
        else:
            # populate an initial state

            if room.getSubscription().hasUnreadMessages():
                state = RoomState.ATTENTION
            else:
                state = RoomState.NORMAL

            self.m_room_states.setdefault(room.getID(), state)

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
        return self.m_palette[f"user_{status.value}"]

    def _getRoomPrefixColor(self, room):
        if not room.isDirectChat():
            return 'selected_text' if room == self.m_current_room else 'text'

        logged_in_user = self.m_controller.getLoggedInUserInfo()
        peer_uid = room.getPeerUserID(logged_in_user)
        peer_user = self.m_controller.getBasicUserInfoByID(peer_uid)
        status, _ = self.m_controller.getUserStatus(peer_user)

        color = self._getUserStatusColor(status)
        color = color[0], self.m_palette["box"][1]
        return urwid.AttrSpec(*color)

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
            # truncate room names to avoid line breaks in list items
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
        # this contains all URLs posted in the room, for being able to open
        # them on user request
        self.m_url_list = []
        # this maps the URL text to the URL index in m_url_list
        self.m_url_map = {}
        # this contains all file attachments posted in the room, for being
        # able to open them on user request
        self.m_file_list = []
        # this maps a file attachment ID to the file index in m_file_list
        self.m_file_map = {}
        # offset to add to values stored in m_msg_nr_row_map,
        # see _recordMsgNr() for a detailed explanation
        self.m_row_offset = 0
        self.m_row_index_bottom = 0
        self.m_row_index_top = -1
        # a mapping of target msg IDs to a list of consecutive numbers of chat
        # messages waiting for them to be resolved
        self.m_waiting_for_msg_refs = {}
        messages = self.m_controller.getCachedRoomMessages()
        self.m_room_msg_count = self.m_controller.getRoomMsgCount()

        self.m_logger.debug(
            "Updating chat box, currently cached: {}, complete count: {}".format(
                len(messages),
                self.m_room_msg_count
            )
        )

        self._updateRoomHeading()

        if not self.m_room_msg_count:
            return

        for nr, msg in enumerate(messages):
            self._addChatMessageSafe(msg, at_end=False)
            if nr != 0 and (nr % 512) == 0:
                self._setStatusMessage(
                    "Processing messages from {} ({}/{})".format(
                        self.m_current_room.getLabel(),
                        nr + 1,
                        len(messages)
                    ),
                    redraw=True
                )

        self._resolveMessageReferences()
        self._scrollMessages(ScrollDirection.NEWEST)

        self.m_logger.debug(
            "Number of messages waiting for message references: {}".format(
                len(self.m_waiting_for_msg_refs)
            )
        )

        self._clearStatusMessages()

    def _updateRoomHeading(self):
        self.m_chat_frame.contents["header"] = self._getRoomHeading()

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

        while self.m_waiting_for_msg_refs:
            new_msgs = self._loadMoreChatHistory()

            if new_msgs == 0:
                # this can happen with some strange inconsistencies on the
                # server end, where messages are references that don't exist
                # any more for some reason. even deleted messages are normally
                # still existing for reference.
                self.m_logger.warning("Failed to resolve some message references")
                for target, waiters in self.m_waiting_for_msg_refs.items():
                    self.m_logger.warning("Waiting for #{}: {}".format(
                        target, ', '.join(['#' + str(waiter[0]) for waiter in waiters])
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
                status = self.m_controller.getUserStatus(info, need_text=True)
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

        return (urwid.AttrMap(urwid.Text(text, align='center'), 'room_topic'), None)

    def _loadMoreChatHistory(self):
        """Attempts to fetch more chat history for the currently selected chat
        room.

        For each newly loaded chat message _addChatMessageSafe() is invoked.

        :return int: Number of message that could be additionally loaded.
        """
        more_msgs = self.m_controller.loadMoreRoomMessages()

        for nr, msg in enumerate(more_msgs):
            self._addChatMessageSafe(msg, at_end=False)

        return len(more_msgs)

    def _getThreadLabel(self, msg, consecutive_nr):
        """Returns a tuple of (label_text, label_color)."""
        max_width = self._getMaxMsgNrWidth()

        parent = msg.getThreadParent()

        if not parent and msg.isIncrementalUpdate() and msg.getNumReplies() != 0:
            # it's an update to a thread message, reference ourselves then
            parent = msg.getID()
        elif not parent:
            # three extra characters for the two spaces and the '#'
            return (max_width + 3) * ' ', 'text'

        nr_list = self.m_msg_nr_map.get(parent, [])
        if not nr_list:
            # we don't know the thread parent yet ... fill in a placeholder
            # that we can later replace when we encounter the thread parent
            # message
            waiters = self.m_waiting_for_msg_refs.setdefault(parent, [])
            waiters.append((consecutive_nr, msg))
            # use a marker that we can later
            # replace when we know the thread nr.
            parent_nr = '?' * max_width
            color = 'text'
        else:
            parent_nr = nr_list[0]
            color = self._getThreadColor(parent_nr)

        return " #{} ".format(str(parent_nr).rjust(max_width)), color

    def _getUpdateMessagePrefix(self, msg, nr):
        # add the original message nr# as a prefix here and handle resolving
        # of yet unknown message IDs.
        nrs = self.m_msg_nr_map.get(msg.getID())
        if nrs:
            label = "[#{}]".format(nrs[0])
        else:
            max_width = self._getMaxMsgNrWidth()
            label = "[#{}]".format('?' * max_width)
            waiters = self.m_waiting_for_msg_refs.setdefault(msg.getID(), [])
            waiters.append((nr, msg))

        prefix = "{}: ".format(label)
        return prefix

    def _getUpdateText(self, new_msg, nr):
        """Calculate an incremental message update message when an existing chat
        message is altered.

        This happens e.g. when reactions are added to messages, new thread
        messages appear or message text is edited etc. Try to filter out
        useless updates, otherwise try to make clear what happened by changing
        message content.

        The handling is quite complex, because the data structures provided
        by stream-room-messages make it hard to understand what is going on,
        because no diffs are sent, only the complete new message, and
        sometimes intermediate states.
        """
        # we could also simply update the original message. this would be
        # easier on the implementation side. on the other hand this is kind of
        # a feature to see when happens what and what are the newest
        # modifications without having to scroll back. Although this data
        # cannot be reconstructed currently from server data after the events
        # are gone. So it is only ephemeral data and once the program is
        # restarted only a single message will appear anymore.
        assert new_msg.isIncrementalUpdate()

        prefix = self._getUpdateMessagePrefix(new_msg, nr)
        text = self._formatUpdateMessage(new_msg)

        return prefix + text

    def _formatUpdateMessage(self, new_msg):
        old_msg = new_msg.getOldMessage()
        MessageType = rocketterm.types.MessageType

        if not old_msg:
            return "update of uncached message -> unable to determine what changed"
        elif new_msg.getMessageType() != old_msg.getMessageType():
            if new_msg.getMessageType() == MessageType.MessageRemoved:
                return rocketterm.utils.getMessageRemoveContext(new_msg)
            else:
                self.m_logger.warning("unhandled message type change. old = {}, new = {}".format(
                    old_msg.getRaw(), new_msg.getRaw()
                ))
                return "unknown message type change"
        elif new_msg.getMessageType() == MessageType.DiscussionCreated:
            return "new messages in discussion '{}': now {} messages".format(
                    new_msg.getMessage(),
                    new_msg.getDiscussionCount()
            )
        elif new_msg.wasEdited() and new_msg.getEditTime() != old_msg.getEditTime():
            ret = rocketterm.utils.getMessageEditContext(new_msg)
            if old_msg.getURLs() != new_msg.getURLs():
                changed_urls = self._getChangedURLInfo(old_msg, new_msg)
                ret += "\n"
                ret += changed_urls
            return ret

        if old_msg.getReactions() != new_msg.getReactions():
            return self._getChangedReactionsText(old_msg, new_msg)
        elif old_msg.getStars() != new_msg.getStars():
            return self._getChangedStarsText(old_msg, new_msg)
        elif old_msg.getURLs() != new_msg.getURLs():
            return self._getChangedURLInfo(old_msg, new_msg)
        elif old_msg.getMessage() != new_msg.getMessage():
            return "[automatic message update]\n" + new_msg.getMessage()
        elif old_msg.getEditUser().getUsername() != new_msg.getEditUser().getUsername():
            return "[username changed to " + new_msg.getEditUser().getUsername() + "]"

        self.m_logger.warning(
            "unhandled message update.\nold = {}\nnew = {}".format(
                pprint.pformat(old_msg.getRaw()), pprint.pformat(new_msg.getRaw())
            )
        )

        return "unable to deduce what changed in this message update"

    def _getChangedStarsText(self, old_msg, new_msg):

        old_stars = old_msg.getStars()
        new_stars = new_msg.getStars()
        changes = []

        for uid in old_stars:
            if uid not in new_stars:
                info = self.m_controller.getBasicUserInfoByID(uid)
                changes.append(
                    "{} unstarred this message".format(info.getUsername())
                )

        for uid in new_stars:
            if uid not in old_stars:
                info = self.m_controller.getBasicUserInfoByID(uid)
                changes.append(
                    "{} starred this message".format(info.getUsername())
                )

        return '\n'.join(changes)

    def _getChangedURLInfo(self, old_msg, new_msg):

        changes = []

        old_urls = dict([(url.getURL(), url) for url in old_msg.getURLs()])
        new_urls = new_msg.getURLs()

        for new in new_urls:
            old = old_urls.get(new.getURL(), None)
            meta = new.getMeta()
            headers = new.getHeaders()

            if old and old.getMeta() == meta and old.getHeaders() == headers:
                # nothing changed
                continue

            changes += ["[updated information on {}]:".format(new.getURL())]

            url_nr = self._recordURL(new.getURL())
            urlinfo = self._getURLText(new, url_nr)
            changes.append(urlinfo)

        if not changes:
            changes.append("[unknown changes in URL information]")
            self.m_logger.warning("no change in {} vs.  {}?".format(old_msg.getRaw(), new_msg.getRaw()))

        return '\n'.join(changes)

    def _getChangedReactionsText(self, old_msg, new_msg):

        old_reactions = old_msg.getReactions()
        new_reactions = new_msg.getReactions()
        changes = []

        user_prefix = rocketterm.types.BasicUserInfo.typePrefix()

        for reaction, info in new_reactions.items():
            old_info = old_reactions.get(reaction, {'usernames': []})
            old_users = old_info.get('usernames')
            new_users = info['usernames']

            for user in new_users:
                if user not in old_users:
                    changes.append(
                        "{}{} reacted with {}".format(user_prefix, user, reaction)
                    )

        for reaction, info in old_reactions.items():
            new_info = new_reactions.get(reaction, {'usernames': []})
            new_users = new_info.get('usernames')
            old_users = info['usernames']

            for user in old_users:
                if user not in new_users:
                    changes.append(
                        "{}{} removed {} reaction".format(user_prefix, user, reaction)
                    )

        return '\n'.join(changes)

    def _getMessageText(self, msg):
        """Transforms the message's text into a sensible message, if it is a
        special message type.

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

            # this is no special message type, just a RegularMessage with
            # an attribute
            if msg.wasEdited():
                text = rocketterm.utils.getMessageEditContext(msg)

            for reaction, info in msg.getReactions().items():
                prefixed_users = [rocketterm.types.BasicUserInfo.typePrefix() + user for user in info['usernames']]
                text += "\n{} reacted with {}".format(
                    ', '.join(prefixed_users), reaction
                )

            for starrer in msg.getStars():
                info = self.m_controller.getBasicUserInfoByID(starrer)
                text += "\n[{} starred this message]".format(info.getLabel())

            if msg.hasFile():
                fi = msg.getFile()
                file_nr = self._recordFile(fi)
                desc = fi.getDescription()
                text += "\n[!{}]: file '{}' ({}){}".format(
                    file_nr,
                    fi.getName(),
                    fi.getMIMEType(),
                    ": {}".format(desc) if desc else ""
                )

            for url in msg.getURLs():
                url_nr = self._recordURL(url.getURL())
                text += "\n"
                text += self._getURLText(url, url_nr)

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
                MessageType.RoomChangedAnnouncement,
                MessageType.RoomChangedAvatar,
        ):
            if _type == MessageType.RoomChangedTopic:
                prefix = "Topic"
            elif _type == MessageType.RoomChangedDescription:
                prefix = "Description"
            elif _type == MessageType.RoomChangedAnnouncement:
                prefix = "Announcement"
            else:
                prefix = "Avatar"

            return "[{} of the {} changed]: {}".format(
                prefix, room.typeLabel(), raw_message
            )
        elif _type in (MessageType.MessageRemoved,):
            text = rocketterm.utils.getMessageRemoveContext(msg)
            return text
        elif _type == MessageType.DiscussionCreated:
            uinfo = msg.getUserInfo()
            actor = uinfo.getFriendlyName()
            event = "{} has created discussion {}{}".format(
                actor, rocketterm.types.PrivateChat.typePrefix(), raw_message
            )

            return "[{}]".format(event)
        elif _type == MessageType.MessagePinned:
            uinfo = msg.getUserInfo()
            actor = uinfo.getFriendlyName()
            info = msg.getPinnedMessageInfo()

            if info:
                event = "{} has pinned message from {} on {}: {}".format(
                    actor,
                    info.getAuthorName(),
                    info.getPinningTime().strftime("%X %x"),
                    info.getPinnedText()
                )
            else:
                event = "{} has pinned a message (don't know which one)".format(actor)

            return "[{}]".format(event)
        else:
            return "unsupported special message type {}: {}".format(str(_type), raw_message)

    def _getURLText(self, url, index):
        meta = url.getMeta()
        headers = url.getHeaders()

        url_prefix = "[{}]: ".format(index)
        indent = len(url_prefix) * ' '
        link = url_prefix + url.getURL()

        if not meta:
            return link

        lines = [link]

        title = meta.getTitle().strip()
        description = meta.getDescription().strip()
        oembed_type = meta.getOEmbedType()

        content_type = headers.getContentType() if headers else None
        if content_type is None:
            content_type = ""

        # don't show header information for HTML website documents, it's too noisy
        if content_type and not content_type.startswith("text/html"):
            _length = headers.getContentLength()
            lines += ["type: {}".format(content_type)]
            if _length:
                kb = int(int(_length) / 1024.0)
                if kb > 0:
                    lines += ["length: {} kb".format(str(kb))]

        if title:
            lines += ["{}# {}".format(indent, title)]
            if description:
                lines += ["{}{}".format(indent, description)]

        if oembed_type == "video":
            lines += ["[contains video preview]"]
        elif oembed_type == "photo":
            lines += ["[contains photo preview]"]
        elif oembed_type in ("rich", "link"):
            author = meta.getOEmbedAuthorName()
            title = meta.getOEmbedTitle()
            html = meta.getOEmbedHTML()
            if author:
                lines += ["author: {}".format(author)]
            if title:
                lines += ["# {}".format(title)]
            if html:
                text = rocketterm.utils.convertHTMLToText(html)
                lines += [text.strip()]
        elif oembed_type:
            lines += ["[unknown oembed type {} encountered]".format(oembed_type)]

        return '\n'.join(lines)

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

    def _checkUpdateMsgReferences(self, msg_nr, msg):
        if msg.isIncrementalUpdate():
            return
        waiters = self.m_waiting_for_msg_refs.pop(msg.getID(), [])

        for waiter_nr, waiter_msg in waiters:
            self._updateMsgReferences(waiter_nr, waiter_msg)

    def _updateMsgReferences(self, source_nr, source_msg):
        """Replaces any pending placeholders added earlier by the actual
        message IDs that we now know."""
        source_index = self._getMsgRowNr(source_nr)
        row = self.m_chat_box.body[source_index]
        text = row.text

        if len(text) < 2 or not text.startswith("#"):
            self.m_logger.warning(
                "thread child msg has unexpected content: {}".format(text)
            )
            return

        try:
            msg_nr = int(text[1:].split(None, 1)[0])
            if msg_nr != source_nr:
                raise Exception("mismatched ref #nr")
        except Exception as e:
            self.m_logger.warning(
                    "couldn't verify child msg nr in: {}: {}".format(text, str(e))
            )
            return

        # format the message again, resolving any unresolved msg# references
        # in the process
        new_text = self._formatChatMessage(source_msg, source_nr)
        self.m_chat_box.body[source_index] = new_text

    def _getMaxMsgNrWidth(self):
        return len(str(self.m_room_msg_count))

    def _formatChatMessage(self, msg, nr):
        """Returns an urwid widget representing the fully formatted chat
        message.

        :param RoomMessage msg: The message to format.
        :param int nr: The consecutive msg nr# for the message.
        """

        nr_label = '#{} '.format(str(nr).rjust(self._getMaxMsgNrWidth()))
        timestamp = msg.getCreationTimestamp().strftime("%X")
        username = msg.getUserInfo().getUsername()
        userprefix = " {}: ".format(username.rjust(15))
        thread_id, thread_color = self._getThreadLabel(msg, nr)

        prefix_len = len(nr_label) + len(timestamp) + len(userprefix) + len(thread_id)
        messagewidth = max(self.m_chat_box.getNumCols(), 80) - prefix_len - 1

        if msg.isIncrementalUpdate():
            msg_text = self._getUpdateText(msg, nr)
        else:
            # handle special message types by creating sensible text to display
            msg_text = self._getMessageText(msg)

        wrapped_text = rocketterm.utils.wrapText(msg_text, messagewidth, prefix_len)

        user_color = self._getUserColor(username)
        parent_color = self._getMsgNrColor(msg, nr)

        text = urwid.Text(
            [
                (parent_color, nr_label),
                ('text', timestamp),
                (thread_color, thread_id),
                (user_color, userprefix),
            ] + self._getHighlightedTextParts(wrapped_text)
        )

        return text

    def _getHighlightedTextParts(self, text):
        """Parses the given string for elements to highlight and returns a
        list of tuples mkaing up the urwid text elements for display.

        This function parses text elements like usernames and emojis and
        highlights them with suitable colors. Returned is a list of tuples of
        (urwid attribute, text) that represents the highlighted text.
        """
        word_seps = ' \t\n'

        class ParseContext:
            elements = []
            cur_element = ""
            cur_word = ""
        context = ParseContext()

        def addCurElement(context):
            if context.cur_element:
                context.elements.append(('text', context.cur_element))
                context.cur_element = ""

        def handleWord(context):
            highlight = self._getHighlightedWord(context.cur_word)
            if highlight:
                highlight, rest = highlight
                addCurElement(context)
                context.elements.append(highlight)
                context.cur_element += rest
            else:
                context.cur_element += context.cur_word

        for ch in text:

            if ch in word_seps:
                handleWord(context)
                context.cur_word = ""
                context.cur_element += ch
            else:
                context.cur_word += ch

        handleWord(context)
        addCurElement(context)

        return context.elements

    def _getHighlightedWord(self, word):
        """Returns colored text if the given message word should be
        highlighted.

        If the given word should not be highlighted then None is returned.
        Otherwise a tuple of ((color, text), str) is returned. The first tuple
        element is itself a tuple suitable to add it to an urwid.Text widget.
        The second element contains any remaining text from word that should
        be treated as normal text.
        """

        length = len(word)

        # check for @username mentionings
        if length > 1 and word.startswith('@'):
            # remove any suffix characters that aren't part of the username
            rest = ""
            while word and not word[-1].isalnum():
                rest += word[-1]
                word = word[:-1]
            # actively querying uncached usernames here is heavily slowing
            # down application responsiveness ... therefore simply treat all
            # valid @<words> as valid usernames. This could mean we also color
            # non-users but its still way cheaper this way.

            # info = self.m_controller.getBasicUserInfoByName(word[1:], only_cached = True)
            # if not info:
            #     return None
            attr = self._getUserColor(word[1:])
            return (attr, word), rest
        # check for :reactions:
        elif length > 2 and word.startswith(':') and word[2:].find(':') != -1:
            # remove any suffix characters that aren't part of the reaction
            end = word.find(':', 1)
            if end <= 1:
                return None
            rest = word[end+1:]
            word = word[:end+1]
            emoji = word[1:-1]
            # make sure we have custom emoji data available
            self.m_controller.fetchCustomEmojiData()
            from rocketterm.emojis import ALL_EMOJIES
            if emoji in ALL_EMOJIES:
                return ("activity_text", word), rest
        # check for [!<num>]: file attachments
        elif length > 4 and word.startswith('[!') and word.endswith(']:'):
            filenum = word[2:-2]
            if not filenum.isnumeric():
                return None

            return ("file_id", word), ""
        # check for [<num>]: links
        elif length > 3 and word.startswith('[') and word.endswith(']:'):
            linknum = word[1:-2]
            if not linknum.isnumeric():
                return None

            return ("link_id", word), ""

        return None

    def _addChatMessageSafe(self, *args, **kwargs):
        """Calls _addChatMessage with exception safety.

        When an exception is thrown while adding chat messages then there is a
        risk that the complete room is broken. Try to avoid that by catching
        exceptions and adding an "error chat message".
        """

        try:
            self._addChatMessage(*args, **kwargs)
        except Exception as e:
            errmsg = urwid.Text([('text', f"internal error processing this message: {e}")])
            self._addRowToChatBox(errmsg, at_end=True)
            import traceback
            et = traceback.format_exc()
            self.m_logger.error(f"Message processing failed: {e}\n{et}\n")
            self.internalError(f"Message processing failed: {e}")

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

        # remember which consecutive number this message has in our chat box
        # so that we can reference it later on if threaded messages occur
        self._recordMsgNr(msg_nr, msg, at_end)
        self.m_num_chat_msgs += 1

        # update not yet resolved thread child numbers we may now be able to
        # resolve with this new message
        self._checkUpdateMsgReferences(msg_nr, msg)

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
            compare_ts = msg.getCreationTimestamp()
            self.m_oldest_chat_msg = msg
            self.m_newest_chat_msg = msg
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

    def _setStatusMessage(self, msg, attention=False, redraw=False):
        """Sets a new status message in the status box."""
        self._clearStatusMessages()
        msg = rocketterm.utils.wrapText(msg, self.m_status_box.getNumCols(), 0)
        text = urwid.Text(('attention_text' if attention else 'text', msg))
        self.m_status_box.body.append(text)

        if redraw and self.m_loop_running:
            self.m_loop.draw_screen()

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

    def _recordURL(self, url):
        try:
            return self.m_url_map[url]
        except KeyError:
            pass

        self.m_url_list.append(url)
        nr = len(self.m_url_list)
        self.m_url_map[url] = nr
        return nr

    def _recordFile(self, file_info):
        try:
            return self.m_file_map[file_info.getID()]
        except KeyError:
            pass

        self.m_file_list.append(file_info)
        nr = len(self.m_file_list)
        self.m_file_map[file_info.getID()] = nr
        return nr

    def getMsgIDForNr(self, msg_nr):
        """Returns the msg ID for a consecutive msg nr#."""
        return self.m_msg_id_map[msg_nr]

    def getNrsForMsgID(self, msg_id):
        """Returns a list of consecutive msg nrs# for a msg ID."""
        return self.m_msg_nr_map.get(msg_id, [])

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

    def handleThreadActivity(self, old_msg, new_msg):
        """Check whether we need to update thread coloring if a new thread was
        opened."""
        if old_msg.getNumReplies() > 1:
            # thread existed already before
            return

        msg_nrs = self.getNrsForMsgID(new_msg.getID())

        if not msg_nrs:
            return

        # only update the original message
        row_nr = self._getMsgRowNr(msg_nrs[0])
        new_text = self._formatChatMessage(new_msg, msg_nrs[0])
        self.m_chat_box.body[row_nr] = new_text

    def handleDiscussionActivity(self, old_msg, new_msg):
        """Process a discussion activity update.

        This callback function returns a boolean whether the activity should
        result in an update message passed on by the Controller or not.
        """

        info = self.m_controller.getRoomInfoByID(old_msg.getRoomID())

        if info.isSubscribed() and info.isOpen():
            # we're monitoring the discussion anyway, no need to produce
            # additional noise
            return False
        elif old_msg.getDiscussionCount() == new_msg.getDiscussionCount():
            # not even new messages available so do nothing
            return False

        difftime = new_msg.getDiscussionLastModified() - old_msg.getDiscussionLastModified()

        if difftime.total_seconds() < 600:
            # avoid too frequent updates about discussion activity
            return False

        return True

    def handleNewRoomMessage(self, msg):
        """Called by the Controller when in a new message appeared in one of
        our visible rooms."""

        room_id = msg.getRoomID()

        if room_id == self.m_current_room.getID():
            self.m_room_msg_count = self.m_controller.getRoomMsgCount()
            self._addChatMessageSafe(msg, at_end=True)
            if msg.isIncrementalUpdate():
                # if this related to an older message then make sure we
                # resolve any unresolved references
                self._resolveMessageReferences()
                self._scrollMessages(ScrollDirection.NEWEST)
        else:
            cur_state = self.m_room_states.get(room_id, None)

            if cur_state is None:
                # room isn't even visible (yet?), so do nothing
                return
            elif cur_state == RoomState.ATTENTION:
                # already on attention, nothing else to do
                return
            elif self.m_controller.doesMessageMentionUs(msg):
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
        self.m_logger.debug("direct chat user status changed: {} -> {}, message = {}".format(
            status_event.getUsername(),
            status_event.getUserPresenceStatus().value,
            status_event.getStatusText()
        ))
        self._updateRoomBox()

        if self.m_current_room.isDirectChat():
            our_info = self.m_controller.getLoggedInUserInfo()
            if self.m_current_room.getPeerUserID(our_info) == status_event.getUserID():
                # update the direct chat status
                self._updateRoomHeading()

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

    def getURLForIndex(self, index):
        """Returns the URL the given url [index] refers to.

        returns None if no such URL exists.
        """

        index -= 1

        if index < 0 or index >= len(self.m_url_list):
            return

        return self.m_url_list[index]

    def getFileInfoForIndex(self, index):
        """Return the FileInfo the given file [!index] refers to.

        returns None if no such file attachment exists.
        """

        index -= 1

        if index < 0 or index >= len(self.m_file_list):
            return

        return self.m_file_list[index]

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
        """Called by the Parser when for status updates of long running
        commands."""
        self._setStatusMessage(msg, redraw=True)

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

        self._setStatusMessage(feedback, redraw=True)

    def refresh(self):
        """Force redraw of the complete screen."""
        self.m_loop.screen.clear()

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
        self._setStatusMessage(feedback, redraw=True)

    def lostConnection(self):
        self._setStatusMessage("Connection to remote server API lost", attention=True)
        self.refresh()

    def internalError(self, text):
        self._setStatusMessage("Internal error occured: {}".format(text), attention=True)
        self.refresh()

    def setRoomBoxVisible(self, visible):
        columns = self._getMainFrameColumns(show_roombox=visible)

        self.m_frame.contents['body'] = (columns, None)
        self.refresh()

    def getRoomStates(self):
        return self.m_room_states

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

    def mainLoop(self):
        """The urwid main loop that processes UI and Controller events."""

        self._applyConfig()
        self._setupWidgets()

        self.m_loop = urwid.MainLoop(
            self.m_frame,
            self._getPalette(),
            unhandled_input=self._handleInput,
            # disable mouse handling to support usual copy/paste interaction
            handle_mouse=False
        )

        self.m_urwid_pipe = self.m_loop.watch_pipe(self._externalEvent)
        self.m_cmd_parser = rocketterm.parser.Parser(self.m_global_objects)

        self.m_controller.addCallbackHandler(self, main_handler=True)
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

# vim: ts=4 et sw=4 sts=4 :

import copy
import functools
import logging
import threading

# rocket.term
import rocketterm.types


class Controller:
    """The Controller holds the non-graphical program state and offers an
    action flow oriented interface to RC.

    The Controller holds all the program state that is independent of the
    graphical display to the user. It performs lazy caching of information
    that is retrieved via the RC APIs. The Screen and Parser application logic
    should mostly use the Controller for interacting with the RC server. For
    some simple cases they may also directly use the Comm interface to do so.

    The Controller receives asynchronous notifications via the Comm interface
    and calls back into the actual application class (Screen in our case) to
    provide pre processed, more abstract information about events.
    """

    def __init__(self, callbacks, comm):
        """
        :param callbacks: The instance of a callback interface that will
                          receive event notifications of different kinds. This
                          is currently not formalized an relies on duck
                          typing.
                          Callbacks should be serialized via the
                          asyncEventOccured() callback member function. This
                          function should make sure that the application main
                          thread calls procesEvents() to execute the pending
                          asynchronous events.
        :param comm: The Comm instance held by the main program which is
                     needed by the controller to interact with the RC server.
        """

        self.m_comm = comm
        self.m_callbacks = callbacks
        self.m_logger = logging.getLogger("controller")
        self._reset()

    def _reset(self):
        self.m_selected_room = None
        # maps room IDs to room object instances, will be lazily filled with
        # room information encountered during runtime
        self.m_rooms = {}
        # a cache of room messages that is filled on-demand as we
        # approach new room objects and load more chat history
        self.m_room_msgs = dict()
        # a map from room IDs to a map of msg ID -> msg object
        # we're keeping a separate msg id map per room to avoid the potential
        # situation of referencing messages from another room
        self.m_room_msg_ids = dict()
        # a mapping of room ID to number of members.
        self.m_room_user_count = dict()
        # a mapping of room ID to a set of users IDs known to be
        # members of the respective room. This mapping is only filled
        # in a lazy fashion as we encounter users
        self.m_room_members = dict()
        # a mapping of user ID to BasicUserInfo objects. This mapping is
        # only filled in a lazy fashion as we encounter users
        self.m_basic_user_infos = dict()
        # this maps usernames to user IDs
        self.m_username_id_map = dict()
        # stores room IDs for which all history has already been
        # loaded
        self.m_history_complete = set()
        # room ID -> number of messages a room has. this includes all chat
        # history that might not yet have been cached
        self.m_room_msg_count = dict()
        # room ID -> EventSubscription. holds the individual subscriptions we
        # register for each subscribed room we have.
        self.m_room_subscriptions = dict()
        # user ID -> [EventSubscription,...]. Holds the individual
        # user event subscriptions we register for each user we're interested
        # in.
        self.m_user_event_subscriptions = dict()
        # holds the EventSubscription for "logged user" events
        self.m_user_status_subscription = None
        # list of pending asynchronous events to be handled from the
        # urwid main thread. Contains tuples of (callback, *args, **kwargs),
        # i.e. the callback to be called and the arguments to be passed to it.
        self.m_pending_events = []
        # this protects access to m_pending_events
        self.m_lock = threading.Lock()
        # a room we're waiting for to open, if any
        self.m_awaited_room = None
        self.m_ignore_next_own_status_change = False
        # full UserInfo for our own account
        self.m_local_user_info = None
        # user ID -> UserPresence. The currently known UserPresence values
        # for different user IDs
        self.m_user_status = {}
        # room ID -> msg ID. default thread the user selected for various
        # rooms, if any.
        self.m_selected_threads = {}
        # whether the full user list from the server has already been cached.
        # This is an time expensive operation...
        self.m_user_list_cached = False
        self.m_started = False

    def start(self, msg_batch_size):
        """Starts the controller runtime operation.

        :param int msg_batch_size: The number of messages the Controller
            should load at once from a room's chat history. This should at at
            least the chat window size or a bit more to allow scrolling the
            history without requiring to talk to the server right away again.

        The controller will register for ansychronous events and forward them
        to the callback interface. Various initial data will be fetched from
        the RC server like the room subscription list etc.
        """
        if self.m_started:
            raise Exception("Controller is already started")

        self.m_msg_batch_size = msg_batch_size

        self.m_local_user_info = self.m_comm.getUserInfo(
                self.m_comm.getLoggedInUserInfo()
        )

        # we need to monitor our room memberships and new direct chats
        subscription_callback = functools.partial(
                self._forwardEventAsync, callback=self._subscriptionEvent)
        room_callback = functools.partial(
                self._forwardEventAsync, callback=self._roomChangedEvent)

        user_subscriptions = self.m_user_event_subscriptions.setdefault(self.m_local_user_info.getID(), [])

        for category, cb in (
            ("subscriptions-changed", subscription_callback),
            ("rooms-changed", room_callback)
        ):
            sub_state = self.m_comm.subscribeForUserEvents(
                category,
                self.m_local_user_info,
                cb
            )
            user_subscriptions.append(sub_state)

        user_status_callback = functools.partial(
                self._forwardEventAsync, callback=self._userStatusEvent)
        self.m_user_status_subscription = self.m_comm.subscribeForLoggedEvents(
            "user-status",
            user_status_callback
        )

        self._fetchRoomInfo()
        self.m_started = True

    def stop(self):
        """Stops the controller runtime operation.

        This will unsubscribe from all asynchronous events and reset all
        controller state. Caches will be purged. After this call no more
        callbacks should occur.
        """

        if not self.m_started:
            raise Exception("Controller isn't currently started.")

        for subscription in self.m_room_subscriptions.values():
            self.m_comm.unsubscribe(subscription)

        self.m_room_subscriptions.clear()

        for subscriptions in self.m_user_event_subscriptions.values():
            for sub in subscriptions:
                self.m_comm.unsubscribe(sub)

        self.m_user_event_subscriptions.clear()

        if self.m_user_status_subscription:
            self.m_comm.unsubscribe(self.m_user_status_subscription)
            self.m_user_status_subscription = None

        self._reset()

    def getLoggedInUserInfo(self):
        """Returns the full UserInfo associated with the logged in user."""
        return self.m_local_user_info

    def doesMessageMentionUs(self, msg):
        """Returns a boolean whether the given RoomMessage mentions the logged in
        user."""
        for mention in msg.getMentions():
            if mention.getID() == self.m_comm.getUserID():
                return True
            elif mention.getID() == "all":
                # this is the @all mention
                return True

        return False

    def processEvents(self):
        """Processes pending asynchronous events in the context of the
        application main thread.
        """

        with self.m_lock:
            # avoid calling callbacks with the lock held (lest we enter some
            # deadlock situation). Therefore simply grab the list of pending
            # events and then continue without the lock.
            to_process = self.m_pending_events
            self.m_pending_events = []

        for cb, args, kwargs in to_process:
            cb(*args, **kwargs)

    def getSelectedThreadID(self, room=None):
        """Returns the default thread message ID for the given room (or the
        currently selected room).

        If no room or no default thread is selected then None is returned.
        """
        room = self._getRoomToOperateOn(room)
        if not room:
            return

        return self.m_selected_threads.get(room.getID(), None)

    def selectThread(self, msg_id, room=None):
        """Selects a new default thread to operate in for the given room (or
        the currently selected room).
        """
        room = self._getRoomToOperateOn(room)
        if not room:
            raise Exception("no room selected")

        self.m_selected_threads[room.getID()] = msg_id

    def leaveThread(self, room=None):
        """Leaves the currently selected default thread in the given room (or
        the currently selected room)."""
        room = self._getRoomToOperateOn(room)
        if not room:
            raise Exception("no room selected")

        self.m_selected_threads.pop(room.getID(), None)

    def getRoomInfo(self, rid):
        try:
            return self.m_rooms[rid]
        except KeyError:
            # probably a room we're not subscribed to. explicitly fetch the
            # info, don't cache it at the moment, because this object won't
            # support all operations due to the missing subscription info
            return self.m_comm.getRoomInfo(rid)

    def getJoinedDirectChats(self, filter_hidden=True):
        ret = [room for room in self.m_rooms.values() if
               room.isDirectChat() and (room.isOpen() or not filter_hidden)]
        return sorted(ret, key=lambda r: r.getName())

    def getJoinedPrivateChats(self, filter_hidden=True):
        ret = [room for room in self.m_rooms.values() if
               room.isPrivateChat() and (room.isOpen() or not filter_hidden)]
        return sorted(ret, key=lambda r: r.getName())

    def getJoinedOpenChats(self, filter_hidden=True):
        ret = [room for room in self.m_rooms.values() if
               room.isChatRoom() and (room.isOpen() or not filter_hidden)]
        return sorted(ret, key=lambda r: r.getName())

    def getJoinedRooms(self, filter_hidden=True):
        return \
            self.getJoinedOpenChats(filter_hidden) + \
            self.getJoinedPrivateChats(filter_hidden) + \
            self.getJoinedDirectChats(filter_hidden)

    def getCachedRoomMessages(self, room=None):
        """Returns a list of currently cached RoomMessage objects for the
        given room (or the currently selected room).

        The returned list will be ordered by message date i.e. the oldest
        message will be the first element.
        """
        room = self._getRoomToOperateOn(room)
        if not room:
            # no room at all is available
            return []

        return self.m_room_msgs.get(room.getID(), [])

    def loadMoreRoomMessages(self, room=None):
        """Loads additional message history for the given room object.

        The newly loaded RoomMessage objects will be returned as a list,
        sorted by message creation date. If no more message history is
        available then None is returned.
        """

        room = self._getRoomToOperateOn(room)
        if not room:
            return []
        elif room.getID() in self.m_history_complete:
            return []

        msgs = self.m_room_msgs.setdefault(room.getID(), [])
        oldest_known = msgs[-1] if msgs else None

        remaining, new_msgs = self.m_comm.getRoomMessages(
                room, self.m_msg_batch_size, oldest_known)
        if not new_msgs or remaining == 0:
            self.m_history_complete.add(room.getID())

        # cache the additional info we've got
        id_map = self.m_room_msg_ids.setdefault(room.getID(), {})
        room_members = self.m_room_members.setdefault(room.getID(), set())

        for msg in new_msgs:
            id_map[msg.getID()] = msg
            user = msg.getUserInfo()
            room_members.add(user.getID())
            self._cacheUserInfo(user)

        msgs.extend(new_msgs)

        self.m_room_msg_count.setdefault(room.getID(), remaining + len(msgs))

        return new_msgs

    def lookupMessage(self, room, msg_id):
        """Returns the message with the given msg_id in the given room.

        If no matching (cached) message could be found, None is returned.
        """

        id_map = self.m_room_msg_ids.get(room.getID(), None)

        if not id_map:
            return None

        return id_map.get(msg_id, None)

    def getRoomMsgCount(self, room=None):
        """Returns the number of messages available in the given room (or the
        currently selected room).

        This includes messages not yet cached by the Controller.
        """
        room = self._getRoomToOperateOn(room)
        if not room:
            # no room at all available
            return 0

        return self.m_room_msg_count[room.getID()]

    def getSelectedRoom(self):
        """Returns the currently selected room object or None if none is
        selected."""
        return self.m_selected_room

    def selectRoom(self, room):
        """Selects the given room object as the new default room to operate
        on."""
        if self.m_selected_room == room:
            # nothing to do
            return

        self.m_selected_room = room

        room_msgs = self.m_room_msgs.get(room.getID(), [])

        if len(room_msgs) < self.m_msg_batch_size and \
                room.getID() not in self.m_history_complete:
            self.loadMoreRoomMessages(room)

        if room.supportsMembers():
            self._cacheRoomMembers(room)

        self.m_callbacks.newRoomSelected()

    def selectRoomBySpec(self, spec):
        """Tries to select a room describe by the given label spec
        like $group, #channel or @personal. Returns a boolean
        indicating whether a room matched and was selected."""

        for room in self.m_rooms.values():
            if room.matchesRoomSpec(spec):
                self.selectRoom(room)
                return True

        return False

    def getRoomUserCount(self, room):
        """Returns the number of users that are members of the given room.

        Note that not all room members will be locally cached.
        """
        # we need to have retrieved at least one batch of users to know the
        # full member count
        self._cacheRoomMembers(room)
        return self.m_room_user_count[room.getID()]

    def getKnownRoomMembers(self, room):
        """Returns the currently known room members as a list of BasicUserInfo
        instances.

        Members are only collected in a lazy fashion as we encounter them,
        because some rooms have a membership count of over 1.000 users and
        that would be pretty high load on the network to enumerate them all.
        """
        members = self.m_room_members.get(room.getID(), set())
        return [self.m_basic_user_infos[uid] for uid in members]

    def getBasicUserInfoByName(self, username):
        """Returns a BasicUserInfo instance for the given username.

        This tries to find a cached UserInfo for the username. If this is not
        possible the remote server will be queried. If the username is unknown
        then None will be returned.
        """
        try:
            uid = self.m_username_id_map[username]
            return self.m_basic_user_infos[uid]
        except KeyError:
            info = self.m_comm.getUserInfoByName(username)
            if info:
                self._cacheUserInfo(info)
            return info

    def getBasicUserInfoByID(self, uid):
        """Returns a BasicUserInfo instance for the given user ID.

        This works just like getBasicUserInfoByName().
        """
        try:
            return self.m_basic_user_infos[uid]
        except KeyError:
            info = self.m_comm.getUserInfoByID(uid)
            if info:
                self._cacheUserInfo(info)
            return info

    def getKnownUsers(self, load_all_users=False):
        """Returns a list of BasicUserInfo for all known users.

        If ``load_all_users`` is False then only all currently cached users
        will be returned. Otherwise all users will be loaded from the server,
        which can be a time consuming operation on larger installations.
        """
        if load_all_users:
            self._cacheUserList()
        return self.m_basic_user_infos.values()

    def getUserStatus(self, user):
        """Returns a tuple of (UserPresence, "status text") for the
        given user object."""
        try:
            return self.m_user_status[user.getID()]
        except KeyError:
            status = self.m_comm.getUserStatus(user)
            info = (status.getStatus(), status.getMessage())
            self.m_user_status[user.getID()] = info
            return info

    def selectNextRoom(self):
        """Selects the next room from the list of (opened) joined rooms.

        Returns a boolean indicating whether a new room could be selected."""
        return self._selectFromRoomList(1)

    def selectPrevRoom(self):
        """Selects the previous room from the list of (opened) joined
        rooms.

        Returns a boolean indicating whether a new room could be selected."""
        return self._selectFromRoomList(-1)

    def selectAnyRoom(self):
        """Selects any room from the room list, if possible.

        This is just to make sure that actually some room is selected (e.g.
        during startup). Returns a boolean indicator whether actually any room
        was available to select.
        """
        joined_rooms = self.getJoinedRooms()

        if not joined_rooms:
            return False

        self.selectRoom(joined_rooms[0])
        return True

    def hideRoom(self, room):
        """Hides the given room object from the user's room list.

        You should wait for the roomHidden() event callback before
        changing anything in the user display.
        """
        self.m_comm.hideRoom(room)

    def openRoom(self, room):
        """Opens the given room object in the user's room list.

        You should wait for the roomOpened() event callback before changing
        anything in the user display."""
        self.m_comm.openRoom(room)
        self.m_awaited_room = room

    def sendMessage(self, msg, room=None):
        """Sends a new chat message into the given room (or the currently
        selected room).

        This function will respect any currently selected default thread for
        the room.
        """
        room = self._getRoomToOperateOn(room)

        if not room:
            raise Exception("no room selected")

        thread_id = self.getSelectedThreadID(room)
        self.m_comm.sendMessage(room, msg, thread_id)

    def setUserStatus(self, presence, message):

        # there seems to be some kind of bug that only status message changed
        # are reported via stream-notify-logged, but not if the status
        # changes. so actively poll from the REST API then ...
        # TODO: this would be worth further investigation and maybe creating
        # an RC upstream issue.
        # this has been fixed in a newer RC server version already in commit
        # 287d1dcb376a4613c9c2d6f5b9c22f3699891d2e (version 3.7.0)
        self.m_comm.setUserStatus(presence, message)

        our_id = self.m_local_user_info.getID()
        new_status = self.m_comm.getUserStatus(self.m_local_user_info)
        new_status = (new_status.getStatus(), new_status.getMessage())
        old_status = self.m_user_status.get(our_id, None)
        self.m_logger.warning("old_status = {}, new_status = {}".format(old_status, new_status))
        if new_status != old_status:
            if new_status[1] != old_status[1]:
                # not even that we're not getting a correct update for the status,
                # but if the message changed then we're getting an update for
                # the message but with the wrong status ... so ignore that
                self.m_ignore_next_own_status_change = True
            self.m_user_status[our_id] = new_status
            self.m_callbacks.ownUserStatusChanged(new_status)

    def _getRoomToOperateOn(self, room):
        """Helper function to implement the often used logic to operate either
        on the provided room parameter or the currently selected room."""
        if room:
            return room
        return self.m_selected_room

    def _forwardEventAsync(self, *args, **kwargs):
        """Forwards the event occuring asynchronously to the callback
        interface for serialized processing in the main thread."""
        callback = kwargs.pop('callback')
        # forward the event to the main loop
        with self.m_lock:
            self.m_pending_events.append((callback, args, kwargs))
        self.m_callbacks.asyncEventOccured()

    def _ignoreMessageEvent(self, msg):
        # ignore these duplicate thread root message reports
        # they're only sent duplicate in the event subscription, not
        # when loading the room history explicitly
        return msg.getNumReplies() != 0

    def _newRoomMessage(self, collection, event, msg):
        """Callback for handling new room message events."""

        if self._ignoreMessageEvent(msg):
            return

        room = self.m_rooms.get(msg.getRoomID())
        msgs = self.m_room_msgs.setdefault(room.getID(), [])
        id_map = self.m_room_msg_ids.setdefault(room.getID(), {})
        old_msg = id_map.get(msg.getID(), None)
        id_map[msg.getID()] = msg

        if old_msg:
            msg = self._handleMessageUpdate(old_msg, msg)

            if not msg:
                # ignore the message
                return
        else:
            # opportunistically cache new info
            user_info = msg.getUserInfo()
            self._cacheUserInfo(user_info)
            self._cacheRoomMember(room, user_info)

        try:
            self.m_room_msg_count[room.getID()] += 1
        except KeyError:
            # no history was loaded yet, will be set during
            # loadMoreRoomMessages()
            pass

        msgs[0:0] = [msg]
        self.m_callbacks.handleNewRoomMessage(msg)

    def _handleMessageUpdate(self, old_msg, msg):
        """This happens e.g. when reactions are added to messages. Try
        to filter out useless updates, otherwise try to make clear
        what happened by changing message content."""

        # we could also simply update the original message. this would
        # be easier on the implementation side. on the other hand this
        # is kind of a feature to see when reactions happen. Although
        # this data cannot be reconstructed currently from server data
        # after the events are gone. So it is only ephemeral data and
        # once the program is restarted only a single message will
        # appear anymore.

        if msg.wasEdited() and msg.getEditTime() != old_msg.getEditTime():
            # edited messages are handled by Screen itself
            return msg

        old_reactions = old_msg.getReactions()
        new_reactions = msg.getReactions()

        if old_reactions == new_reactions:
            return None

        ret = copy.deepcopy(msg)

        new_text = []

        user_prefix = rocketterm.types.BasicUserInfo.typePrefix()

        for reaction, info in new_reactions.items():

            old_info = old_reactions.get(reaction, {'usernames': []})
            old_users = old_info.get('usernames')
            new_users = info['usernames']

            for user in new_users:
                if user not in old_users:
                    new_text.append(
                        "{}{} reacted with {}".format(user_prefix, user, reaction)
                    )

        for reaction, info in old_reactions.items():

            new_info = new_reactions.get(reaction, {'usernames': []})
            new_users = new_info.get('usernames')
            old_users = info['usernames']

            for user in old_users:
                if user not in new_users:
                    new_text.append(
                        "{}{} removed {} reaction".format(user_prefix, user, reaction)
                    )

        ret.setMessage('\n'.join(new_text))
        ret.setIsIncrementalUpdate(True)

        return ret

    def _subscriptionEvent(self, collection, change_type, event, data):
        """Called when something related to the user's subscriptions changes
        (new rooms joined, rooms left, hide/show event). It's a bit
        strange that this event also always occurs when a known room
        receives a new message."""

        rid = data.getRoomID()

        if change_type == "updated":
            room = self.m_rooms.get(rid, None)
            if room:
                self._subscriptionChanged(room, data)
            else:
                self._subscriptionAdded(data)
        elif change_type == "removed":
            self._subscriptionRemoved(data)
        elif change_type == "inserted":
            self._subscriptionAdded(data)
        else:
            self.m_logger.warning("Unknown subscription change type: {}".format(change_type))

    def _roomChangedEvent(self, category, change_type, event, data):
        """Called when something related to one of our joined rooms
        changes."""

        if change_type != "updated":
            # this can happen when e.g. creating a direct chat and we get the
            # room changed event sooner than the subscription changed event
            self.m_logger.warning("Ignoring room change event of type {}: {}".format(
                change_type, str(data)
            ))
            return

        room = self.m_rooms[data['_id']]
        room.setRaw(data)

        self.m_callbacks.roomChanged(room)

    def _userStatusEvent(self, category, change_type, status_event):
        info = (status_event.getUserPresenceStatus(), status_event.getStatusText())
        self.m_user_status[status_event.getUserID()] = info

        if status_event.getUserID() == self.m_local_user_info.getID():
            # update our local representation of our own user
            # status
            self.m_local_user_info = self.m_comm.getUserInfo(self.m_local_user_info)
            if self.m_ignore_next_own_status_change:
                self.m_ignore_next_own_status_change = False
            else:
                self.m_callbacks.ownUserStatusChanged(status_event)
            return

        # check whether the user status for any of our open direct chats
        # changed and inform our callback interface, if necessary

        for chat in self.getJoinedDirectChats():
            if chat.getPeerUserID(self.m_local_user_info) == status_event.getUserID():
                self.m_callbacks.newDirectChatUserStatus(status_event)

    def _subscriptionChanged(self, room, new_data):
        # an already known room changed
        old_data = room.getSubscription()
        room.setSubscription(new_data)

        if old_data.isOpen() == new_data.isOpen():
            # nothing important changed
            return

        # try to have some room selected if the current room is hidden or if
        # previously no room was available to select...

        if new_data.isOpen():
            # room is visible now
            self.m_callbacks.roomOpened(room)
            if self.m_awaited_room and room.getID() == self.m_awaited_room.getID():
                self.m_awaited_room = None
                self.selectRoom(room)
            elif not self.m_selected_room:
                self.selectRoom(room)
        else:
            # room is hidden now
            self.m_callbacks.roomHidden(room)

            if self.m_selected_room.getID() == room.getID():
                if not self.selectAnyRoom():
                    self.m_selected_room = None
                    self.m_callbacks.newRoomSelected()

    def _subscriptionAdded(self, data):
        # a new room appeared

        # NOTE: this is potentially inefficient, we could get
        # an incremental update from the server by passing a
        # timestamp
        new_rooms = self.m_comm.getJoinedRooms()

        for room in new_rooms:
            if room.getID() == data.getRoomID():
                break
        else:
            # race?
            return

        self._addRoom(room)
        self.m_callbacks.roomAdded(room)

    def _subscriptionRemoved(self, data):
        gone = self.m_rooms.get(data.getRoomID(), None)
        if not gone:
            self.m_logger.warning("subscription for unknown room was removed?  " + str(data))
            return
        self._delRoom(data.getRoomID())

        self.m_callbacks.roomRemoved(gone)

    def _addRoom(self, room):
        # register for new room events
        callback = functools.partial(self._forwardEventAsync, callback=self._newRoomMessage)
        sub_state = self.m_comm.subscribeForRoomMessages(room, callback)
        self.m_room_subscriptions[room.getID()] = sub_state
        self.m_rooms[room.getID()] = room

    def _delRoom(self, rid):
        try:
            sub_id = self.m_room_subscriptions.pop(rid)
            self.m_comm.unsubscribe(sub_id)
        except KeyError:
            self.m_logger.warning("attempt to delete room not yet subscribed to? " + str(rid))
        self.m_rooms.pop(rid)
        if self.m_selected_room.getID() == rid:
            self.selectAnyRoom()

    def _fetchRoomInfo(self):
        room_list = self.m_comm.getJoinedRooms()
        self.m_rooms = {}

        for room in room_list:
            self._addRoom(room)

    def _cacheRoomMember(self, room, user):
        members = self.m_room_members.setdefault(room.getID(), set())
        members.add(user.getID())

    def _cacheRoomMembers(self, room):
        if room.getID() in self.m_room_user_count:
            # already cached
            return

        total, members = self.m_comm.getRoomMembers(room)

        self.m_room_user_count[room.getID()] = total
        room_members = self.m_room_members.setdefault(room.getID(), set())
        for member in members:
            room_members.add(member.getID())
            self._cacheUserInfo(member)

        self.m_logger.debug("Cached members of {}: {}/{}:".format(room.getName(), len(members), total))

    def _cacheUserList(self):
        """Caches the full server side user list. Potentially time expensive."""
        if self.m_user_list_cached:
            return

        for info in self.m_comm.getUserList():
            self._cacheUserInfo(info)

        self.m_user_list_cached = True

    def _cacheUserInfo(self, info):
        self.m_basic_user_infos.setdefault(info.getID(), info)
        self.m_username_id_map.setdefault(info.getUsername(), info.getID())

    def _selectFromRoomList(self, offset):
        """Selects a new room from the room list relative to the currently
        selected room.

        :param int offset: Positive of negative integer that determines which
                           room to select relativ to the currently selected
                           room.
        """

        rooms = self.getJoinedRooms()
        if len(rooms) <= 1 or not self.m_selected_room:
            return False
        new = -1

        for nr, room in enumerate(rooms):
            if room == self.m_selected_room:
                new = nr + offset
                break

        if new < 0 or new >= len(rooms):
            if offset >= 0:
                new = 0
            else:
                new = len(rooms) - 1

        self.selectRoom(rooms[new])
        return True

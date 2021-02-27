# vim: ts=4 et sw=4 sts=4 :

import copy
import functools
import logging
import threading

import rocketterm.emojis


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

    def __init__(self, global_objects):
        """
        :param global_objects: program wide global object instances
        """

        self.m_comm = global_objects.comm
        from rocketterm.utils import CallbackMultiplexer
        self.m_callbacks = CallbackMultiplexer()
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
        # a mapping of room IDs to ChatRoom objects for all known channels on
        # the server
        self.m_channels = {}
        # a mapping of user ID to BasicUserInfo objects. This mapping is
        # only filled in a lazy fashion as we encounter users
        self.m_basic_user_infos = dict()
        # this maps usernames to user IDs
        self.m_username_id_map = dict()
        # stores room IDs for which all history has already been
        # loaded
        self.m_history_complete = set()
        # room ID -> number of messages a room has. this includes all chat
        # history that might not yet have been cached, also incremental
        # message updates that we included ourselves and are unknown by the
        # server
        self.m_room_msg_count = dict()
        # room ID -> EventSubscription. holds the individual subscriptions for
        # new room messages we register for each subscribed and opened room we
        # have.
        self.m_room_msg_subscriptions = dict()
        # user ID -> [EventSubscription,...]. Holds the individual
        # user event subscriptions we register for each user we're interested
        # in.
        self.m_user_event_subscriptions = dict()
        # holds the EventSubscription for "logged user" events
        self.m_user_status_subscription = None
        # room ID -> [EventSubscription,...]. Holds the individual
        # stream-notify-room subscriptions we register for each room we're
        # interested in.
        self.m_room_event_subscriptions = dict()
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
        # cached EmojiInfo instances from the server
        self.m_custom_emoji_list = None
        # an instance of ServerInfo containing remote server version info
        self.m_server_info = None
        self.m_started = False

    def addCallbackHandler(self, callback, main_handler=False):
        """Adds a callback interface that will receive various callbacks when
        certain events like new messages occur.

        Multiple callback interface can be registered in parallel.
        """
        self.m_callbacks.addConsumer(callback, main_handler)

    def delCallbackHandler(self, callback):
        self.m_callbacks.delConsumer(callback)

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

        self.m_server_info = self.m_comm.getServerInfo()

        # we need to monitor our room memberships and new direct chats
        subscription_callback = functools.partial(
                self._forwardEventAsync, callback=self._subscriptionEvent)
        room_changed_callback = functools.partial(
                self._forwardEventAsync, callback=self._roomChangedEvent)

        user_subscriptions = self.m_user_event_subscriptions.setdefault(self.m_local_user_info.getID(), [])

        for category, cb in (
            ("subscriptions-changed", subscription_callback),
            ("rooms-changed", room_changed_callback)
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

        # for some reason previously for message deletions a 'rm' message type
        # was received via regular message subscriptions, but now deleted
        # messages only appear in this special per-room deleteMessage event.
        #
        # also previously deleted messages have still been returned by the
        # server afterwards as "deleted" but now they completely disappear.
        #
        # so let's be prepared for both
        for room in self.getJoinedRooms():
            self._subscribeRoomEvents(room)

        self.m_started = True
        self.m_comm.setErrorCallback(self.lostAPIConnection)

    def lostAPIConnection(self):
        self.m_callbacks.lostConnection()

    def stop(self):
        """Stops the controller runtime operation.

        This will unsubscribe from all asynchronous events and reset all
        controller state. Caches will be purged. After this call no more
        callbacks should occur.
        """

        if not self.m_started:
            raise Exception("Controller isn't currently started.")

        for subscription in self.m_room_msg_subscriptions.values():
            self.m_comm.unsubscribe(subscription)

        self.m_room_event_subscriptions.clear()

        for subscriptions in self.m_user_event_subscriptions.values():
            for sub in subscriptions:
                self.m_comm.unsubscribe(sub)

        for subscriptions in self.m_room_event_subscriptions.values():
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

    def isMessageFromUs(self, msg):
        """Returns whether the given message was authored by the currently
        logged in user."""
        return msg.getUserInfo().getID() == self.m_comm.getUserID()

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
            try:
                cb(*args, **kwargs)
            except Exception as e:
                import traceback
                et = traceback.format_exc()
                self.m_logger.error("Event processing for {} failed: {}\n{}\n".format(str(cb), str(e), et))
                self.m_callbacks.internalError("event processing for {} failed: {}".format(str(cb), str(e)))

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

    def getRoomInfoByID(self, rid):
        """Returns a specialization of RoomBase for the given room ID."""
        try:
            return self.m_rooms[rid]
        except KeyError:
            # probably a room we're not subscribed to. explicitly fetch the
            # info, don't cache it at the moment, because this object won't
            # support all operations due to the missing subscription info
            return self.m_comm.getRoomInfo(rid)

    def getRoomInfoByLabel(self, label):
        """Returns a specialization of RoomBase for the given room label.

        :param str label: The room name with leading prefix like '#', '$',
            etc. It can be the short or the friendly room name.
        """

        for room in self.m_rooms.values():
            if room.typePrefix() != label[0]:
                continue

            if room.getName() == label[1:] or room.getFriendlyName() == label[1:]:
                return room

        return self.m_comm.getRoomInfo(room_name=label[1:])

    def getChannels(self):
        """Returns a dictionary of all rooms known on the server."""

        if self.m_channels:
            # TODO: we should implement a refresh logic here, e.g. when the
            # last load is older then a minute or something, then request a
            # delta from the server
            pass
        else:

            def channelLoadProgress(so_far, total):
                self.m_callbacks.getChannelsInProgress(so_far, total)

            channels = self.m_comm.getChannelList(progress_cb=channelLoadProgress)

            for channel in channels:
                self.m_channels[channel.getID()] = channel

        return self.m_channels

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

    def getMessageFromID(self, msg_id, room=None):
        room = self._getRoomToOperateOn(room)
        if not room:
            return None

        return self.m_room_msg_ids[room.getID()].get(msg_id, None)

    def loadMoreRoomMessages(self, room=None, amount=None):
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

        if not amount:
            amount = self.m_msg_batch_size

        msgs = self.m_room_msgs.setdefault(room.getID(), [])
        for msg in reversed(msgs):
            if msg.isIncrementalUpdate():
                continue

            oldest_known = msg
            break
        else:
            oldest_known = None

        self.m_callbacks.loadHistoryStarted(room)

        remaining, new_msgs = self.m_comm.getRoomMessages(
                room, amount, oldest_known)
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

        # NOTE: this count includes incremental update messages that the
        # server doesn't know about i.e. our total message count can be higher
        # for a room than what the server tells us.
        self.m_room_msg_count.setdefault(room.getID(), remaining + len(msgs))

        self.m_callbacks.loadHistoryEnded(room)

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

        This includes messages not yet cached by the Controller. If the amount
        is not yet known then -1 is returned.
        """
        room = self._getRoomToOperateOn(room)
        if not room:
            # no room at all available
            return 0

        return self.m_room_msg_count.get(room.getID(), -1)

    def getSelectedRoom(self):
        """Returns the currently selected room object or None if none is
        selected."""
        return self.m_selected_room

    def selectRoom(self, room):
        """Selects the given room object as the new default room to operate
        on."""
        if self.m_selected_room == room:
            # nothing to do
            return True
        elif not room.isOpen():
            self.m_logger.warning("Trying to select hidden room {}".format(room.getName()))
            return False

        self.m_selected_room = room

        room_msgs = self.m_room_msgs.get(room.getID(), [])

        if len(room_msgs) < self.m_msg_batch_size and \
                room.getID() not in self.m_history_complete:
            self.loadMoreRoomMessages(room)

        if room.supportsMembers():
            self._cacheRoomMembers(room)

        if room.getSubscription().hasUnreadMessages():
            # reading room history seems not enough to reset the unread
            # messages counter so do it explicitly
            self.m_comm.markRoomAsRead(room)

        self.m_callbacks.newRoomSelected()
        return True

    def selectRoomBySpec(self, spec):
        """Tries to select a room described by the given label spec
        like $group, #channel or @personal. Returns a boolean
        indicating whether a room matched and was selected."""

        for room in self.m_rooms.values():
            if room.matchesRoomSpec(spec):
                return self.selectRoom(room)

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

    def getBasicUserInfoByName(self, username, only_cached=False):
        """Returns a BasicUserInfo instance for the given username.

        This tries to find a cached UserInfo for the username. If this is not
        possible the remote server will be queried, unless only_cached is set.
        If the username is unknown then None will be returned.
        """
        try:
            uid = self.m_username_id_map[username]
            return self.m_basic_user_infos[uid]
        except KeyError:
            if only_cached:
                return None
            try:
                info = self.m_comm.getUserInfoByName(username)
            except Exception:
                return None

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

    def getUserStatus(self, user, need_text=False):
        """Returns a tuple of (UserPresence, "status text") for the
        given user object."""
        try:
            if self._getServerNeedsUserStatusEventWorkaround() and need_text:
                # disable caching if this problem exists
                raise KeyError
            return self.m_user_status[user.getID()]
        except KeyError:
            return self._refreshUserStatus(user)

    def selectNextRoom(self):
        """Selects the next room from the list of (opened) joined rooms.

        Returns a boolean indicating whether a new room could be selected."""
        return self._selectFromRoomList(1)

    def selectPrevRoom(self):
        """Selects the previous room from the list of (opened) joined
        rooms.

        Returns a boolean indicating whether a new room could be selected."""
        return self._selectFromRoomList(-1)

    def selectAnyRoom(self, hint_index=0):
        """Selects any room from the room list, if possible.

        This is just to make sure that actually some room is selected (e.g.
        during startup or when a room is removed). Returns a boolean indicator
        whether actually any room was available to select.

        :param int hint_index: an index into the list of joined (visible)
                               rooms for being able to select a room close to
                               the old position, which is useful when a room
                               gets hidden for example.
        """
        joined_rooms = self.getJoinedRooms()

        if not joined_rooms:
            return False

        if hint_index is None:
            hint_index = 0

        hint_index = min(hint_index, len(joined_rooms) - 1)

        self.selectRoom(joined_rooms[hint_index])
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

        self.m_comm.setUserStatus(presence, message)

        if not self._getServerNeedsSetUserStatusWorkaround():
            return

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

    def joinChannel(self, room):
        """Joins the given chat room and selects it once the subscription is
        sent back from the server."""

        self.m_comm.joinChannel(room)
        self.m_awaited_room = room

    def createRoom(self, room, initial_users):
        rid = self.m_comm.createRoom(room, initial_users)

        for i in range(20):
            try:
                room = self.getRoomInfoByID(rid)
                self.m_awaited_room = room
                break
            except Exception:
                import time
                time.sleep(0.5)
        else:
            raise Exception("Timed out waiting for new room subscription")

    def fetchCustomEmojiData(self, force_refresh=False):
        if not force_refresh and self.m_custom_emoji_list is not None:
            return

        self.m_custom_emoji_list = self.m_comm.getCustomEmojiList()

        rocketterm.emojis.addCustomEmojis(
            [emoji.getName() for emoji in self.m_custom_emoji_list]
        )

    def getEmojiData(self):
        """Returns a dictionary of emoji categories and their names.

        Example return value: {
            "custom": [ "myemoji", "youremoji" ],
            [...]
        }
        """
        self.fetchCustomEmojiData()

        return rocketterm.emojis.EMOJIS_BY_CATEGORY

    def _getServerNeedsSetUserStatusWorkaround(self):
        return rocketterm.utils.getServerHasSetUserStatusBug(self.m_server_info)

    def _getServerNeedsUserStatusEventWorkaround(self):
        return rocketterm.utils.getServerHasBogusUserStatusEventBug(self.m_server_info)

    def _refreshUserStatus(self, user):
        status = self.m_comm.getUserStatus(user)
        info = (status.getStatus(), status.getMessage())
        self.m_user_status[user.getID()] = info
        return info

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

    def _getNewestMessage(self, msgs):
        for newest in msgs:
            if newest.isIncrementalUpdate():
                continue

            return newest

        self.m_logger.error("couldn't find newest message! Assuming no update...")
        return None

    def _isMessageUpdate(self, room_msgs, msg):
        """Returns a boolean whether the given RoomMessage is only an update
        for an existing message."""

        if not room_msgs:
            # the room had no messages before so it must be a new message
            return False

        newest = self._getNewestMessage(room_msgs)

        if not newest:
            return False

        return msg.getClientTimestamp() <= newest.getClientTimestamp()

    def _shouldProcessMessageUpdate(self, room_msgs, old_msg, new_msg):
        """Returns a boolean whether the given updated RoomMessage should be
        ignored from processing."""

        newest = self._getNewestMessage(room_msgs)
        MessageType = rocketterm.types.MessageType

        # if the update wasn't applied yet on the server side, ignore this.
        # This happens e.g. when new reactions are added, then multiple
        # notifications go out, the first one will not have an updated
        # timestamp.
        if new_msg.getServerTimestamp() <= newest.getServerTimestamp():
            return False
        elif not old_msg:
            # we cannot really tell since there is no comparison object
            # existing
            return True
        elif old_msg.getNumReplies() != new_msg.getNumReplies():
            self.m_callbacks.handleThreadActivity(old_msg, new_msg)
            return False
        elif old_msg.getMessageType() == MessageType.DiscussionCreated:
            # this means that a discussion sub-room has new messages
            return self.m_callbacks.handleDiscussionActivity(old_msg, new_msg)

        old_json = old_msg.getRaw()
        new_json = new_msg.getRaw()

        for key, value in new_json.items():
            if key in ('ts', '_updatedAt', 'u'):
                # sometimes we get updates with resolved usernames, ignore
                # that. that timestamp fields change is also expected.
                continue

            if key not in old_json:
                return True
            elif old_json[key] != value:
                return True

        for key in old_json:
            if key not in new_json:
                # something was removed
                return True

        # nothing interesting changed
        return False

    def _newRoomMessage(self, collection, event, msg):
        """Callback for handling new room message events.

        The logic for this is surprisingly complex, because updates of old
        messages are not communicated very well by the server and spurious
        updates are also sent out with incomplete information.
        """

        room = self.m_rooms.get(msg.getRoomID())
        messages = self.m_room_msgs.setdefault(room.getID(), [])
        msg_map = self.m_room_msg_ids.setdefault(room.getID(), {})
        orig_msg = msg_map.get(msg.getID(), None)

        if orig_msg:
            if orig_msg.getServerTimestamp() == msg.getServerTimestamp():
                # ignore this, it's some garbage update the server sent
                return

        if self._isMessageUpdate(messages, msg):
            if not self._shouldProcessMessageUpdate(messages, orig_msg, msg):
                return

            msg_to_add = copy.deepcopy(msg)
            msg_to_add.setIsIncrementalUpdate(orig_msg)
        else:
            msg_to_add = msg

        # opportunistically cache new info
        self._cacheMessage(room, msg)

        messages[0:0] = [msg_to_add]
        self.m_room_msg_count[room.getID()] += 1
        self.m_callbacks.handleNewRoomMessage(msg_to_add)

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

    def _roomMessageDeleted(self, room_id, msg_id):
        room = self.getRoomInfoByID(room_id)
        old_msg = self.getMessageFromID(msg_id, room)

        import datetime
        now = datetime.datetime.now()

        # treat this like a message update to have only a single code path for
        # this in the implementation of our consumers

        if old_msg:
            msg = copy.deepcopy(old_msg)
            msg.setIsIncrementalUpdate(old_msg)
        else:
            msg = rocketterm.types.RoomMessage.createNew(room_id, "message was deleted", msg_id)
            msg.setClientTimestamp(now)
            msg.setServerTimestamp(now)
            # claim we removed it, we have no other way to fill in sensible
            # info
            msg.setUserInfo(self.m_local_user_info)

        # claim the author itself removed it
        msg.setEditUser(msg.getRaw()['u'])
        # claim it is a regular "message removed" message
        msg.setMessageType(rocketterm.types.MessageType.MessageRemoved)
        msg.setEditTime(now)
        # prepend the message so it can be reconstructed later
        messages = self.m_room_msgs.setdefault(room.getID(), [])
        messages[0:0] = [msg]
        self.m_room_msg_count[room.getID()] += 1
        self.m_callbacks.handleNewRoomMessage(msg)

    def _subscriptionChanged(self, room, new_data):

        is_selected_room = self.m_selected_room and \
            self.m_selected_room.getID() == room.getID()

        if is_selected_room:
            hint_index = self._getJoinedRoomIndex(room)

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
            self._subscribeRoomEvents(room)
            self.m_callbacks.roomOpened(room)
            self._selectRoomIfAwaited(room)
            if not self.m_selected_room:
                self.selectRoom(room)
        else:
            self._unsubscribeRoomEvents(room)
            # room is hidden now
            self.m_callbacks.roomHidden(room)

            if is_selected_room:
                if not self.selectAnyRoom(hint_index):
                    self.m_selected_room = None
                    self.m_callbacks.newRoomSelected()

    def _selectRoomIfAwaited(self, room):
        if not self.m_awaited_room:
            return False
        elif room.getID() != self.m_awaited_room.getID():
            return False

        self.m_awaited_room = None
        self.selectRoom(room)
        return True

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
        self._selectRoomIfAwaited(room)
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
        self.m_room_msg_subscriptions[room.getID()] = sub_state
        self.m_rooms[room.getID()] = room
        # load initial messages for the room. To process room message events
        # in the callback correctly later on we need at least the newest
        # message. It doesn't seem to make a big difference if we load just
        # one message or a batch of message startup time wise.
        self.loadMoreRoomMessages(room)

    def _delRoom(self, rid):

        if self.m_selected_room.getID() == rid:
            hint_index = self._getJoinedRoomIndex(rid)

        try:
            sub_id = self.m_room_msg_subscriptions.pop(rid)
            self.m_comm.unsubscribe(sub_id)
        except KeyError:
            self.m_logger.warning("attempt to delete room not yet subscribed to? " + str(rid))
        self.m_rooms.pop(rid)

        if self.m_selected_room.getID() == rid:
            self.selectAnyRoom(hint_index)

    def _getJoinedRoomIndex(self, rid):
        for index, room in enumerate(self.getJoinedRooms()):
            if room.getID() == rid:
                return index

        return None

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

        def userLoadProgress(so_far, total):
            self.m_callbacks.loadUsersInProgress(so_far, total)

        for info in self.m_comm.getUserList(progress_cb=userLoadProgress):
            self._cacheUserInfo(info)

        self.m_user_list_cached = True

    def _cacheMessage(self, room, msg):
        # in any case cache the new message so we always have the most recent version
        msg_map = self.m_room_msg_ids.setdefault(room.getID(), {})
        msg_map[msg.getID()] = msg

        user_info = msg.getUserInfo()
        self._cacheUserInfo(user_info)
        self._cacheRoomMember(room, user_info)

    def _cacheUserInfo(self, info):
        self.m_basic_user_infos.setdefault(info.getID(), info)
        self.m_username_id_map.setdefault(info.getUsername(), info.getID())

    def _selectFromRoomList(self, offset):
        """Selects a new room from the room list relative to the currently
        selected room.

        :param int offset: Positive or negative integer that determines which
                           room to select relative to the currently selected
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

    def _subscribeRoomEvents(self, room):
        room_msgdeleted_callback = functools.partial(
                self._forwardEventAsync, callback=self._roomMessageDeleted)

        sub_id = self.m_comm.subscribeForRoomEvents(room, "deleteMessage", room_msgdeleted_callback)
        sub_list = self.m_room_event_subscriptions.setdefault(room.getID(), [])
        sub_list.append(sub_id)

    def _unsubscribeRoomEvents(self, room):
        subscriptions = self.m_room_event_subscriptions.pop(room.getID())
        for sub in subscriptions:
            self.m_comm.unsubscribe(sub)

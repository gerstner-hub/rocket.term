# vim: ts=4 et sw=4 sts=4 :

import datetime
import logging
import time

# rocket.term
import rocketterm.realtime
import rocketterm.rest
import rocketterm.types
import rocketterm.utils


class RocketComm:
    """An abstraction layer that merges the available Rocket.Chat APIs into a
    single interface.

    Application logic should only ever access this interface here to perform
    Rocket.Chat operations. You need to call connect() and login() before any
    other functions can be used.
    """

    def __init__(self, server_uri, login_data):
        """:param login_data: An instance of PasswordLoginData or
        TokenLoginData to be used during login()."""
        self._reset()
        self.m_logger = logging.getLogger("comm")
        self.m_server_uri = server_uri
        self.m_login_data = login_data

        self.m_rt_session = rocketterm.realtime.RealtimeSession(server_uri)
        self.m_rest_session = rocketterm.rest.RestSession(server_uri)

    def _reset(self):
        self.m_logged_in = False
        # this will store subscriptions IDs as keys as they are returned from
        # RealtimeSession.subscribe and map them to EventSubscription
        # instances. This is needed to keep track of callbacks to be invoked
        # upon events and for being able to unsubscribe from event
        # subscriptions later on.
        self.m_subscriptions = {}

    def _subscriptionCB(self, sub_state, collection, event, data):
        """Generical callback function that is called for all type of realtime
        API subscription events.

        This function will forward the event to more specialized callback
        handlers and finally to the registered client callbacks.
        """

        client_cb = self.m_subscriptions[sub_state.getSubID()]

        if collection == "stream-room-messages":
            self._streamRoomMessagesCB(client_cb, collection, event, data)
        elif collection == "stream-notify-user":
            self._streamNotifyUserCB(client_cb, collection, event, data)
        elif collection == "stream-notify-logged":
            self._streamNotifyLoggedCB(client_cb, collection, event, data)
        elif collection == "stream-notify-room":
            self._streamNotifyRoomCB(client_cb, collection, event, data)
        else:
            raise Exception("Yet unsupported event collection")

    def _streamRoomMessagesCB(self, client_cb, collection, event, data):
        # split-up each message into its own callback event
        for item in data:
            info = rocketterm.types.RoomMessage(item)
            client_cb(collection, event, info)

    def _streamNotifyUserCB(self, client_cb, collection, event, data):
        change, item = data
        _id, evname = event.split('/', 1)

        if evname == "subscriptions-changed":
            info = rocketterm.types.SubscriptionInfo(item)
        elif evname == "rooms-changed":
            # this is the room-only data without the subscription
            # part, so we can't create a new room object with it
            info = item
        else:
            raise Exception("Yet unsupported notify-user event")

        client_cb(collection, change, event, info)

    def _streamNotifyLoggedCB(self, client_cb, collection, event, data):
        if event == "user-status":
            for user_id, username, status, text in data:
                presence = rocketterm.utils.createUserPresenceFromStatusIndicator(status)
                status_event = rocketterm.types.UserStatusEvent(
                    user_id, username, presence, text
                )
                client_cb(collection, event, status_event)
        else:
            raise Exception("Yet unsupported notify-logged event")

    def _streamNotifyRoomCB(self, client_cb, collection, event, data):

        if collection != "stream-notify-room":
            self.m_logger.warn(f"stream-notify-room CB with unexpected collection {collection}")
            return

        parts = event.split('/', 1)

        if len(parts) != 2 or parts[1] != "deleteMessage":
            self.m_logger.warn(f"unsupported stream-notify-room event {event}")
            return

        # in this room a message got deleted
        room_id = parts[0]

        client_cb(room_id, data[0]["_id"])

    def _getUserInfo(self, resp):
        """Extract user information from API responses and returns a UserInfo
        instance for it."""

        user = resp["user"]

        if "lastlogin" in user:
            # seems to be for the locally logged in user
            ret = rocketterm.types.LocalUserInfo(user)
        else:
            ret = rocketterm.types.UserInfo(user)

        return ret

    def callREST_Get(self, endpoint):
        return self.m_rest_session._get(endpoint)

    def callREST_Post(self, endpoint, data):
        return self.m_rest_session._post(endpoint, data, convert_to_json=False)

    def callRealtimeMethod(self, method, params):
        return self.m_rt_session.methodCall(method, params)

    def setErrorCallback(self, callback):
        """Register a callback function that will be called if the API
        connection is lost."""

        def callbackForwarder(cb):
            cb()

        import functools
        rt_cb = functools.partial(callbackForwarder, cb=callback)
        self.m_rt_session.setErrorCallback(rt_cb)

    def connect(self):
        """Connect to the RC APIs.

        This process only creates a network connection to the remote server
        but does not authenticate yet."""
        self.m_rt_session.connect()

    def close(self):
        """Close all RC API sessions and reset all state."""
        self.m_rt_session.close()
        self.m_rest_session.close()
        self._reset()

    def isLoggedIn(self):
        """Returns whether the configured user is currently logged in on the
        server."""
        return self.m_rt_session.isLoggedIn() and self.m_rest_session.isLoggedIn()

    def login(self):
        """Performs a login operation on all required APIs.

        This call attempts to authenticate with the RC APIs using the login
        data specified during construction time. On error an exception is
        thrown.
        """

        self.m_rt_session.login(self.m_login_data.getRealtimeLoginParams())
        user_info = self.m_rest_session.login(self.m_login_data.getRESTLoginParams())

        self.m_our_user_id = user_info["_id"]
        self.m_email = user_info.get("email", None)
        self.m_full_name = user_info["name"]
        self.m_username = user_info["username"]

    def logout(self):
        """Performs a logout operation on all required APIs.

        This basically undoes the login() operation.
        """
        if self.m_login_data.needsLogout():
            self.m_rt_session.logout()
            self.m_rest_session.logout()
        self.m_email = None
        self.m_full_name = None

    def getServerURI(self):
        """Returns the configured server name."""
        return self.m_server_uri

    def getUsername(self):
        """Returns the username of the logged in user."""
        return self.m_username

    def getFullName(self):
        """Returns the full friendly name of the logged in user."""
        return self.m_full_name

    def getEmail(self):
        """Returns the mail address of the logged in user."""
        return self.m_email

    def getUserID(self):
        """Returns the unique user ID of the logged in user."""
        return self.m_our_user_id

    def getLoggedInUserInfo(self):
        """Returns a BasicUserInfo instance for the currently logged in
        user."""
        return rocketterm.types.BasicUserInfo.create(
            self.m_our_user_id,
            self.m_username,
            self.m_full_name
        )

    def getJoinedRooms(self):
        """Returns a list of room objects representing the rooms that the
        currently logged in user is subscribed to."""
        # we need to get two disctinct data items here, the joined rooms and
        # the subscription info to create sensible objects.
        rooms = self.m_rt_session.getJoinedRooms()['result']['update']
        subscriptions = self.m_rt_session.getSubscriptions()['result']['update']
        subscriptions = dict([(s['rid'], s) for s in subscriptions])
        ret = []

        for room in rooms:
            r = rocketterm.utils.createRoom(room, subscriptions[room['_id']])
            ret.append(r)

        return ret

    def getRoomMessages(self, room, num_msgs, older_than=None):
        """Retrieves the RoomMessage objects for the given room object.

        :param int num_msgs: maximum number of messages to return
        :param RoomMessage older_than: if present then only messages
                                       older than this one are returned.
        :return: a tuple of (msgs_remaining, [RoomMessage, ...])
        """

        # it seem the client creation date is used here
        # it's a bit unclear how this behaves wrt to threading, where
        # new messages can appear out of order.
        if older_than:
            max_ts = older_than.getRaw()['ts']['$date']
        else:
            max_ts = None

        history = self.m_rt_session.getRoomHistory(room.getID(), num_msgs, max_ts)

        remaining = history['result']['unreadNotLoaded']
        messages = [rocketterm.types.RoomMessage(m) for m in history['result']['messages']]

        return (remaining, messages)

    def getRoomMembers(self, room, max_items=50, offset=0):
        """Retrieves the UserInfo for the members of the given room object.
        This is only supported for group and channel room types, not for
        direct chats (for obvious reasons)."""

        if room.isPrivateChat():
            resp = self.m_rest_session.getGroupMembers(room.getID(), max_items, offset)
        elif room.isChatRoom():
            resp = self.m_rest_session.getChannelMembers(room.getID(), max_items, offset)
        else:
            raise Exception("Unsupported room type: {}".format(room.typeLabel()))

        members = resp["members"]
        total = resp["total"]

        return (total, [rocketterm.types.UserInfo(data) for data in members])

    def getUserList(self, progress_cb=None, retry_on_too_many_reqs=True):
        """Retrieves a full list of users on the server. Returns a list of
        BasicUserInfo instances.

        :param progress_cb: A callback function that is called for each chunk
            of users loaded from the server. It receives two parameters:
            number of users already loaded, total number of users to load.
        """

        offset = 0

        ret = []

        while True:
            try:
                # using a larger count here is necessary to avoiding hitting
                # rate limiting too early. with the default count of 50 and a
                # users list of ~1.500 users I am currently hitting a rate
                # limiting delay of ~50 seconds, which breaks user experience
                # considerably :-/
                resp = self.m_rest_session.getUserList(count=200, offset=offset)
            except rocketterm.types.HTTPError as e:
                if e.isForbidden():
                    raise rocketterm.types.ActionNotAllowed(
                            "your account is not allowed to list users on the server")
                raise Exception(f"code = {e.getCode()}")
            except rocketterm.types.TooManyRequests as err:
                if not retry_on_too_many_reqs:
                    raise

                if err.hasResetTime():
                    now = datetime.datetime.utcnow()
                    diff = err.getResetTime() - now
                    sleep_secs = diff.seconds
                    if sleep_secs < 0:
                        sleep_secs = 1.0
                else:
                    sleep_secs = 1.0

                time.sleep(sleep_secs)
                continue

            ret.extend([rocketterm.types.BasicUserInfo(info) for info in resp["users"]])

            offset += len(resp["users"])
            total_users = resp["total"]

            if progress_cb:
                progress_cb(offset, total_users)

            if offset >= total_users:
                break

        return ret

    def hideRoom(self, room):
        """Hides the given room object, but does not unsubscribe from it. This
        only changes the 'open' state of the room."""
        self.m_rt_session.hideRoom(room.getID())

    def openRoom(self, room):
        """Opens the given room object, which needs to be already subscribed
        to. This only changes the 'open' state of the room."""
        self.m_rt_session.openRoom(room.getID())

    def subscribeForRoomMessages(self, room, callback):
        """Subscribe for asynchronous notification of new room message
        events in the given room object.

        :param callback: A callback function that will receive the new message
                         as a parameter.
        :return: An EventSubscription instance that can be used in
                 unsubscribe() to stop asynchronous notifications again.
        """
        sub_state = self.m_rt_session.subscribe(
            "stream-room-messages",
            room.getID(),
            self._subscriptionCB
        )
        self.m_subscriptions[sub_state.getSubID()] = callback
        return sub_state

    def subscribeForUserEvents(self, category, user, callback):
        """Subscribe for asynchronous notification of user events like
        new room memberships / subscriptions.

        The ``callback`` function receives the following parameters:

        - category: This will equal the ``category`` parameter used here.
        - change_type: This will be a string like "updated", "removed",
                       "inserted".
        - event: This will a a string describing the event like
          "<event-id>/<event-type>".
        - data: This will be a dictionary containing the data that depends on
          the occured event, e.g. the room ID and further room information for
          the 'subscriptions-changed' category.

        The category of the event, the change_type
        which can be.  e.g. "updated" or "deleted" and the status_event

        :param category: The event category to subscribe for, see
                         RealtimeSession.NOTIFY_USER_EVENTS.
                         'subscriptions-changed' will deliver notifications
                         about new/removed room subscriptions. 'rooms-changed'
                         will deliver notifications about e.g. room open/hide
                         events.
        :param user: The user object to subscribe for.
        :param callback: A callback function that will receive the
                         asynchronous events. See the detailed description for
                         more information.
        :return: An EventSubscription instance that can be used in
                 unsubscribe() to stop asynchronous notifications again.
        """

        if category not in self.m_rt_session.NOTIFY_USER_EVENTS:
            raise Exception("Invalid user event category: {}".format(category))

        sub_state = self.m_rt_session.subscribe(
            "stream-notify-user",
            "{}/{}".format(user.getID(), category),
            self._subscriptionCB
        )
        self.m_subscriptions[sub_state.getSubID()] = callback
        return sub_state

    def subscribeForRoomEvents(self, room, category, callback):
        """Subscribe for asynchronous notification of events in rooms.

        Currently only the 'deleteMessage' category is supported. For this the
        callback arguments will be:

        - room_id: the ID of the room where a message was deleted.
        - msg_id: the ID of the message that was deleted.

        :param str category: The event category so subscribe for, see
                             RealtimeSession.ROOM_EVENTS.
        :param str callback: The callback function which will be called upon
                             an asynchronous event.
        """

        if category not in self.m_rt_session.ROOM_EVENTS:
            raise Exception(f"Invalid room event category: {category}")

        sub_state = self.m_rt_session.subscribe(
            "stream-notify-room",
            f"{room.getID()}/{category}",
            self._subscriptionCB
        )
        self.m_subscriptions[sub_state.getSubID()] = callback
        return sub_state

    def subscribeForLoggedEvents(self, category, callback):
        """Subscribe for asynchronous notification of events for
        "logged" users.

        It isn't fully clear what a "logged user" is from the RC
        documentation. It seems like it refers to all currently logged in
        users on the server. It seems not to be possible to limit the accounts
        one is interested in which means that this creates quite a lot of
        traffic for larger RC instances.

        The callback will receive three parameters:

        - category: This will equal the ``category`` parameter used here.
        - change_type: This will be a string like "updated", "removed",
                       "inserted".
        - event_data: This will be a UserStatusEvent instance for the
                      'user-status' category.

        :param str category: The event category to subscribe for, see
                         RealtimeSession.LOGGED_USERS_EVENTS. 'user-status'
                         will deliver events when a user's online status
                         changed. Other categories are currently not
                         supported.
        :param callback: The callback function which will be called upon an
                         asynchronous event. See the detailed description for
                         more information.
        :return: An EventSubscription instance that can be used in
                 unsubscribe() to stop asynchronous notifications again.
        """

        if category not in self.m_rt_session.LOGGED_USERS_EVENTS:
            raise Exception("Invalid logged users event category: {}".format(category))

        sub_state = self.m_rt_session.subscribe(
            "stream-notify-logged",
            category,
            self._subscriptionCB
        )
        self.m_subscriptions[sub_state.getSubID()] = callback
        return sub_state

    def unsubscribe(self, sub_state):
        """Unsubscribes from a previously established event subscrption.

        :param EventSubscription sub_state: The data that was previously
          returned from e.g. subscribeForRoomMessages or one of the other
          subscribe functions.
        """
        self.m_rt_session.unsubscribe(sub_state)
        self.m_subscriptions.pop(sub_state.getSubID())

    def sendMessage(self, room, msg, thread_id=None):
        """Sends a new chat message into a room object.

        On failure to send the message an exception is thrown. You should wait
        for a new room message event in the room (see
        subscribeForRoomMessage()) before rendering the message, because the
        server might change some aspects of the message before it is actually
        posted in the room.

        :param room: The room object where the new message should be sent to.
        :param str msg: The plaintext that comprises the new message.
        :param str thread_id: The optional thread this message should be a
                              part of.  This needs to be the thread root
                              message's id as returned from
                              RoomMessage.getID().
        """
        self.m_rt_session.sendMessage(room.getID(), msg, thread_id)

    def updateMessage(self, msg, new_text):
        """Updates the text of an existing chat message.

        :param RoomMessage msg: The RoomMessage object of the message whoose
        text should be changed.
        :param str new_text: The new message text to set for the message.
        """
        import copy
        new_msg = copy.deepcopy(msg)
        new_msg.setMessage(new_text)

        # NOTE: it seems we need to supply the full message object with
        # changed text, just passing '_id' and 'msg' causes an internal server
        # error.
        self.m_rt_session.updateMessage(new_msg.getRaw())

    def deleteMessage(self, msg):
        """Delete the message represented by the given RoomMessage object."""
        self.m_rt_session.deleteMessage(msg.getID())

    def uploadFileMessage(self, room, path, message=None, description=None,
                          filename=None, thread_id=None):
        """Upload a file attachment to the given room.

        :param room: The Room object where to post the file upload.
        :param str path: The local file path to upload.
        :param str message: An optional message text to add.
        :param str description: An optional file description to add.
        :param str filename: An optional explicit filename to give the uploaded file.
        :param str thread_id: An optional thread RoomMessage object to attach the file to.
        """
        if thread_id:
            # if this is a thread child message then use the parent thread ID,
            # otherwise the message's own ID.
            parent = thread_id.getThreadParent()
            thread_id = parent if parent else thread_id.getID()

        return self.m_rest_session.uploadFileMessage(
                room.getID(), path, filename,
                message, description,
                thread_id
        )

    def getUserInfoByID(self, uid):
        """Retrieves a UserInfo structure for the given user ID from the
        server."""
        resp = self.m_rest_session.getUserInfo(uid)
        return self._getUserInfo(resp)

    def getUserInfoByName(self, username):
        """Retrieves a UserInfo structure for the given username from the
        server."""
        resp = self.m_rest_session.getUserInfo(username=username)
        return self._getUserInfo(resp)

    def getUserInfo(self, user):
        """Retrieves a full UserInfo structure for the given partial UserInfo
        structure."""
        return self.getUserInfoByID(user.getID())

    def getUserStatus(self, user):
        """Retrieves a UserStatus structure for the given user object from the
        server."""
        ret = self.m_rest_session.getUserStatus(user.getID())

        return rocketterm.types.UserStatus(ret)

    def setUserStatus(self, status, message):
        """Sets a new user status for the currently logged in user.

        :param UserPresence status: A value from the UserPresence enum to set
                                    the status to.
        :param str message: The status message to be displayed for other
                            users.
        """
        self.m_rest_session.setUserStatus(status.value, message)

    def setRoomTopic(self, room, topic):
        """Sets a new room topic for the given room object.

        Note that changing the topic requires special permissions. If these
        permissions are not present then an ActionNowAllowed exception will be
        thrown.

        :param room: The room object for which to change the topic.
        :param str topic: The new string to set as topic.
        """
        self.m_rt_session.setRoomTopic(room.getID(), topic)

    def createDirectChat(self, user):
        """Creates a new direct chat room to talk to the given user.

        Direct chats are handled specially in RC. You cannot
        subscribe/unsubscribe them, you can only create them and afterwards
        all you can do is "hiding" them again.

        If the direct chat with the given ``user`` already exists then this
        call will still succeed and return the appropriate room information.

        :param UserInfo user: The UserInfo instance of the user to talk to.
        """
        resp = self.m_rt_session.createDirectChat(user.getUsername())
        return resp["result"]["rid"]

    def getRoomInfo(self, rid=None, room_name=None):
        """Retrieves a Room info object for the given room ID.

        :return: A specialization of RoomBase, depending on the room type.
        """

        if rid and room_name:
            raise Exception("only one parameter allowed")
        elif rid is None and room_name is None:
            raise Exception("missing one required parameter")

        try:
            data = self.m_rest_session.getRoomInfo(rid, room_name)
        except rocketterm.types.RESTError as e:
            raise Exception("Getting room info for {} failed: {}".format(
                rid if rid else room_name,
                e.getErrorReason()
            ))

        # we don't have any subscription data here, this method is rather for
        # rooms we're not subscribed to yet.
        return rocketterm.utils.createRoom(data, None)

    def getChannelList(self, progress_cb=None):
        """Retrieves the full list of open chat rooms on the server which can
        take a longer time.
        """

        offset = 0
        ret = []

        while True:
            try:
                resp = self.m_rest_session.getChannelList(offset=offset)
            except rocketterm.types.HTTPError as e:
                if e.isForbidden():
                    raise rocketterm.types.ActionNotAllowed(
                            "you account is not allowed to list channels on the server")
                raise

            channels = resp["channels"]

            for channel in channels:
                # here again there's no subscription data available, maybe we
                # should find another approach that models subscribed rooms
                # differently than just "rooms"
                ret.append(rocketterm.utils.createRoom(channel, None))

            offset += len(channels)
            total_channels = resp["total"]

            if progress_cb:
                progress_cb(offset, total_channels)

            if offset >= total_channels:
                break

        return ret

    def getRoomDiscussions(self, room):
        """Retrieves a list of PrivateGroup objects representing the
        discussions in the given room."""

        total, discussions = self.m_rest_session.getDiscussions(room.getID())

        if total > len(discussions):
            self.m_logger.warning("Didn't fetch full amount of discussions")

        # here we don't have subscription information, because we're not
        # necessarily subscribed to all the existing discussions
        return [rocketterm.utils.createRoom(data, None) for data in discussions]

    def getRoomRoles(self, room):
        """Retrieves a list of tuples of (UserInfo, [roles]) describing the
        users with special roles in given room.

        There exist various default roles like 'owner' and 'moderator' in
        rocket chat."""

        ret = []
        reply = self.m_rt_session.getRoomRoles(room.getID())

        for info in reply['result']:
            ret.append((rocketterm.types.UserInfo(info['u']), info['roles']))

        return ret

    def inviteUserToRoom(self, room, user):
        """Invites the user identified by the given UserInfo into the room
        identified by the given RoomInfo.

        This is not possible for DirectChat rooms.

        "Inviting" in this context means actually adding a user to the room.
        The user will not have to accept the invitation or anything of the
        likes.
        """

        from rocketterm.types import PrivateChat, ChatRoom

        room_types = {
            PrivateChat: self.m_rest_session.inviteToGroup,
            ChatRoom: self.m_rest_session.inviteToChannel
        }

        for _type, fct in room_types.items():
            if not isinstance(room, _type):
                continue

            return fct(room.getID(), user.getID())
        else:
            raise Exception("unsupported room type to invite user to")

    def kickUserFromRoom(self, room, user):
        """Removes the user identifier by the given UserInfo from the room
        identified by the given RoomInfo.

        This is not possible for DirectChat rooms.
        """

        from rocketterm.types import PrivateChat, ChatRoom

        room_types = {
            PrivateChat: self.m_rest_session.kickFromGroup,
            ChatRoom: self.m_rest_session.kickFromChannel
        }

        for _type, fct in room_types.items():
            if not isinstance(room, _type):
                continue

            return fct(room.getID(), user.getID())
        else:
            raise Exception("unsupported room type to kick user from")

    def createRoom(self, name, initial_users=[]):
        """Creates a new private group or open chat room.

        :param str name: The name of the new room with type prefix.
        :param list initial_users: A list of usernames to initially add to the new room.
        :return str: The room ID of the newly created room object.
        """

        from rocketterm.types import PrivateChat, ChatRoom

        room_types = {
            PrivateChat.typePrefix(): self.m_rt_session.createPrivateGroup,
            ChatRoom.typePrefix(): self.m_rt_session.createChannel
        }

        for prefix, fct in room_types.items():
            if not name.startswith(prefix):
                continue

            reply = fct(name[1:], initial_users)
            return reply['result']['rid']
        else:
            raise Exception(f"unsupported or missing room type in label {name}")

    def leaveRoom(self, room):
        """Removes the logged in users subscription from the given room."""

        try:
            if room.isChatRoom():
                self.m_rest_session.leaveChannel(room.getID())
            elif room.isPrivateChat():
                self.m_rest_session.leaveGroup(room.getID())
            else:
                raise Exception("You cannot leave this room type, you can only hide it.")
        except rocketterm.types.RESTError as e:
            raise Exception("Leaving {} failed: {}".format(
                room.getLabel(),
                e.getErrorReason()
            ))

    def eraseRoom(self, room):
        """Deletes the given room object (open chat room or private group)
        permanently.

        Direct chats cannot be erased, they can only be hidden. You also need
        proper permissions to perform this operation.
        """

        self.m_rt_session.eraseRoom(room.getID())

    def joinChannel(self, room):
        """Let the logged in user join the given channel and add it to her
        subscriptions."""

        try:
            self.m_rest_session.joinChannel(room.getID())
        except rocketterm.types.RESTError as e:
            raise Exception("Joining {} failed: {}".format(
                room.getLabel(),
                e.getErrorReason()
            ))

    def markRoomAsRead(self, room):
        """Marks any unread messages in the given room as read."""

        self.m_rest_session.subscriptionsRead(room.getID())

    def getCustomEmojiList(self):
        """Returns a list of EmojiInfo instances representing the known
        custom emojis on the server."""

        emojis = self.m_rt_session.listCustomEmojis()

        return [rocketterm.types.EmojiInfo(d) for d in emojis['result']]

    def addReaction(self, msg, reaction):
        self.m_rt_session.setReaction(msg.getID(), reaction, True)

    def delReaction(self, msg, reaction):
        self.m_rt_session.setReaction(msg.getID(), reaction, False)

    def setMessageStar(self, msg):
        self.m_rt_session.starMessage(msg.getID(), msg.getRoomID(), True)

    def delMessageStar(self, msg):
        self.m_rt_session.starMessage(msg.getID(), msg.getRoomID(), False)

    def getServerInfo(self):
        info = self.m_rest_session.getInfo()
        return rocketterm.types.ServerInfo(info)

    def getMessageByID(self, msg_id):
        msg = self.m_rest_session.getMessage(msg_id)
        return rocketterm.types.RoomMessage(msg['message'])

    def downloadFile(self, file_info, outfile):
        self.m_rest_session.downloadFile(file_info.getSubURL(), outfile)

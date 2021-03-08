# vim: ts=4 et sw=4 sts=4 :

import copy
import json
import logging
import pprint
import threading

# rocket.term
import rocketterm.types

# 3rd party
import websocket


class RealtimeSession:
    """Interface to the Rocket.Chat Realtim API via websocket ws:// protocol.

    Contrary to the REST API, the Realtime API isn't a full featured. But it
    allows to establish subscriptions for asynchronous events. So this API is
    needed get notifications about state changes in chat rooms, for users etc.
    """

    NOTIFY_USER_EVENTS = (
        "message", "otr", "webrtc",
        "notification", "rooms-changed",
        "subscriptions-changed"
    )

    LOGGED_USERS_EVENTS = (
        "Users:NameChanged",
        "Users:Deleted",
        "updateAvatar",
        "updateEmojiCustom",
        "deleteEmojiCustom",
        "roles-change",
        "user-status"
    )

    ROOM_EVENTS = (
        "deleteMessage",
        "typing"
    )

    _CENSORED = "<CENSORED>"

    def __init__(self, server_uri):
        """:param str server: The DNS name and optional non-standard port of
        the remote server, without ws:// scheme prefix."""

        self.m_server_uri = server_uri
        self.m_logger = logging.getLogger("rtsocket")

        self._reset()
        self._checkVersion()

        self.m_ws = websocket.WebSocketApp(
            server_uri.getRealtimeURI(),
            on_message=self._wsMessage,
            on_error=self._wsError,
            on_close=self._wsClose,
            on_open=self._wsOpen
        )

    def _checkVersion(self):
        major, minor, _ = websocket.__version__.split('.')
        major = int(major)
        minor = int(minor)

        MIN_MAJOR = 0
        MIN_MINOR = 53

        # older versions of websocket-client have issues with the callback
        # signatures for _wsError & friends. Somewhen they added support for
        # class methods but it was strangely broken, some versions around 0.48
        # through 0.50 also worked but lets just stick to newer versions than
        # 0.53 at the moment to avoid trouble. See upstream issue #471.

        if major < MIN_MAJOR or (major == MIN_MAJOR and minor < MIN_MINOR):
            raise Exception(
                f"Your websocket-client module ({websocket.__version__}) is too old. "
                f"At least version {MIN_MAJOR}.{MIN_MINOR} is needed."
            )

    def _reset(self):
        """Resets all session state."""
        # since the websocket session can also be closed unexpectedly by the
        # remote server we need this flag to recognize expected and unexpected
        # close events.
        self.m_wait_for_close = False
        self.m_login_data = None
        self.m_logged_in = False
        # asynchronously received messages not yet consumed
        self.m_pending_replies = []
        # whether the web socket is open
        self.m_open = False
        # whether a connect cycle has been performed
        self.m_connected = False
        # if an asynchronous open error was received then it is stored in here
        # as a string
        self.m_open_error = None
        # thread running the web-app loop
        self.m_thread = None
        # for keeping track of method calls
        self.m_next_req_id = 1
        # for keeping track of subscriptions
        self.m_next_sub_id = 1
        # maps subscribed item IDs to a list of EventSubscription
        # instances
        self.m_sub_states = {}
        self.m_active_subs = set()
        # synchronization object for dealing with asynchronous replies
        # received from the websocket run loop
        self.m_condition = threading.Condition()
        self.m_error_cb = None

    def _wsMessage(self, message):
        """Called when a new websocket message is received.

        :param str message: plaintext message content.
        """
        try:
            data = json.loads(message)
        except Exception:
            self.m_logger.warning("Non-JSON message received:" + message)
            return

        if data.get("msg", "") == "ping":
            # this is a ping/ping protocol on websocket level that needs to be
            # fulfilled to prevent the connection being terminated
            try:
                self.m_ws.send(json.dumps({"msg": "pong"}))
            except Exception as e:
                self.m_logger.warning("failed to reply to ping:" + str(e))
        else:
            try:
                self._handleIncoming(data)
            except Exception as e:
                self.m_logger.error("failed to handle incoming message:" + str(e))

    def _wsError(self, error):
        """Called when an error on websocket level is received.

        :param str error: plaintext error message content.
        """
        self.m_logger.error("received error: {}".format(error))
        with self.m_condition:
            if not self.m_open:
                self.m_open_error = error

    def _wsClose(self):
        """Called when the websocket connection was closed locally or by the
        peer."""

        with self.m_condition:
            if not self.m_open or not self.m_wait_for_close:
                self.m_logger.error("API closed unexpectedly")
                call_error_cb = True
            else:
                self.m_logger.debug("API closed")
                call_error_cb = False

            self.m_wait_for_close = False
            self.m_open = False
            self.m_connected = False
            self.m_condition.notify()

        if call_error_cb and self.m_error_cb:
            self.m_error_cb()

    def _wsOpen(self):
        """Called when the websocket connection has been successfully
        established."""
        with self.m_condition:
            self.m_open = True
            self.m_condition.notify()

    def _handleIncoming(self, data):
        """Handle an incoming message.

        :param dict data: Decoded message data.
        """

        try:
            self._debugIncoming(data)

            if not self.m_connected and 'server_id' in data:
                # it is the initial server reply
                return
            elif self._checkForSubscriptionEvent(data):
                return

            with self.m_condition:
                self.m_pending_replies.append(data)
                self.m_condition.notify()
        except Exception as e:
            from rocketterm.utils import getExceptionContext
            self.m_logger.error(
                "Failed to process incoming message: {}.\nFailed message was: {}. Exception: {}".format(
                    str(e), data, getExceptionContext(e)
                )
            )

    def _getNewReqID(self):
        """Returns the next request ID to use for method calls."""
        ret = self.m_next_req_id
        self.m_next_req_id += 1
        return str(ret)

    def _getNewSubID(self):
        """Returns the next subscription ID to use for asynchronous
        notifications."""
        ret = self.m_next_sub_id
        self.m_next_sub_id += 1
        return str(ret)

    def _shouldDebug(self):
        # avoid performing expensive copying/formatting for each transfer when
        # logging is not active
        return self.m_logger.isEnabledFor(logging.DEBUG)

    def _debugRequest(self, req):
        if not self._shouldDebug():
            return

        # censor any login data during logging
        if req.get("method", "") == "login":
            req = copy.deepcopy(req)

            for params in req.get("params", []):
                if 'resume' in params:
                    params['resume'] = self._CENSORED
                elif 'password' in params:
                    params['password']['digest'] = self._CENSORED

        self.m_logger.debug("-> request {}".format(pprint.pformat(req)))

    def _debugIncoming(self, data):
        if not self._shouldDebug():
            return

        result = data.get("result", {})

        # censor any token/login data during logging
        if type(result) == dict and 'token' in result:
            data = copy.deepcopy(data)
            data["result"]["token"] = self._CENSORED

        self.m_logger.debug("<- incoming message {}".format(pprint.pformat(data)))

    def _waitForIncoming(self, cond):
        """Wait for an incoming message that matches the given predicate
        ``cond``.

        :param cond: A function that takes an incoming message structure as
                     argument and return a boolean indicator, whether the
                     caller is interested in it or not.
        :return dict: The message structure of a message that matched the
                      desired ``cond``.
        """
        with self.m_condition:
            while True:
                for i, msg in enumerate(self.m_pending_replies):
                    if cond(msg):
                        return self.m_pending_replies.pop(i)

                self.m_condition.wait()

    def _checkErrorReply(self, resp):
        """Checks a server response for error conditions and raises Exceptions
        as appropriate."""
        error = resp.get('error', None)
        if error:
            tag = error.get('error')

            if tag == "error-action-not-allowed":
                raise rocketterm.types.ActionNotAllowed(error)
            elif tag == "too-many-requests":
                raise rocketterm.types.TooManyRequests(error)
            else:
                raise rocketterm.types.MethodCallError(error)

        return resp

    def _checkForSubscriptionEvent(self, data):
        """Checks whether an incoming message belongs to event subscription
        handling."""
        msg = data.get("msg", None)
        if not msg:
            return False

        if msg == "ready" and "subs" in data:
            for sub in data['subs']:
                with self.m_condition:
                    existing = sub in self.m_active_subs
                # it's just a successful registration reply,
                # nothing else to do
                if not existing:
                    self.m_logger.warning(
                            "Received subscription 'ready' for unknown subscription {}".format(sub))
            return True
        elif msg == "nosub":
            # unsubscribe confirmation
            return True
        elif msg == "changed" and "fields" in data:
            self._handleSubscriptionEvent(data)
            return True

        return False

    def _handleSubscriptionEvent(self, data):
        """Handles a subscription event by calling registered callbacks."""
        # the subscription id in 'id' is worthless, see
        # subscribe(). Use the eventName to find subscribers.
        fields = data["fields"]
        item_id = fields["eventName"]

        with self.m_condition:
            states = self.m_sub_states.get(item_id, None)

            if not states:
                self.m_logger.warning(
                        "Received subscription event for unknown subscription for item ID {}".format(item_id)
                )
                return

            self._invokeCallbacks(states, data)

    def _invokeCallbacks(self, states, data):
        """Invokes the callbacks for the given EventSubscription objects for
        the given event message ``data``."""

        collection = data['collection']
        event = data['fields']['eventName']
        args = data['fields']['args']

        for state in states:
            cb = state.getCallback()
            try:
                cb(state, collection, event, args)
            except Exception as e:
                import traceback
                et = traceback.format_exc()
                self.m_logger.error("Subscription callback failed: {}\n{}\n".format(str(e), et))

    def setErrorCallback(self, callback):
        """Sets a callback function that will be called if the connection to
        the API is lost unexpectedly.

        The callback will receive no parameters.
        """
        self.m_error_cb = callback

    def request(self, data):
        """Sends the given request to the server.
        :param dict data: The data structure to send which will be JSON encoded.
        """
        self._debugRequest(data)

        self.m_ws.send(json.dumps(data))

    def receiveReply(self, req_id):
        """Synchronous wait for an asynchronous reply to the given request
        ID."""
        return self._waitForIncoming(lambda msg: msg.get("id", "") == req_id)

    def receiveMessage(self, _type):
        """Synchronous wait for an asynchronous reply of the given message
        type."""
        return self._waitForIncoming(lambda msg: msg.get("msg", "") == _type)

    def methodCall(self, method, params, check_error_reply=True):
        """Performs a method call and synchronously returns the result.

        :param str method: The method name to invoke.
        :param dict params: The parameter data structure to pass to the
                            method.
        :param bool check_error_reply: If True then on non-successful replies
                                       an Exception will be thrown.
        :return dict: The deserialized reply message.
        """

        if not isinstance(params, list):
            params = [params]

        req = {
            "msg": "method",
            "method": method,
            "id": self._getNewReqID(),
            "params": params
        }

        self.request(req)

        reply = self.receiveReply(req["id"])

        if check_error_reply:
            self._checkErrorReply(reply)

        return reply

    def connect(self):
        """Connects the websockets to the remote server."""

        if self.m_connected or self.m_open:
            raise Exception("Already open/connected")

        self.m_thread = threading.Thread(target=self.m_ws.run_forever)
        self.m_thread.start()

        with self.m_condition:
            while not self.m_open:
                if self.m_open_error:
                    raise Exception("Realtime API connection failed: " + str(self.m_open_error))
                self.m_condition.wait()

        connect_msg = {
            "msg": "connect",
            "version": "1",
            "support": ["1"]
        }

        self.request(connect_msg)

        reply = self.receiveMessage("connected")
        _ = reply["session"]
        self.m_connected = True

    def close(self):
        """Closes the API connection and resets any session state."""

        self.m_logger.debug("Closing API")

        with self.m_condition:
            self.m_wait_for_close = True

        self.m_ws.close()

        if self.m_thread:
            self.m_thread.join()

        self._reset()

    def login(self, login_data):
        """Logs into the realtime API using the given login data.

        :param login_data: An instance of TokenLoginData or PasswordLoginData.
        """
        resp = self.methodCall("login", login_data, check_error_reply=False)

        if 'error' in resp:
            error = resp['error']
            raise rocketterm.types.LoginError(
                    "Failed to login to realtime API: Error {}: {}".format(
                        error['error'], error['message']
                    )
            )
        _ = resp['result']

        self.m_login_data = login_data
        self.m_logged_in = True

    def logout(self):
        if not self.m_logged_in:
            return

        # it looks like logging out the realtime API is not really
        # needed, at least its not fully documented and nobody seems
        # to call it
        self.m_logged_in = False
        self.m_login_data = None

    def isLoggedIn(self):
        return self.m_logged_in

    def subscribe(self, name, item_id, callback):
        """Subscribe for asynchronous event notification.

        The callback function receives the following parameters:

        - sub_state: the EventSubscription that is returned from this function
          call for identification of the subscription for which a event was
          received.
        - collection: a string identifying the data collection the event
          refers to. This will be the same as the ``name`` parameter passed to
          this function.
        - event: a string describing the event that occured which is dependant
          upon the collection.
        - data: the event data which is dependant upon the event type.

        :param str name: The name of the event to subscribe for.
        :param str item_id: the ID of the object to receive events for e.g. a
                            room or user ID.
        :param callback: A function to call for each asynchronous event.
        :return EventSubscription: A reference to the newly created
                   subscription that will be passed to callbacks and can be
                   used during unsubscribe() again.
        """

        sub_id = self._getNewSubID()

        data = {
            "msg": "sub",
            "id": sub_id,
            "name": name,
            "params": [
                item_id,
                False
            ]
        }

        # this subscription API is half-broken somehow, see
        # https://github.com/RocketChat/Rocket.Chat/issues/9917
        #
        # the subscription ID used is useless, therefore we need to
        # fall back to the item_id instead. Since multiple clients can
        # subscribe for the same item ID we keep a list of subscribers
        # for each item_id in m_sub_states.
        new_state = rocketterm.types.EventSubscription(sub_id, item_id, callback)

        with self.m_condition:

            self.request(data)

            states = self.m_sub_states.setdefault(item_id, [])
            states.append(new_state)
            self.m_active_subs.add(sub_id)

        return new_state

    def unsubscribe(self, sub_state):
        """Unsubscribes from an asynchronous event source that was previously
        subscribed to via subscribe().

        :param EventSubscription sub_state: The state object that was
        previously returned from subscribe().
        """

        with self.m_condition:
            states = self.m_sub_states[sub_state.getItemID()]
            states.pop(states.index(sub_state))
            self.m_active_subs.discard(sub_state.getSubID())

        data = {
            "msg": "unsub",
            "id": sub_state.getSubID()
        }

        self.request(data)

    def sendMessage(self, rid, msg, thread_id=None):
        """Sends a new message into the given room ID.

        :param str rid: The room ID to send a new message to.
        :param str msg: The message plaintext to send.
        :param str thread_id: The optional thread ID to reply to. Needs to be
                              the root message of a thread.
        """

        msg = rocketterm.types.RoomMessage.createNew(
                rid, msg, thread_id=thread_id
        )
        return self.methodCall("sendMessage", msg.getRaw())

    def updateMessage(self, msg):
        """Updates an existing chat message with new data like the message
        text.

        :param dict msg: The raw message data structure that needs at least a
        valid message _id
        """
        return self.methodCall("updateMessage", msg)

    def deleteMessage(self, msg_id):
        """Delete the chat message with the given ID."""

        params = {
            "_id": msg_id
        }

        return self.methodCall("deleteMessage", params)

    def getJoinedRooms(self, changes_since_ts=0):
        """Returns a sequence of data structures representing the
        different rooms the logged in user is a member of.

        :param int changes_since_ts: Optional change date parameter. If
                                     supplied then only changes in joined
                                     rooms since the given timestamp
                                     are returned.
        :return list: Sequence of dictionaries that describe the joined rooms.
        """

        params = {
            "$date": changes_since_ts
        }

        return self.methodCall("rooms/get", params)

    def getSubscriptions(self, changes_since_ts=0):
        """Returns a list of Subscriptions that the logged in user
        has.

        :param int changes_sice_ts: Optional change data parameter. See
                                    getJoinedRooms().

        :return list: Sequence of dictionaries that describe the subscriptions.
        """

        params = {
            "$date": changes_since_ts
        }

        return self.methodCall("subscriptions/get", params)

    def getRoomHistory(self, rid, num_msgs, max_ts=None, last_update=None):
        """Returns a number of chat messages for the given room ID.

        :param int num_msgs: maximum number of messages that will be
               returned.
        :param int max_ts: newest timestamp of messages to return.
                           Only older messages will be returned.
        :param int last_update: timestamp the client queried data from
                                this channel the last time. Unclear what this
                                is for.
        :return: raw dictionary data structure with reply data
        """

        if max_ts:
            max_ts = {"$date": max_ts}

        params = [rid, max_ts, num_msgs, {"$date": last_update}]

        return self.methodCall("loadHistory", params)

    def getRoomRoles(self, rid):
        """Returns a list of dictionary entries describing special user roles
        in the given room."""

        params = [rid]

        return self.methodCall("getRoomRoles", params)

    def hideRoom(self, rid):
        """Hides the given room ID.

        Only rooms that the user are subscribed for and that are currently
        open can be hidden. This only sets a state flag that makes the room
        disappear from the room list for the user. The user will remain a
        member of the room and can e.g. still be mentioned, which will cause
        it to be displayed again.
        """

        params = [rid]

        return self.methodCall("hideRoom", params)

    def openRoom(self, rid):
        """Opens the given room ID.

        This performs the inverse operation of hideRoom(). Only rooms that the
        user is currently subscribed for can be opened.
        """

        params = [rid]

        return self.methodCall("openRoom", params)

    def eraseRoom(self, rid):
        """Deletes a public channel or private group permanently.

        This requires proper user permissions to do so.
        """

        return self.methodCall("eraseRoom", [rid])

    def setUserPresence(self, status):
        """Sets the user presence status of the currently logged in
        user.

       :param rocketterm.types.UserPresence status: The new presence state to
                                                    set.
       """

        params = [status.value]

        return self.methodCall("UserPresence:setDefaultStatus", params)

    def setRoomTopic(self, rid, topic):
        """Changes the room topic for the given room ID.

        Note that changing a room's topic requires special room permissions.
        If they are missing an ActionNotAllowed error is raised.

        :param str topic: The new topic string to set.
        """

        params = [rid, "roomTopic", topic]

        return self.methodCall("saveRoomSettings", params)

    def createDirectChat(self, username):
        """Creates a new direct chat to communicate with the given username.

        A direct chat behaves a bit peculiar in RC. Once it is created you
        cannot unsubscribe from it but you can only hide it. "Recreating" it
        is possible, which will simply return the existing room ID.

        :param str username: The username to create the direct chat for.
        """

        params = [username]

        return self.methodCall("createDirectMessage", params)

    def createChannel(self, name, initial_users=[], read_only=False):
        """Creates a new open channel of the given name.

        :param list initial_users: a list of usernames to be added to the new
                                   channel.
        :param bool read_only: whether the new channel should be read-only.
        """

        params = [name, initial_users, read_only]

        return self.methodCall("createChannel", params)

    def createPrivateGroup(self, name, initial_users=[]):
        """Creates a new private group of the given name.

        :param list initial_users: a list of usernames to be added to the new
                                   private group.
        """

        params = [name, initial_users]

        return self.methodCall("createPrivateGroup", params)

    def listCustomEmojis(self):
        """Returns a list of all custom emoji names the server knows about."""

        return self.methodCall("listEmojiCustom", [])

    def setReaction(self, msg_id, reaction, add=True):
        """Adds or removes a reaction to/from the given message ID.

        :param str reaction: A string like ':crying:', see listEmojis().
        :param bool add: If set then the reaction will be added, otherwise it
                         will be removed.
        """

        return self.methodCall("setReaction", [reaction, msg_id, add])

    def starMessage(self, msg_id, room_id, star_it=True):
        """Adds or removes a message star.

        From RC docs:

        Starring allows a user to quickly save for future reference, or
        something similar, for their own personal usage.
        """

        args = {
            "_id": msg_id,
            "rid": room_id,
            "starred": star_it
        }

        return self.methodCall("starMessage", [args])

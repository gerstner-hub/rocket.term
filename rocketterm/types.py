# vim: ts=4 et sw=4 sts=4 :

# a collection of various simple data structures and types used across
# rocket.term. Most of these are modelled around REST or Realtime API data
# JSON data structures.

from enum import Enum

from rocketterm.utils import rcTimeToDatetime


class MethodCallError(Exception):

    def __init__(self, error, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.m_error = error

    def reason(self):
        return self.m_error.get('reason', 'unknown')


class ActionNotAllowed(Exception):

    def __init__(self, error, *args, **kwargs):
        super().__init__(error, *args, **kwargs)
        self.m_error = error


class LoginError(Exception):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class HTTPError(Exception):

    def __init__(self, code, *args, **kwargs):

        self.m_code = code
        super().__init__(*args, **kwargs)

    def getCode(self):
        return self.m_code

    def isForbidden(self):
        return self.m_code == 403


class _RoomTypeMixin:

    def getType(self):
        return self.m_data["t"]

    def isDirectChat(self):
        return self.getType() == 'd'

    def isChatRoom(self):
        return self.getType() == 'c'

    def isPrivateChat(self):
        return self.getType() == 'p'


class SubscriptionInfo(_RoomTypeMixin):

    def __init__(self, data):
        self.m_data = data

    def getName(self):
        return self.m_data['name']

    def getRoomID(self):
        return self.m_data['rid']

    def getRaw(self):
        return self.m_data

    def isOpen(self):
        """DirectChat is a bit strange in Rocket.Chat. You can't
        actually leave one, you can only "archive", i.e. hide it. This
        open field will determine whether it's been "hidden"."""
        return self.m_data['open']


class RoomBase(_RoomTypeMixin):
    """Base type for all room types."""

    def __init__(self, room_data, subscription_data):

        self.m_data = room_data
        self.m_subscription = SubscriptionInfo(subscription_data)

    @classmethod
    def supportsMembers(cls):
        """Whether this room type supports a member list."""
        return True

    def getRaw(self):
        return self.m_data

    def setRaw(self, data):
        self.m_data = data

    def getSubscription(self):
        return self.m_subscription

    def setSubscription(self, ss):
        self.m_subscription = ss

    def getFriendlyName(self):
        """Attempts to retrieve a friendly name for this room.

        If there is no friendly name then this falls back to the unfriendly
        name."""
        try:
            return self.m_data['fname']
        except KeyError:
            return self.getName()

    def getID(self):
        return self.m_data["_id"]

    def __eq__(self, other):
        if other is None:
            return False
        return self.getID() == other.getID()

    def __ne__(self, other):
        return not self.__eq__(other)

    def isOpen(self):
        """This reflects the show/hidden state for room objects that
        we're subscribed to."""
        return self.m_subscription.isOpen()

    def matchesRoomSpec(self, spec):
        """Returns whether this room matches the given room label like
        $my_group or #my_channel or @my_direct_chat."""

        if len(spec) < 2 or spec[0] != self.typePrefix():
            return False

        return self.getName() == spec[1:]

    def supportsTopic(self):
        """Returns whether this room type supports setting a topic."""
        return self.isChatRoom() or self.isPrivateChat()


class DirectChat(RoomBase):
    """A direct chat between two users. Has not additional attributes over
    RoomBase."""

    @classmethod
    def typePrefix(cls):
        return '@'

    @classmethod
    def typeLabel(self):
        return "direct chat"

    @classmethod
    def supportsMembers(cls):
        return False

    def getName(self):
        # the direct chat name needs to be fetched from the accompanying
        # subscription
        return self.m_subscription.getName()

    def getPeerUserID(self, our_user_info):
        """Returns the user ID of the user this direct chat is for. To
        determine this, the Userinfo of the currently logged in user is
        necessary."""
        # this is another dark corner of the API. The 'fname' of the
        # DirectChat contains the friendly name of the user, the
        # 'name' the username of the user but that's about it. No
        # sensible way to deduct the actual user ID ... so we'd need
        # to map the username to the userID, this is not possible
        # using the realtime API, only using the REST API. The REST
        # API has pretty heavy DoS restrictions that we might hit here
        # ... instead we use a hack to do that:
        #
        # the room ID of DirectChat objects is the concatenation of
        # the two user IDs involved. The order of them is undefined,
        # however (or has to do with sorting).
        # This is probably undocumented API but suits us well here.
        rid = self.getID()
        our_id = our_user_info.getID()

        parts = rid.split(our_id)
        if len(parts) != 2:
            if len(rid) == len(our_id) and self.getName() == our_user_info.getUsername():
                # strange special case when we're chatting with ourselves...
                # the room-id is only half the length but it does not match
                # our user-id in this case but something else?
                return our_id
            raise Exception(
                    "Failed to determine DirectChat '{}' peer userid from {} (ours = {})".format(
                        self.getName(), rid, our_user_info.getID()
                    )
            )

        for part in parts:
            if not part:
                continue
            return part


class ChatRoom(RoomBase):
    """An open chatroom that everyone can join."""

    @classmethod
    def typePrefix(cls):
        return '#'

    @classmethod
    def typeLabel(self):
        return "chat room"

    def getName(self):
        return self.m_data["name"]

    def getTopic(self):
        return self.m_data.get("topic", "N/A")

    def getCreator(self):
        return BasicUserInfo(self.m_data.get('u'))


class PrivateChat(ChatRoom):
    """A private chatroom that resembles ChatRoom."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def typePrefix(cls):
        return '$'

    @classmethod
    def typeLabel(self):
        return "private group"

    def isReadOnly(self):
        return self.m_data.get("ro")

    def getTopic(self):
        return self.m_data.get("topic", "N/A")

    def isDiscussion(self):
        """Discussions are sub-rooms that are modelled as private chats.

        Discussions have no nice names from getName(), use getFriendlyName()
        instead.
        """
        return 'prid' in self.m_data

    def getDiscussionParentRoomID(self):
        return self.m_data.get('prid', None)


ROOM_TYPES = (PrivateChat, DirectChat, ChatRoom)


class BasicUserInfo:
    """BasicUserInfo only contains minimum information and is sent along
    with messages and such.

    The full UserInfo is returned for an explicit user info lookup."""

    def __init__(self, data):

        self.m_data = data

    @classmethod
    def create(cls, uid, username, name):
        data = {
            "_id": uid,
            "username": username,
            "name": name
        }
        return BasicUserInfo(data)

    @classmethod
    def typePrefix(cls):
        return '@'

    def getID(self):
        return self.m_data["_id"]

    def getUsername(self):
        return self.m_data["username"]

    def getFriendlyName(self):
        try:
            return self.m_data["name"]
        except KeyError:
            return self.getUsername()

    def __eq__(self, other):
        return self.getID() == other.getID()

    def __ne__(self, other):
        return not self.__eq__(other)


class UserInfo(BasicUserInfo):

    def getStatus(self):
        return UserPresence(self.m_data["status"])

    def getUTCOffset(self):
        return self.m_data.get("utcOffset", 0)

    def isActive(self):
        return self.m_data["active"]


class LocalUserInfo(UserInfo):
    """This user type additionally holds information only available for
    the own logged in user."""

    def getLastLogin(self):
        return rcTimeToDatetime(self.m_data["lastLogin"])

    def getRoles(self):
        return self.m_data["roles"]


class UserStatus:
    """This type is returned from the REST API users.getStatus."""

    def __init__(self, data):
        self.m_data = data

    def getMessage(self):
        return self.m_data.get("message", "")

    def getStatus(self):
        return UserPresence(self.m_data["status"])

    def getConnectionStatus(self):
        """This status modelling is a bit strange. The UserPresence
        can be online/offline independently of the actual connection
        status. It seems to be only returned for the own user account
        which is kind of senseless, since it is clear that we're
        online ourselves when we're talking to the API ..."""
        return self.m_data.get("connectionStatus", "")


class UserStatusEvent:
    """This type is returned from stream-notify-logged user-status
    asynchronous events and it describes a user status change."""

    def __init__(self, user_id, username, presence, status_text):
        self.m_user_id = user_id
        self.m_username = username
        self.m_presence = presence
        # this text is sometimes None and sometimes '' so let's
        # harmonize it
        self.m_status_text = status_text if status_text else ""

    def getUserID(self):
        return self.m_user_id

    def getUsername(self):
        return self.m_username

    def getUserPresenceStatus(self):
        return self.m_presence

    def getStatusText(self):
        return self.m_status_text


class UserPresence(Enum):

    Online = "online"
    Busy = "busy"
    Away = "away"
    Offline = "offline"


class EventSubscription:
    """The state kept for asynchronous event subscriptions registered at the
    Realtime API."""

    def __init__(self, sub_id, item_id, callback):
        self.m_sub_id = sub_id
        self.m_item_id = item_id
        self.m_callback = callback

    def getSubID(self):
        """Returns the unique subscription ID."""
        return self.m_sub_id

    def getItemID(self):
        """Returns the item the subscription is for (e.g. room ID, user
        ID)."""
        return self.m_item_id

    def getCallback(self):
        """The callback to be invoked when this event occurs."""
        return self.m_callback

    def __eq__(self, other):
        return self.getSubID() == other.getSubID()

    def __ne__(self, other):
        return not self.__eq__(other)


class PasswordLoginData:
    """This type holds information for password based authentication at RC
    APIs."""

    def __init__(self, username, password):
        self.m_username = username
        self.m_digest_alg = "sha-256"
        self.m_passwd_hexdigest = self._calcPasswordDigest(password)
        # the REST API still needs the cleartext password, not very
        # consistent :-/
        self.m_passwd_cleartext = password

    def _calcPasswordDigest(self, password):
        import hashlib
        h = hashlib.sha256()
        h.update(password.encode())
        return h.hexdigest()

    def _getPasswdHexDigest(self):
        return self.m_passwd_hexdigest

    def _getDigestAlg(self):
        return self.m_digest_alg

    def getUsername(self):
        return self.m_username

    def getRealtimeLoginParams(self):

        return {
            "user": {"username": self.getUsername()},
            "password": {
                "algorithm": self._getDigestAlg(),
                "digest": self._getPasswdHexDigest()
            }
        }

    def getRESTLoginParams(self):
        return {
            "user": self.m_username,
            "password": self.m_passwd_cleartext
        }

    def needsLogout(self):
        return True


class TokenLoginData:
    """This type holds information for OAUTH token based authentication at RC
    APIs."""

    def __init__(self, access_token):
        self.m_token = access_token

    def _getAccessToken(self):
        return self.m_token

    def getRealtimeLoginParams(self):
        return {
            "resume": self._getAccessToken()
        }

    def getRESTLoginParams(self):
        return {
            "resume": self._getAccessToken()
        }

    def needsLogout(self):
        # on REST API level it seems that when we're using an oauth
        # token for authentication then "logging out" means to delete
        # the token forever. That is not what we want. Therefore don't
        # logout when using this mechanism.
        return False


class URLMeta:
    """Metadata for URLs that is sent by RC when URLs are included in chat
    messages."""

    def __init__(self, data):
        self.m_data = data

    def getDescription(self):
        return self.m_data.get("description", "")

    def getTitle(self):
        return self.m_data.get("pageTitle", "")


class URLInfo:

    def __init__(self, data):
        self.m_data = data

    def _getData(self):
        return self.m_data

    def getHeaders(self):
        return self.m_data.get("headers", {})

    def getMeta(self):
        ret = self.m_data.get("meta", {})
        return URLMeta(ret) if ret else None

    def hasMeta(self):
        return self.getMeta() is not None

    def getURL(self):
        return self.m_data["url"]


class FileInfo:
    """Information about file attachments that can be part of RC chat
    messages."""

    def __init__(self, data):
        self.m_data = data

    def getID(self):
        return self.m_data['_id']

    def getName(self):
        return self.m_data['name']

    def getMIMEType(self):
        return self.m_data['type']


class MessageType(Enum):
    """This models the undocument message type found in RoomMessage
    objects in field 't'.

    Part of this is found in upstream MessageTypes.js, but it seems not to
    contain all types. Look for invocations of method with the name pattern
    'create.*Room.*\\(\''.
    """
    RoomNameChanged = "r"
    UserAddedBy = "au"
    UserRemovedBy = "ru"
    UserLeft = "ul"
    UserJoined = "uj"
    UserJoinedConversation = "ut"
    WelcomeMessage = "wm"
    MessageRemoved = "rm"
    RenderRtcMessage = "rtc"
    UserMuted = "user-muted",
    UserUnmuted = "user-unmuted"
    SubscriptionRoleAdded = "subscription-role-added"
    SubscriptionRoleRemoved = "subscription-role-removed"
    RoomArchived = "room-archived"
    RoomUnarchived = "room-unarchived"
    RegularMessage = "normal-message"
    MessagePinned = "message-pinned"
    DiscussionCreated = "discussion-created"
    NewLeader = "new-leader"
    LeaderRemoved = "leader-removed"
    OwnerRemoved = "owner-removed"
    NewOwner = "new-owner"
    ModeratorRemoved = "moderator-removed"
    NewModerator = "new-moderator"
    RoomChangedTopic = "room_changed_topic"
    RoomChangedDescription = "room_changed_description"
    RoomChangedPrivacy = "room_changed_privacy"
    RoomChangedAnnouncement = "room_changed_announcement"
    # unclear what this is, maybe only for the livechat extension
    # ('connected', 'promptTransscript', ...)
    Command = "command"
    Unknown = "unknown"


class RoomMessage:
    """This represents a chat room message."""

    def __init__(self, data):
        self.m_data = data

    @classmethod
    def createNew(cls, rid, msg, parent_id=None, thread_id=None):
        data = {}
        if parent_id:
            data["_id"] = parent_id
        if thread_id:
            data["tmid"] = thread_id

        data["rid"] = rid
        data["msg"] = msg
        return RoomMessage(data)

    def isIncrementalUpdate(self):
        """An incremental update is just an addition/removal for an already
        existing message.

        This does not include message edits, which have their own message
        type. Instead it is for RegularMessage type and can contain e.g. for
        reactions added/removed.
        """
        return self.m_data.get("incupdate", False)

    def setIsIncrementalUpdate(self, val):
        self.m_data["incupdate"] = val

    def getRaw(self):
        return self.m_data

    def getID(self):
        """Returns the unique message ID."""
        return self.m_data["_id"]

    def getRoomID(self):
        return self.m_data["rid"]

    def getMessage(self):
        return self.m_data["msg"]

    def setMessage(self, msg):
        self.m_data["msg"] = msg

    def getMessageType(self):
        try:
            return MessageType(self.m_data["t"])
        except ValueError:
            # unsupported type
            return MessageType.Unknown
        except KeyError:
            # it seems regular messages don't carry a type entry
            return MessageType.RegularMessage

    def getClientTimestamp(self):
        return rcTimeToDatetime(self.m_data["ts"]["$date"])

    def getServerTimestamp(self):
        """This server timestamp may consider updates like reactions
        etc. so it is not the creation time stamp. Use
        getClientTimestamp() for this."""
        return rcTimeToDatetime(self.m_data["_updatedAt"]["$date"])

    def getCreationTimestamp(self):
        if self.isIncrementalUpdate():
            return self.getServerTimestamp()
        else:
            return self.getClientTimestamp()

    def getUserInfo(self):
        return BasicUserInfo(self.m_data["u"])

    def hasReplies(self):
        return self.getNumReplies() != 0

    def getNumReplies(self):
        return self.m_data.get("tcount", 0)

    def isThreadMessage(self):
        return self.getThreadParent() is not None

    def getThreadParent(self):
        """Returns the ID of the parent message in the thread, if
        any."""
        # threading is implemented somewhat strangely, once somebody
        # replies to another message we will first receive this new
        # message with this "tmid", then afterwards the message that
        # was replied to appears with "replies" and "tcount", where
        # "replies" only contains the users that replied but not the
        # individual message IDs.
        return self.m_data.get("tmid", None)

    def wasEdited(self):
        return 'editedAt' in self.m_data

    def getEditTime(self):
        date = self.m_data.get('editedAt', {"$date": 0})["$date"]
        return rcTimeToDatetime(date)

    def getEditUser(self):
        try:
            return BasicUserInfo(self.m_data['editedBy'])
        except KeyError:
            return None

    def hasURLs(self):
        return len(self.getURLs()) != 0

    def getURLs(self):
        return [URLInfo(url) for url in self.m_data.get("urls", [])]

    def getMentions(self):
        return [BasicUserInfo(mention) for mention in self.m_data.get("mentions", [])]

    def hasFile(self):
        return 'file' in self.m_data

    def getFile(self):
        if not self.hasFile():
            return None

        return FileInfo(self.m_data['file'])

    def getReactions(self):
        """Returns a dictionary like: {
            ':coffee': {'usernames': ['user1', 'user2']}
        }."""
        return self.m_data.get("reactions", {})

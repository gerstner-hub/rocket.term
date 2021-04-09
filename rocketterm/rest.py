# vim: ts=4 et sw=4 sts=4 :

import copy
import datetime
import http.client
import logging
import pprint

# 3rd party
import requests

# rocket.term
import rocketterm.types
import rocketterm.utils


class RestSession:
    """Interface to the Rocket.Chat REST API via https:// protocol.

    The REST API is the more complete API interface of RC. A lot of large and
    complex data structures can be retrieved with it. The downsides are the
    following:

    - it is not possible to receive asynchronous events like new messages
      added to chat rooms.
    - by default RC servers employ a denial-of-service protection
      configuration that quickly causes REST queries to be throttled or
      denied.

    These two downsides are not present in the realtime API, which lacks a lot
    of the methods the REST API provides, though.
    """

    # constant used to censor certain sensitive authentication information
    # from logs
    _CENSORED = "<CENSORED>"

    def __init__(self, server_uri):
        """:param str server: They DNS name and possible non-standard port of
        the RC server, without protocol scheme."""
        self.m_server_uri = server_uri
        self.m_logger = logging.getLogger("rest")

        self._reset()

    def _reset(self):
        """Resets all object state and sets up a clean session."""
        self.m_session = requests.Session()
        self.m_logged_in = False
        # these will be the auth token and user id issued by the RC
        # server after successful login
        self.m_auth_token = None
        self.m_user_id = None

    def _buildURL(self, endpoint):
        """Returns a full https URL for the given API endpoint."""
        return '/'.join((self.m_server_uri.getREST_URI(), endpoint))

    def _getStatusString(self, http_status_code):
        """Returns a human readable string for the given numerical http status code."""
        return requests.status_codes._codes[http_status_code][0]

    def _raiseOnBadStatus(self, resp, expected):
        """Raises an HTTPError if the given request.Response object does match
        the ``expected`` status code.
        """
        if resp.status_code == expected:
            return
        elif resp.status_code == http.client.TOO_MANY_REQUESTS:
            self.m_logger.warning("Too many requests on REST API")
            reset_time = resp.headers.get("x-ratelimit-reset", None)
            if reset_time:
                reset_time = rocketterm.utils.datetimeFromUTC_ms(int(reset_time))
                diff = reset_time - datetime.datetime.utcnow()
                self.m_logger.warning(f"Rate limiting will be reset in {diff.seconds} seconds")
            raise rocketterm.types.TooManyRequests(resp.json(), reset_time=reset_time)

        raise rocketterm.types.RESTError(
            resp.status_code,
            resp.json(),
            "Bad http status {} ({})".format(
                str(resp.status_code), self._getStatusString(resp.status_code))
        )

    def _shouldDebug(self):
        # avoid performing expensive copying/formatting for each transfer when
        # logging is not active
        return self.m_logger.isEnabledFor(logging.DEBUG)

    def _debugRequest(self, call, url, headers, json_data, url_params):
        """Logs the to-be-executed API request via Python logging."""

        if not self._shouldDebug():
            return

        msg = "-> Request {} {} ".format(call.__name__.upper(), url)
        if headers:
            headers = copy.deepcopy(headers)
            if 'X-Auth-Token' in headers:
                # don't output auth tokens
                headers['X-Auth-Token'] = self._CENSORED
            entries = ['='.join((key, val)) for key, val in headers.items()]
            msg += "with headers {}".format(', '.join(entries))
        msg += "\n"

        if url_params:
            msg += "with params: " + pprint.pformat(url_params)

        if json_data:
            json_data = copy.deepcopy(json_data)
            if "password" in json_data:
                # don't output cleartext passwords
                json_data["password"] = self._CENSORED
            if "resume" in json_data:
                json_data["resume"] = self._CENSORED
            msg += "with data: " + pprint.pformat(json_data)

        self.m_logger.debug(msg)

    def _debugResult(self, ret):
        """Logs the REST API reply via Python logging."""

        if not self._shouldDebug():
            return

        status_string = self._getStatusString(ret.status_code)

        json = copy.deepcopy(ret.json())
        data = json.get("data", {})
        if 'authToken' in data:
            # don't output auth tokens
            data['authToken'] = self._CENSORED
        me = data.get("me", {})
        if me:
            services = me.get("services", {})
            password = services.get("password", {})
            if 'bcrypt' in password:
                # don't output password digests
                password['bcrypt'] = self._CENSORED

        self.m_logger.debug("<- Reply https status = {} ({})".format(ret.status_code, status_string))
        self.m_logger.debug("{}".format(pprint.pformat(json)))

    def _getHeaders(self):
        headers = {}
        if self.m_auth_token and self.m_user_id:
            # put cached authentication data into the headers for each call
            headers["X-Auth-Token"] = self.m_auth_token
            headers["X-User-Id"] = self.m_user_id
        return headers

    def _request(self, request_call, endpoint, data, url_params, convert_to_json=True, files=None):
        """Perform a specific REST API request.

        Note that RC REST endpoints accept some parameters only as URL
        parameters or only as input data.

        :param request_call: The http request call to perform. Needs to be the
                             function of ``self.m_session`` to call like
                             ``self.m_session.get``.
        :param str endpoint: The endpoint to be appended to the AIP URL.
        :param dict data: A dictionary to be converted into JSON and supplied
                          as input data to the REST API request.
        :param dict url_params: A dictionary of key/value pairs to be encoded
                          as parameters into the REST API URL.
        :param bool convert_to_json: Whether data should be converted to a
                                     JSON string or otherwise be passed as is.
        :return: A request.Response object.
        """
        import json
        url = self._buildURL(endpoint)

        headers = self._getHeaders()

        self._debugRequest(request_call, url, headers, data, url_params)

        if convert_to_json:
            data = json.dumps(data)

        ret = request_call(
            url,
            data=data,
            headers=headers,
            params=url_params,
            files=files
        )

        self._debugResult(ret)

        return ret

    def _get(self, endpoint, data=None, good_status=200, url_params={}):
        """Perform a specific REST API GET request.

        :param int good_status: The expected "good" http status reply code. On
                                all other statuses an Exception will be thrown.
        :return: Returns a dictionary containing the reply data from the
                 server, if any.
        """
        ret = self._request(self.m_session.get, endpoint, data, url_params)

        self._raiseOnBadStatus(ret, good_status)

        return ret.json()

    def _post(self, endpoint, data=None, good_status=200, url_params={}, convert_to_json=True, files=None):
        """Perform a specific REST API POST request.

        :param int good_status: The expected "good" http status reply code. On
                                all other status an Exception will be thrown.
        :return: Returns a dictionary containing the reply data from the
                 server, if any.
         """
        ret = self._request(self.m_session.post, endpoint, data, url_params, convert_to_json, files)

        self._raiseOnBadStatus(ret, good_status)

        return ret.json()

    def close(self):
        """Reset all API state."""
        self._reset()

    def isLoggedIn(self):
        return self.m_logged_in

    def login(self, login_data):
        """Logs into the REST API using the given authentication data.

        :return: Returns a dictionary describing the logged in user account.

        The ``login_data`` is obtained from either TokenLoginData or
        PasswordLoginData objects.

        This raises LoginError if the login failed.
        """
        try:
            resp = self._post("login", login_data)
        except rocketterm.types.HTTPError as e:
            raise rocketterm.types.LoginError("Failed to login to REST API: {}".format(str(e)))
        data = resp["data"]
        # these are needed as http headers in the future to present our
        # authentication to the remote server.
        self.m_auth_token = data["authToken"]
        self.m_user_id = data["userId"]
        self.m_logged_in = True

        return data["me"]

    def logout(self):
        """Logs out the REST API."""

        if not self.m_logged_in:
            return

        data = {"userId": self.m_user_id, "authToken": self.m_auth_token}
        _ = self._post("logout", data)
        self.m_logged_in = False

    def getJoinedChannels(self):
        """Returns a list of dictionaries describing the channels that the
        logged in user has joined."""
        resp = self._get("channels.list.joined")
        return resp["channels"]

    def getJoinedGroups(self):
        """Returns a list of dictionaries describing the groups that the
        logged in user has joined."""
        resp = self._get("groups.list")
        return resp["groups"]

    def getChannelInfo(self, rid):
        """Returns the information about a specific chat room ID (only for
        ChatRoom types)."""
        resp = self._get("channels.info", url_params={"roomId": rid})
        return resp["channel"]

    def getGroupMembers(self, group_id, count=50, offset=0):
        """Gets the current members of the private group with the
        given ID.

        This information seems not to be available via the
        realtime API. This API call employs a windowing approach i.e. you need
        to specify ``count`` and ``offset`` to retrieve a certain amount of
        the full server data.
        """
        resp = self._get("groups.members", url_params={"roomId": group_id, "count": count, "offset": offset})
        return resp

    def getChannelMembers(self, channel_id, count=50, offset=0):
        """Like getGroupMembers() but for channel objects."""
        resp = self._get("channels.members", url_params={"roomId": channel_id, "count": count, "offset": offset})
        return resp

    def getUserInfo(self, user_id=None, username=None):
        """Retrieve the full user information for the given user ID or
        username.

        :return: A dictionary containing the user information.

        You need to specify at least one of the parameters.
        """
        if user_id:
            params = {"userId": user_id}
        else:
            params = {"username": username}

        resp = self._get("users.info", url_params=params)
        return resp

    def getUserStatus(self, user_id):
        """Returns the current user status information for the given user ID
        as a dictionary."""
        resp = self._get("users.getStatus", url_params={"userId": user_id})
        return resp

    def getUserList(self, count=50, offset=0):
        """Returns a list of dictionary representing the known users on the server.

        This API employs a windowing mechanism to avoid having to retrieve the
        full list of users, which can be quite large.
        """
        resp = self._get("users.list", url_params={"count": count, "offset": offset})
        return resp

    def setUserStatus(self, status, message):
        """Sets the user status of the currently logged in user to the given
        parameters.

        :param str status: The new user status which needs to match one of the
                           supported values. See the UserPresence enum for the
                           supported ones.
        :param str message: The status message that should be supplied along
                            with the status.
        """
        """Sets the status of the logged in user to the given state
        and status text."""
        resp = self._post("users.setStatus", {
            "status": status,
            "message": message
        })

        return resp

    def getDiscussions(self, rid, count=50, offset=0):
        """Returns the discussions existing in the given room ID.

        Discussions are modeled as private groups and are sub-rooms for the
        given room.

        :return: A tuple of (int, [dict(), ...]). The integer denotes the
          number of objects existing in total. The list contains the retrieved
          data structures representing the individual discussions.
        """
        resp = self._get("rooms.getDiscussions", url_params={
            "roomId": rid,
            "offset": offset,
            "count": count
        })

        return resp["total"], resp["discussions"]

    def leaveChannel(self, rid):
        """Leaves the channel with the given room ID that the user is
        currently subscribed to.

        This only works for open chat rooms, not for groups. See leaveGroup()
        for that.
        """

        self._post("channels.leave", data={"roomId": rid})

    def joinChannel(self, rid, join_code=""):
        """Joins the given open chat room and adds it to the current user's
        subscriptions."""

        self._post("channels.join", data={"roomId": rid, "joinCode": join_code})

    def leaveGroup(self, rid):
        """Leaves the group with the given room ID that the user is currently
        subscribed to.

        This only works for private groups, not for open chat rooms. See
        leaveChannel() for that.
        """

        self._post("groups.leave", data={"roomId": rid})

    def inviteToChannel(self, rid, user_id):
        """Invites another user into the given chat room."""

        self._post("channels.invite", data={"roomId": rid, "userId": user_id})

    def inviteToGroup(self, rid, user_id):
        """Invites another user into the given private group."""

        self._post("groups.invite", data={"roomId": rid, "userId": user_id})

    def kickFromChannel(self, rid, user_id):
        """Removes the given user from the given chat room."""

        self._post("channels.kick", data={"roomId": rid, "userId": user_id})

    def kickFromGroup(self, rid, user_id):
        """Removes the given user from the given private group."""

        self._post("groups.kick", data={"roomId": rid, "userId": user_id})

    def getRoomInfo(self, rid=None, room_name=None):
        """Returns a specialization of RoomBase for the given room ID or room
        name.

        Only one of the parameters is allowed and required to be set.
        """

        params = {}
        if rid:
            params["roomId"] = rid
        elif room_name:
            params["roomName"] = room_name

        resp = self._get("rooms.info", url_params=params)

        return resp["room"]

    def subscriptionsRead(self, rid=None):
        """marks the given room (any type) as read."""

        self._post("subscriptions.read", data={"rid": rid})

    def getChannelList(self, count=50, offset=0):
        """Returns a list of all open chat rooms on the server.
        """

        resp = self._get(
            "channels.list",
            url_params={"count": count, "offset": offset}
        )

        return resp

    def getInfo(self):
        """Returns remote server information like the RC server version."""

        resp = self._get("info")
        return resp["info"]

    def getMessage(self, msg_id):
        """Returns a single chat message by explicit message ID reference."""

        resp = self._get(
            "chat.getMessage",
            url_params={"msgId": msg_id}
        )

        return resp

    def uploadFileMessage(self, rid, path, filename=None, message=None,
                          description=None, thread_id=None, mime_type=None):
        """Upload a message with a file attachment.

        :param str rid: The room ID where to post this message and attachment.
        :param str path: The file on disk which to attach to the message.
        :param str filename: The filename to send along with the data. If
                             unset then the name will be take from the path
                             argument's basename.
        :param str message: An optional additional text message to send along
                            with the attachment.
        :param str description: An optional additional text description of the
                                attachment.
        :param str thread_id: An optional thread ID to attach the message to.
        :param str mime_type: An optional explicit MIME type for the file
                              attachment.
        """

        import os
        import magic

        data = {}

        if message:
            data['msg'] = message
        if description:
            data['description'] = description
        if thread_id:
            data['tmid'] = thread_id

        with open(path, 'rb') as fd:
            if not filename:
                filename = os.path.basename(path)
            if not mime_type:
                m = magic.Magic(mime=True)
                mime_type = m.from_file(path)

            files = {
                'file': (filename, fd, mime_type)
            }

            return self._post(
                "rooms.upload/" + rid,
                data,
                files=files,
                convert_to_json=False
            )

    def downloadFile(self, sub_url, outfile):
        """Download a file attachment from the server.

        :param str sub_url: the download sub-URL on the server.
        :param file outpath: a file-like object to write the downloaded data
                             to. It must be opened in binary mode.
        """

        url = "{}/{}?download".format(
            self.m_server_uri.getURI(),
            sub_url.lstrip('/')
        )

        with self.m_session.get(
                url,
                stream=True,
                headers=self._getHeaders()
        ) as r:
            r.raise_for_status()

            for chunk in r.iter_content(chunk_size=8192):
                outfile.write(chunk)

            outfile.flush()

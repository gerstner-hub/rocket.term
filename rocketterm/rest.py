# vim: ts=4 et sw=4 sts=4 :

import copy
import logging
import pprint

# 3rd party
import requests

# rocket.term
import rocketterm.types


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

        raise rocketterm.types.HTTPError(
            resp.status_code,
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

        msg = "-> Request {} ".format(call.__name__.upper())
        if headers:
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

    def _request(self, request_call, endpoint, data, url_params):
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
        :return: A request.Response object.
        """
        import json
        headers = {}
        if self.m_auth_token and self.m_user_id:
            # put cached authentication data into the headers for each call
            headers["X-Auth-Token"] = self.m_auth_token
            headers["X-User-Id"] = self.m_user_id
        url = self._buildURL(endpoint)

        self._debugRequest(request_call, url, headers, data, url_params)

        ret = request_call(
            url,
            data=json.dumps(data),
            headers=headers,
            params=url_params
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

    def _post(self, endpoint, data=None, good_status=200, url_params={}):
        """Perform a specific REST API POST request.

        :param int good_status: The expected "good" http status reply code. On
                                all other status an Exception will be thrown.
        :return: Returns a dictionary containing the reply data from the
                 server, if any.
         """
        ret = self._request(self.m_session.post, endpoint, data, url_params)

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
        """Returns the information about a specific room ID."""
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

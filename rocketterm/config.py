# vim: ts=4 et sw=4 sts=4 :

import configparser
import os
from enum import Enum


class ConfigError(Exception):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class AuthType(Enum):

    OAuth = "oauth"
    Cleartext = "cleartext"
    External = "external"


class RocketConfig:

    DEFAULT_BASENAME = "rocket-term.ini"

    def __init__(self, path=None):

        self.m_config = dict()
        self.m_path = path if path else self._getDefaultPath()

    def _getDefaultPath(self):

        return os.path.expanduser("~/.config/{}".format(self.DEFAULT_BASENAME))

    def _checkConfig(self):
        try:
            info = os.stat(self.m_path)
        except FileNotFoundError:
            print("No configuration exists yet in", self.m_path)
            print("Please create one from the example file shipped with rocket.term")
            if self.m_path == self._getDefaultPath():
                self._tryExtractTemplate(self.m_path)
            raise ConfigError("No configuration exists")
        except Exception as e:
            print("Failed to open config file in", self.m_path)
            print(e)
            raise ConfigError("Opening configuration failed")

        self._checkSafeMode(info)

    def _tryExtractTemplate(self, outpath):
        """This is the integration with setuptools and its package
        resource concept. Only works if we're running from an EGG."""
        try:
            import pkg_resources
        except Exception:
            print("Failed to import pkg_resources module to extract template configuration file")
            return

        try:
            import shutil
            import stat
            import sys
            template = pkg_resources.resource_filename("rocketterm", "etc/{}".format(self.DEFAULT_BASENAME))
            shutil.copy(template, outpath)
            # shutil doesn't allow specifying a safe mode. the
            # template doesn't contain sensitive data yet, though,
            # so having this chmod race is acceptable.
            os.chmod(outpath, stat.S_IWUSR | stat.S_IRUSR)
            print("\nA template configuration file has been created in", outpath, "\n")
            # exit instead of raising the exception, we offered
            # the user the template configuration file and this is
            # the important information in this case
            sys.exit(2)
        except KeyError:
            # this would mean there simply is no EGG or the EGG
            # doesn't contain our file
            return
        except Exception as e:
            print("Failed to extract template configuration resources:", e)

    def _checkSafeMode(self, info):

        import stat

        problems = []

        if os.getuid() != info.st_uid:
            problems.append("The file is not owned by your user")
        if os.getgid() != 0 and os.getgid() != info.st_gid:
            problems.append("The file group is not your user's main group")
        if (info.st_mode & (stat.S_IROTH | stat.S_IWOTH)) != 0:
            problems.append("The file is world readable/writeable")

        if not problems:
            return

        print("The configuration file in {} has no safe permissions:\n".format(self.m_path))

        for problem in problems:
            print("- {}".format(problem))

        print("\nThe configuration file contains sensitive authentication",
              "data and should only be accessible by your user")

        raise ConfigError("Unsafe configuration file encountered")

    def _parseConfig(self):

        self.m_parser = configparser.ConfigParser()
        self.m_config = dict()
        self.m_parser.read(self.m_path)

        self._parseConnectionDetails()
        self._parseDefaults()

    def _raiseMissingItemError(self, section, setting=None):
        if not setting:
            text = "{}: missing [{}] section".format(self.m_path, section)
        else:
            text = "{}: missing [{}]->{} setting".format(self.m_path, section, setting)

        raise ConfigError(text)

    def _parseConnectionDetails(self):
        conn_section = 'connection'

        if conn_section not in self.m_parser.sections():
            self._raiseMissingItemError(conn_section)

        connection = self.m_parser[conn_section]

        auth_type = connection.get("auth_type", None)

        if not auth_type:
            self._raiseMissingItemError(conn_section, "auth_type")

        AUTH_COMBINATIONS = {
            AuthType.Cleartext: ["password"],
            AuthType.External: ["password_eval"],
            AuthType.OAuth: ["oauth_user_id", "oauth_access_token"]
        }

        try:
            auth_type = AuthType(auth_type)
            self.m_config["auth_type"] = auth_type
        except ValueError:
            raise ConfigError("Invalid auth_type setting {}. Choose one of {}".format(
                auth_type, ', '.join([e.value for e in AuthType]))
            )

        auth_settings = AUTH_COMBINATIONS[auth_type]
        required_settings = auth_settings + ["server", "username"]

        for setting in required_settings:
            value = connection.get(setting, None)
            if not value:
                self._raiseMissingItemError(conn_section, setting)
            self.m_config[setting] = value

        for key, _default in (
            ("rest_protocol", "https://"),
            ("realtime_protocol", "wss://")
        ):
            val = connection.get(key, _default)
            if not val.endswith("://"):
                raise ConfigError(
                    "Invalid protocol setting '{}={}'. Should end with '://' like 'https://' or 'wss://'".format(
                        key, val
                    )
                )

            self.m_config[key] = val

    def _parseDefaults(self):
        global_section = 'global'

        self.m_config["default_room"] = self.m_parser.get(
                global_section, "default_room", fallback=None)

    def getConfig(self):

        if self.m_config:
            return self.m_config

        self._checkConfig()
        self._parseConfig()
        return self.m_config

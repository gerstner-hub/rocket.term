# vim: ts=4 et sw=4 sts=4 :

import configparser
import os
import stat
from enum import Enum

import rocketterm.utils
from rocketterm.screen import Screen


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

        problems = []

        if os.getuid() != info.st_uid:
            problems.append("The file is not owned by your user")
        if os.getgid() != info.st_gid and info.st_gid != 0:
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

        self.m_parser = configparser.RawConfigParser()
        self.m_config = dict()
        self.m_parser.read(self.m_path)

        self._parseConnectionDetails()
        self._parseDefaults()
        self._parseHooks()
        self._parseColors()
        self._parseUserColors()
        self._parsePaletteColors()
        self._parseKeys()

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

        roombox_pos = self.m_parser.get(global_section, "roombox_position", fallback="left")
        supported = ("left", "right")

        if roombox_pos not in supported:
            raise ConfigError(f"Invalid roombox_position setting '{roombox_pos}'. Supported: {' '.join(supported)}")

        self.m_config["roombox_pos"] = roombox_pos

        show_roombox = self.m_parser.get(global_section, "show_roombox", fallback="true")
        show_roombox = self._parseBoolean("show_roombox", show_roombox)

        self.m_config["show_roombox"] = show_roombox

    def _parseBoolean(self, setting, value):

        value = value.lower().strip()

        if value in ("true", "yes", "1", "on"):
            return True
        elif value in ("false", "no", "0", "off"):
            return False

        raise ConfigError(f"Invalid setting {setting}={value}. Expected boolean string like true/false")

    def _parseHooks(self):
        hook_section = 'hooks'

        hooks = dict()
        self.m_config["hooks"] = hooks

        if not self.m_parser.has_section(hook_section):
            return

        for key, value in self.m_parser["hooks"].items():
            if not key.startswith("on_"):
                raise ConfigError(f"Invalid hooks setting '{key}'. Should start with 'on_'")

            key = key[3:]

            hooks[key] = value

    def _normalizeColor(self, value):
        ret = value.strip().strip('"\'').lower()

        if ret == "none":
            # replace by an arbitrary color to pass verification, it is
            # ignored anyway
            ret = "black"

        return ret

    def _parseUserColors(self):
        section = 'color.users'

        colors = dict()
        self.m_config["color.users"] = colors

        if not self.m_parser.has_section(section):
            return

        for key, value in self.m_parser[section].items():
            color = self._validateForegroundColor(key, value)
            colors[key] = color

    def _parsePaletteColors(self):
        section = 'color.palette'

        colors = dict()
        self.m_config["color.palette"] = colors

        if not self.m_parser.has_section(section):
            return

        for key, value in self.m_parser[section].items():
            if key not in Screen.DEFAULT_PALETTE:
                raise ConfigError(
                    f"Unsupported palette configuration item '{key} = {value}' encountered"
                )
            fg, bg = self._validateColorPair(key, value)
            colors[key] = fg, bg

    def _parseColors(self):
        section = 'color'

        colors = dict()
        self.m_config["color"] = colors

        dynamic_users = list()
        colors["dynamic_users"] = dynamic_users

        dynamic_threads = list()
        colors["dynamic_threads"] = dynamic_threads

        if not self.m_parser.has_section(section):
            return

        def parseColorList(value):
            for part in value.split(','):
                color = self._validateForegroundColor(key, part)
                yield color

        for key, value in self.m_parser[section].items():
            if key == "own_user_color":
                color = self._validateForegroundColor(key, value)
                colors["own_user"] = color
            elif key == "dynamic_user_colors":
                for color in parseColorList(value):
                    dynamic_users.append(color)
            elif key == "dynamic_thread_colors":
                for color in parseColorList(value):
                    dynamic_threads.append(color)
            else:
                raise ConfigError(f"Invalid [{section}] key '{key}'")

    def _validateForegroundColor(self, key, color):
        fg_colors = rocketterm.utils.getSupportedForegroundColors()
        color = self._normalizeColor(color)
        if color in fg_colors:
            return color

        raise ConfigError(
            f"Invalid foreground color specification '{key} = {color}'. Supported colors: {', '.join(fg_colors)}"
        )

    def _validateBackgroundColor(self, key, color):
        bg_colors = rocketterm.utils.getSupportedBackgroundColors()
        color = self._normalizeColor(color)
        if color in bg_colors:
            return color

        raise ConfigError(
            f"Invalid background color specification '{key} = {color}'. Supported colors: {', '.join(bg_colors)}"
        )

    def _validateColorPair(self, key, color):
        parts = [p.strip() for p in color.split('/')]
        if len(parts) != 2:
            raise ConfigError(
                f"Invalid color pair specification '{key} = {color}'. Expected 'fg_color/bg_color'."
            )

        if parts[0] == "none" and parts[1] == "none":
            raise ConfigError(
                f"Invalid color pair specification '{key} = {color}' (can't be all 'none')."
            )

        fg = self._validateForegroundColor(key, parts[0])
        bg = self._validateBackgroundColor(key, parts[1])

        return fg, bg

    def _parseKeys(self):
        section = 'keys'

        keys = dict()
        self.m_config["keys"] = keys

        if not self.m_parser.has_section(section):
            return

        for key, value in self.m_parser[section].items():
            if key not in Screen.DEFAULT_KEYMAP:
                raise ConfigError(
                    f"Unsupported key configuration item '{key} = {value}' encountered"
                )
            label = value.strip().strip('"\'')
            keys[key] = label

    def getConfig(self):

        if self.m_config:
            return self.m_config

        self._checkConfig()
        self._parseConfig()
        return self.m_config

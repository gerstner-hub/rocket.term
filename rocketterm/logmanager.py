# vim: ts=4 et sw=4 sts=4 :
import logging


class LogManager:
    """Manages the application white logging module settings."""

    def __init__(self):
        pass

    def setDefaultLogLevel(self, level):
        level = self._getLogLevel(level)
        logging.root.setLevel(level)

    def addLogfile(self, path):
        handler = logging.FileHandler(path)
        formatter = logging.Formatter('%(asctime)s %(name)10s %(levelname)10s: %(message)s')
        handler.setFormatter(formatter)
        logging.root.addHandler(handler)

    def disableConsoleLogging(self):
        # by default the logging module logs to the console,
        # which is bad when we're using urwid. It's not all
        # that easy to simply disable logging python-wide.
        # Adding this NullHandler() seems to overwrite the
        # default console handler, though.
        logging.getLogger().addHandler(logging.NullHandler())

    def applyLogLevels(self, settings):
        """Parses a comma separate loglevel setting string.

        :param str settings: main=DEBUG,rtsession=WARNING
        """

        errors = []

        for setting in settings.split(','):
            if not setting:
                continue
            parts = setting.split('=')
            if len(parts) != 2:
                errors.append("Bad loglevel setting: '{}'".format(setting))
                continue

            logger, level = parts

            try:
                logging.getLogger(logger).setLevel(self._getLogLevel(level))
            except Exception as e:
                errors.append("bad logger or loglevel name: '{}': {}".format(setting, str(e)))

        if errors:
            raise Exception('\n'.join(errors))

    @classmethod
    def _getLogLevel(self, string):
        """Translates the loglevel string from the command line into
        the numerical loglevel required by the logging module."""
        try:
            if not isinstance(string, str):
                return string
            return getattr(logging, string.upper())
        except AttributeError:
            raise Exception("Invalid loglevel: '{}'".format(string))

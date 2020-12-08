# vim: ts=4 et sw=4 sts=4 :

import argparse
import logging
import os
import sys

from rocketterm.comm import RocketComm
from rocketterm.terminal import printe, print_colored
import rocketterm.config
import rocketterm.screen
import rocketterm.types
import rocketterm.utils


class RocketTerm:
    """Main application class that holds all objects and runs the main
    loop."""

    def __init__(self):
        self.m_comm = None
        self.setupArgparse()

    def setupArgparse(self):
        self.m_parser = argparse.ArgumentParser(
            description="Text based terminal client for Rocket.Chat"
        )
        self.m_parser.add_argument(
            "--logfile", type=str,
            help="Output logging messages into the given file. See --loglevel for the default loglevel setting",
            default=None
        )
        self.m_parser.add_argument(
            "--loglevel", type=str,
            help="Sets the default loglevel for the --logfile option",
            choices=("debug", "info", "warning", "error", "critical"),
            default="warning"
        )
        self.m_parser.add_argument(
            "--loglevel-set",
            type=str,
            help="Sets per-logger loglevels. Expects a comma separated string like 'main=debug,screen=warning'. Can also be set through the environment variable LOGLEVEL_SET which takes precedence over this command line switch.",
            default=""
        )
        self.m_parser.add_argument(
            "--config",
            type=str,
            help="Provides an alternate path to the configuration file to use.",
            default=None
        )

    def parseArgs(self):
        self.m_args = self.m_parser.parse_args()

        if self.m_args.logfile:
            logging.basicConfig(
                filename=self.m_args.logfile,
                level=self._getLogLevel(self.m_args.loglevel),
                format='%(asctime)s %(name)10s %(levelname)10s: %(message)s'
            )
        else:
            # by default the logging module logs to the console,
            # which is bad when we're using urwid. It's not all
            # that easy to simply disable logging python-wide.
            # Adding this NullHandler() seems to overwrite the
            # default console handler, though.
            logging.getLogger().addHandler(logging.NullHandler())

        loglevel_set = os.environ.get("LOGLEVEL_SET", None)
        if not loglevel_set:
            loglevel_set = self.m_args.loglevel_set

        for setting in loglevel_set.split(','):
            if not setting:
                continue
            parts = setting.split('=')
            if len(parts) != 2:
                printe("Bad LOGLEVEL_SET or --loglevel-set setting:", setting)
                continue

            logger, level = parts

            try:
                logging.getLogger(logger).setLevel(self._getLogLevel(level))
            except Exception as e:
                printe(
                    "Bad logger or loglevel name in LOGLEVEL_SET or --loglevel-setting '{}':".format(setting), str(e)
                )

        self.m_logger = logging.getLogger("main")

    @classmethod
    def _getLogLevel(self, string):
        """Translates the loglevel string from the command line into
        the numerical loglevel required by the logging module."""
        try:
            return getattr(logging, string.upper())
        except AttributeError:
            raise Exception("Invalid loglevel: '{}'".format(string))

    def getLoginData(self):
        username = self.m_config["username"]

        auth_type = self.m_config["auth_type"]

        if auth_type == rocketterm.config.AuthType.Cleartext:
            password = self.m_config.get("password")
            return rocketterm.types.PasswordLoginData(username, password)
        elif auth_type == rocketterm.config.AuthType.External:
            pw_eval = self.m_config.get("password_eval")
            evaluator = rocketterm.utils.CommandEvaluator(pw_eval)
            try:
                password = evaluator.getResult()
            except Exception as e:
                print_colored(
                        "Failed to produce password from external command: {}".format(str(e)),
                        color='red'
                )
                sys.exit(1)
            return rocketterm.types.PasswordLoginData(username, password)
        elif auth_type == rocketterm.config.AuthType.OAuth:
            oauth_token = self.m_config.get("oauth_access_token")
            return rocketterm.types.TokenLoginData(oauth_token)
        else:
            raise Exception("Unexpected auth type encountered")

    def setupComm(self):

        login_data = self.getLoginData()
        server = self.m_config["server"]
        self.m_comm = RocketComm(server, login_data)
        print("Connecting to server {}...".format(server), end='')
        sys.stdout.flush()
        try:
            self.m_comm.connect()
            print_colored("success", color='green')
        except Exception:
            print_colored("failed", color='red')
            raise

    def setupScreen(self):

        self.m_screen = rocketterm.screen.Screen(self.m_config, self.m_comm)

    def login(self):
        print("Logging into {} as {}...".format(
                self.m_config["server"],
                self.m_config["username"],
            ),
            end=''
        )
        sys.stdout.flush()
        try:
            self.m_comm.login()
            print_colored("success", color='green')
        except rocketterm.types.LoginError as e:
            print_colored("failed", color='red')
            self.m_logger.error((str(e)))
            return False
        except Exception as e:
            print_colored("failed", color='red')
            self.m_logger.error(str(e))
            raise

        print("Logged in as {} ({}, {})".format(
            self.m_comm.getFullName(),
            self.m_comm.getUsername(),
            self.m_comm.getEmail())
        )

        return True

    def teardown(self):
        if not self.m_comm:
            # nothing to do at all
            return

        if self.m_comm.isLoggedIn():
            print("Logging out...")
            sys.stdout.flush()
            self.m_comm.logout()
            print_colored("success", color='green')

        self.m_comm.close()

    def run(self):

        self.parseArgs()
        rconfig = rocketterm.config.RocketConfig(self.m_args.config)
        self.m_config = rconfig.getConfig()

        self.setupComm()

        try:
            if self.login():
                self.setupScreen()
                self.m_screen.mainLoop()
        finally:
            self.teardown()

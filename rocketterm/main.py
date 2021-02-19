# vim: ts=4 et sw=4 sts=4 :

import argparse
import logging
import os
import sys

import rocketterm.config
import rocketterm.hookmanager
import rocketterm.logmanager
import rocketterm.screen
import rocketterm.types
import rocketterm.utils
from rocketterm.comm import RocketComm
from rocketterm.terminal import print_colored, printe


class GlobalObjects:

    config = None
    controller = None
    comm = None
    screen = None
    log_manager = None
    # command line arguments as returned from argparse
    cmd_args = None


class RocketTerm:
    """Main application class that holds all objects and runs the main
    loop."""

    def __init__(self):
        self.m_comm = None
        self.m_global_objects = GlobalObjects()
        self.m_global_objects.log_manager = rocketterm.logmanager.LogManager()
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
            help="Sets per-logger loglevels. Expects a comma separated string like 'main=debug,screen=warning'. "
                 "Can also be set through the environment variable LOGLEVEL_SET which takes precedence over this "
                 "command line switch.",
            default=""
        )
        self.m_parser.add_argument(
            "--no-hidden-commands",
            action='store_true',
            help="Certain internal commands for development purposes are hidden from command completion by default. "
                 "By passing this switch they will be treated like normal commands.",
        )
        self.m_parser.add_argument(
            "--config",
            type=str,
            help="Provides an alternate path to the configuration file to use.",
            default=None
        )

    def parseArgs(self):
        self.m_args = self.m_parser.parse_args()

        log_manager = self.m_global_objects.log_manager

        if self.m_args.logfile:
            log_manager.addLogfile(self.m_args.logfile)
            log_manager.setDefaultLogLevel(self.m_args.loglevel)
        else:
            log_manager.disableConsoleLogging()

        loglevel_set = os.environ.get("LOGLEVEL_SET", None)
        if not loglevel_set:
            loglevel_set = self.m_args.loglevel_set

        try:
            log_manager.applyLogLevels(loglevel_set)
        except Exception as e:
            printe("Bad LOGLEVEL_SET or --loglevel-set setting(s):\n{}".format(str(e)))

        self.m_logger = logging.getLogger("main")

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

    def getServerURI(self):
        name = self.m_config["server"]
        rest_scheme = self.m_config["rest_protocol"]
        rt_scheme = self.m_config["realtime_protocol"]

        return rocketterm.types.ServerURI(rest_scheme, rt_scheme, name)

    def setupComm(self):

        login_data = self.getLoginData()
        server_uri = self.getServerURI()
        self.m_comm = RocketComm(server_uri, login_data)
        self.m_global_objects.comm = self.m_comm
        print("Connecting to server {}...".format(
            server_uri.getServerName()), end=''
        )
        sys.stdout.flush()
        try:
            self.m_comm.connect()
            print_colored("success", color='green')
        except Exception:
            print_colored("failed", color='red')
            raise

    def setupController(self):
        self.m_global_objects.controller = rocketterm.controller.Controller(self.m_global_objects)

    def setupScreen(self):
        self.m_global_objects.screen = rocketterm.screen.Screen(self.m_global_objects)

    def setupHookManager(self):
        self.m_hook_manager = rocketterm.hookmanager.HookManager(self.m_global_objects)

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

        email = self.m_comm.getEmail()

        print("Logged in as {} ({}, {})".format(
            self.m_comm.getFullName(),
            self.m_comm.getUsername(),
            email if email else "<unknown email>"
        ))

        return True

    def teardown(self):
        if not self.m_comm:
            # nothing to do at all
            return

        if self.m_comm.isLoggedIn():
            print("Logging out...", end='')
            sys.stdout.flush()
            try:
                self.m_comm.logout()
            except Exception:
                print()
                raise
            print_colored("success", color='green')

        self.m_comm.close()

    def run(self):

        self.parseArgs()
        self.m_global_objects.cmd_args = self.m_args
        rconfig = rocketterm.config.RocketConfig(self.m_args.config)
        self.m_config = rconfig.getConfig()
        self.m_global_objects.config = self.m_config

        self.setupComm()

        try:
            if self.login():
                self.setupController()
                self.setupScreen()
                self.setupHookManager()
                self.m_global_objects.screen.mainLoop()
        finally:
            self.teardown()

# vim: ts=4 et sw=4 sts=4 :

import os
import sys

# 3rd party
try:
    import termcolor
    have_termcolor = True
except ModuleNotFoundError:
    have_termcolor = False


def print_colored(*args, **kwargs):
    """print() wrapper that supports a color='mycolor' parameter."""
    import io
    color = kwargs.pop("color", None)

    if not color or not have_termcolor:
        print(*args, **kwargs)
        return

    sio = io.StringIO()
    orig_file = kwargs.pop("file", None)
    kwargs["file"] = sio
    print(*args, **kwargs)
    print(
            termcolor.colored(sio.getvalue(), color),
            sep='', end='', file=orig_file)


def printe(*args, **kwargs):
    """Shortcut function to print to stderr."""
    kwargs["file"] = sys.stderr
    print(*args, **kwargs)


def printe_colored(*args, **kwargs):
    kwargs["file"] = sys.stderr
    print_colored(*args, **kwargs)


class TerminalSession:
    """Small helper class to interact with the terminal."""

    def __init__(self):
        self.m_debug_out = None

    def setDebugStream(self, debug):
        self.m_debug_colored = os.isatty(debug.fileno())
        self.m_debug_out = debug

    def isDebugActive(self):
        return self.m_debug_out is not None

    def _readline(self, prompt):

        print(prompt, end='')
        sys.stdout.flush()
        reply = sys.stdin.readline()
        if not reply:
            raise EOFError
        return reply.strip()

    def printBanner(self, text, color=None):
        print()
        if have_termcolor:
            termcolor.cprint(text.upper().center(120), color=color)
        else:
            print(text.upper().center(120))
        print()

    def printError(self, *args, **kwargs):
        kwargs["color"] = "red"
        printe_colored(*args, **kwargs)

    def printWarning(self, *args, **kwargs):
        kwargs["color"] = "yellow"
        printe_colored(*args, **kwargs)

    def printDebug(self, *args, **kwargs):
        if not self.m_debug_out:
            return
        kwargs["file"] = self.m_debug_out

        if self.m_debug_colored:
            kwargs["color"] = "cyan"
            print_colored(*args, **kwargs)
        else:
            print(*args, **kwargs)
            self.m_debug_out.flush()

    def queryYesNo(self, query):
        reply = ''
        while reply.lower() not in ('y', 'n'):
            reply = self._readline(query + " (y/n) ")

        return reply.lower() == 'y'


terminal = TerminalSession()

# vim: ts=4 et sw=4 sts=4 :

import datetime
from html.parser import HTMLParser


class CommandEvaluator:
    """Helper class that runs an external command and returns its standard
    output as a string."""

    def __init__(self, cmd):
        """:param str cmd: A string containing the external command plus
        possible parameters."""
        self.m_eval_cmd = cmd.split()

    def getResult(self):
        import subprocess
        try:
            output = subprocess.check_output(
                self.m_eval_cmd,
                shell=False,
                close_fds=True
            )

            return output.decode('utf8').strip()
        except subprocess.CalledProcessError:
            raise


class CallbackMultiplexer:
    """A helper class that multiplexes callback invocations to a dynamic list
    of callback consumers.
    """

    def __init__(self):
        # list of actual consumers to forward callbacks to
        self.m_consumers = []
        self.m_main_consumer = None

    def addConsumer(self, consumer, main_consumer=False):
        """add an additional callback consumer.

        :param bool main_consumer: If set then this consumer will be the main
                                   consumer. This means that its return value
                                   will determine the overall result of a
                                   callback. There can only be one main
                                   consumer.
        """
        if main_consumer and self.m_main_consumer:
            raise Exception("Multiple main consumers added")
        if main_consumer:
            self.m_main_consumer = consumer
        self.m_consumers.append(consumer)

    def delConsumer(self, consumer):
        self.m_consumers.remove(consumer)
        if consumer == self.m_main_consumer:
            self.m_main_consumer = None

    def _invoke(self, *args, **kwargs):
        method_name = kwargs.pop("method_name")

        for consumer in self.m_consumers:
            method = getattr(consumer, method_name)
            this_ret = method(*args, **kwargs)

            if consumer == self.m_main_consumer:
                ret = this_ret

        return ret if self.m_main_consumer else None

    def __getattr__(self, method_name):
        import functools
        return functools.partial(self._invoke, method_name=method_name)


def rcTimeToDatetime(rc_time):
    """Converts a rocket chat timestamp into a Python datetime object."""
    return datetime.datetime.fromtimestamp(rc_time / 1000.0)


def datetimeToRcTime(dt):
    return int(dt.timestamp() * 1000.0)


def datetimeFromUTC_ms(utc_ts_ms):
    return datetime.datetime.utcfromtimestamp(utc_ts_ms / 1000.0)


def createRoom(rt_room_obj, rt_subscription):
    """Creates a new room object from the given raw room and subscription data
    structures."""
    from rocketterm.types import DirectChat, ChatRoom, PrivateChat

    type_map = {
        'd': DirectChat,
        'c': ChatRoom,
        'p': PrivateChat
    }

    _type = rt_room_obj.get('t')

    return type_map[_type](rt_room_obj, rt_subscription)


def createUserPresenceFromStatusIndicator(indicator):
    """Translates numerical "status indicator" values received from e.g.
    stream-notify-logged events into a UserPresence() type."""
    from rocketterm.types import UserPresence

    mapping = {
        0: UserPresence.Offline,
        1: UserPresence.Online,
        2: UserPresence.Away,
        3: UserPresence.Busy
    }

    try:
        return mapping[indicator]
    except KeyError:
        raise Exception("Invalid status indicator value: {}".format(indicator))


def getExceptionContext(ex):
    import sys
    import traceback

    _, _, tb = sys.exc_info()
    fn, ln, _, _ = traceback.extract_tb(tb)[-1]
    return "{}:{}: {}".format(fn, ln, str(ex))


def getMessageEditContext(room_msg):
    """Returns an explanatory message for the given RoomMessage instance that
    was edited.
    """
    assert room_msg.wasEdited()
    editor = room_msg.getEditUser()
    edited_by_self = editor == room_msg.getUserInfo()

    edit_prefix = "[message was edited {}on {}]".format(
        "" if edited_by_self else "by {} ".format(room_msg.getEditUser().getUsername()),
        room_msg.getEditTime().strftime("%x %X")
    )

    msg = room_msg.getMessage()
    if msg:
        return "{}: {}".format(edit_prefix, msg)
    else:
        return edit_prefix


def getMessageRemoveContext(room_msg):
    import rocketterm.types
    assert room_msg.wasEdited()
    assert room_msg.getMessageType() == rocketterm.types.MessageType.MessageRemoved

    remover = room_msg.getEditUser()
    removed_by_self = remover == room_msg.getUserInfo()

    msg = "[a message was removed {}on {}]".format(
        "" if removed_by_self else "by {} ".format(room_msg.getEditUser().getUsername()),
        room_msg.getEditTime().strftime("%x %X")
    )

    return msg


def getServerHasBogusUserStatusEventBug(server_info):
    # TODO: this is similar to what getServerHasSetUserStatusBug() is about
    # but I'm not sure yet in which version or in which version of the RC
    # server this is fixed at all.
    #
    # the issue is that upon a user status change two event message are sent
    # out, one with the correct status message and one with the wrong one.
    return True


def getServerHasSetUserStatusBug(server_info):
    # there is a bug that only status message changes are reported via
    # stream-notify-logged, but not if the status changes.
    #
    # this has been fixed in a newer RC server version already in commit
    # 287d1dcb376a4613c9c2d6f5b9c22f3699891d2e (version 3.7.0)
    version = server_info.getVersion()

    if not version or len(version) < 3:
        # can't conclude, suppose it has the bug, it's just a performance
        # issue in the worst case
        return True

    if version[0] < 3:
        return True
    elif version[0] == 3 and version[1] < 7:
        return True
    else:
        return False


class HTMLToTextConverter(HTMLParser):

    def __init__(self):
        super().__init__()
        self.m_text = ""

    def handle_data(self, data):
        self.m_text += data

    def get_text(self):
        return self.m_text


def convertHTMLToText(html):
    converter = HTMLToTextConverter()
    converter.feed(html)
    return converter.get_text()


def getSafeFilename(basename):
    ret = basename.replace(' ', '_')
    ret = "".join(c for c in ret if c.isalnum() or c in "._")
    return ret


def openTempFile(basename, dir=None, auto_delete=True):
    import os
    import tempfile
    basename = getSafeFilename(basename)
    prefix, suffix = os.path.splitext(basename)
    return tempfile.NamedTemporaryFile(
        dir=dir, prefix=prefix, suffix=suffix, delete=auto_delete
    )


def getSupportedForegroundColors():
    """Returns a list of supported foreground color names in urwid."""
    import urwid
    return urwid.display_common._BASIC_COLORS


def getSupportedBackgroundColors():
    """Returns a list of supported backgroundcolor names in urwid."""
    # only the first half are supported as background colors
    import urwid
    return urwid.display_common._BASIC_COLORS[:8]


def wrapText(text, width, indent_len):
    import textwrap

    # the textwrap module is a bit difficult to tune ... we want to
    # maintain newlines from the original string, but enforce a maximum
    # line length while prefixing an indentation string to each line
    # starting from the second one.
    #
    # maintaining the original newlines works via `replace_whitespace =
    # False`, however then we don't get these lines split up in the
    # result, also existing newlines aren't resetting the line length
    # calculation, causing early linebreaks to be inserted.
    #
    # therefore explicitly split existing newlines to keep them, then
    # process each line with textwrap.

    indent = (' ' * indent_len)

    orig_lines = text.split('\n')
    lines = []

    for line in orig_lines:
        add = textwrap.wrap(line, width=width, replace_whitespace=False)
        lines.extend(add)

    if len(lines) > 1:
        lines = lines[0:1] + [indent + line for line in lines[1:]]

    return '\n'.join(lines)

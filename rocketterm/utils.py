# vim: ts=4 et sw=4 sts=4 :

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


def rcTimeToDatetime(rc_time):
    """Converts a rocket chat timestamp into a Python datetime object."""
    import datetime
    return datetime.datetime.fromtimestamp(rc_time / 1000.0)


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

    msg = "[message was removed {}on {}]".format(
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

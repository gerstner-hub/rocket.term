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

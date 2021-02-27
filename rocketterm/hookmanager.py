# vim: ts=4 et sw=4 sts=4 :
import json
import logging
import os
import shlex
import subprocess


class HookManager:
    """This type acts as a callback consumer of the Controller class to invoke
    configured hooks for certain callback events."""

    def __init__(self, global_objects):
        self.m_global_objects = global_objects
        self.m_logger = logging.getLogger("HookManager")

        self.SUPPORTED_HOOKS = (
            "new_room_message",
            "room_opened",
            "room_hidden",
            "room_added",
            "room_removed",
            "lost_connection",
            "internal_error",
            "mentioned"
        )

        self.m_hooks = dict([(hook, []) for hook in self.SUPPORTED_HOOKS])

        self._configureHooks()

        global_objects.controller.addCallbackHandler(self)

    def _configureHooks(self):
        hooks = self.m_global_objects.config["hooks"]

        for name, cmdline in hooks.items():
            self._addHook(name, cmdline)

    def _addHook(self, name, cmdline):
        if name not in self.m_hooks:
            self.m_logger.warning(f"unsupported hook type {name} encountered.")
            return

        try:
            args = shlex.split(cmdline)

            if not args:
                self.m_logger.warning(f"empty command line for hook {name}.")
                return

            # expand possible ~/ home directory elements
            args[0] = os.path.expanduser(args[0])

        except Exception as e:
            self.m_logger.warning(f"failed to parse hook {name} command line {cmdline}: {str(e)}.")
            return

        try:
            info = os.stat(args[0])

            if not self._checkSafeMode(args[0], info):
                return
        except OSError as e:
            self.m_logger.warn(f"hook executable {args[0]} cannot be found/accessed: {str(e)}.")
            return

        self.m_hooks[name].append(args)

    def __getattr__(self, name):

        def _nullCallback(*args, **kwargs):
            return

        # for any callback we did not implement (and are not interested in)
        # simply do nothing
        return _nullCallback

    def _isHookConfigured(self, hook):
        return len(self.m_hooks[hook]) > 0

    def _getEnv(self, context):
        # dervice the environment variable from the format specifier names
        extra_env = dict((f"RC_{key.upper()}", value) for key, value in context.items())
        ret = os.environ.copy()
        ret.update(extra_env)
        return ret

    def _executeHook(self, raw_args, env, fmt_dict):
        argv = []

        for arg in raw_args:
            try:
                arg = arg.format(**fmt_dict)
                argv.append(arg)
            except KeyError as e:
                self.m_logger.warn(f"Failed to format hook {' '.join(raw_args)}: KeyError: {str(e)}. "
                                   f"Available keys: {' '.join(fmt_dict.keys())}")
                return False

        try:
            res = subprocess.call(
                    argv, shell=False, close_fds=True, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

            if res != 0:
                self.m_logger.warning(f"Hook '{' '.join(argv)}' returned non-zero status {res}")
                return False
        except Exception as e:
            self.m_logger.warning(f"Failed to execute hook '{' '.join(argv)}': {str(e)}")
            return False

        return True

    def _executeHooks(self, name, context):
        env = self._getEnv(context)

        bad_hooks = []

        for nr, args in enumerate(self.m_hooks[name]):
            try:
                if not self._executeHook(args, env, context):
                    bad_hooks.append(nr)
            except Exception as e:
                self.m_logger.warning(f"Internal error while executing hook {name}: {str(e)}")

        for bad_nr in reversed(bad_hooks):
            # remove hooks that failed to avoid future errors
            self.m_hooks[name].pop(bad_nr)

    def _getRoomContext(self, hook_name, room):
        return {
            "friendly_name": room.getFriendlyName(),
            "hook": hook_name,
            "json": json.dumps(room.getRaw()),
            "label": room.getLabel(),
            "name": room.getName(),
            "type": room.typeLabel(),
        }

    def _handleRoomHook(self, hook, room):
        context = self._getRoomContext(hook, room)
        self._executeHooks(hook, context)

    def _checkSafeMode(self, path, info):

        import stat

        problems = []

        is_group_writeable = (info.st_mode & (stat.S_IWGRP)) != 0
        is_world_writeable = (info.st_mode & (stat.S_IWOTH)) != 0

        # Allow the executable to be owned by ourselves or by the trusted
        # 'root' user. Other users should not be able to influence which code
        # we execute.
        if info.st_uid != 0 and os.getuid() != info.st_uid:
            problems.append("The file is not owned by root or your own user account")
        if is_group_writeable and info.st_gid != 0 and os.getgid() != info.st_gid:
            problems.append("The file is group writeable and the group is not root or your user's main group")
        if is_world_writeable:
            problems.append("The file is world writeable")

        if not problems:
            return True

        self.m_logger.warn(f"Ignoring hook {path}, because its contents can be influenced by other users:")

        for problem in problems:
            self.m_logger.warn(problem)

        return False

    def handleNewRoomMessage(self, msg):
        hook = "new_room_message"
        if not self._isHookConfigured(hook) and not self._isHookConfigured("mentioned"):
            # dont perform anything expensive if nobody consumes this
            return

        room = self.m_global_objects.controller.getRoomInfoByID(msg.getRoomID())

        context = {
            "hook": hook,
            "is_update": str(msg.isIncrementalUpdate()),
            "json": json.dumps(msg.getRaw()),
            "msg_author": msg.getUserInfo().getUsername(),
            "msg_id": msg.getID(),
            "msg_is_thread": str(msg.isThreadMessage()),
            "msg_text": msg.getMessage(),
            "msg_type": msg.getMessageType().value,
            "msg_was_edited": str(msg.wasEdited()),
            "room_friendly_name": room.getFriendlyName(),
            "room_id": room.getID(),
            "room_json": json.dumps(room.getRaw()),
            "room_label": room.getLabel(),
            "room_name": room.getName(),
            "room_type": room.typeLabel(),
        }

        if msg.isIncrementalUpdate() and msg.getOldMessage() is not None:
            context["old_json"] = json.dumps(msg.getOldMessage().getRaw())

        self._executeHooks(hook, context)

        controller = self.m_global_objects.controller
        mentions_us = controller.doesMessageMentionUs(msg)
        is_direct_chat = room.isDirectChat() and not controller.isMessageFromUs(msg)

        if mentions_us or is_direct_chat:
            hook = "mentioned"
            context["hook"] = hook
            self._executeHooks(hook, context)

    def internalError(self, description):
        hook = "internal_error"
        context = {
            "hook": hook,
            "error_text": description
        }

        self._executeHooks(hook, context)

    def roomOpened(self, room):
        self._handleRoomHook("room_opened", room)

    def roomHidden(self, room):
        self._handleRoomHook("room_hidden", room)

    def roomAdded(self, room):
        self._handleRoomHook("room_added", room)

    def roomRemoved(self, room):
        self._handleRoomHook("room_removed", room)

    def lostConnection(self):
        hook = "lost_connection"
        context = {
            "hook": hook
        }
        self._executeHooks(hook, context)

# vim: ts=4 et sw=4 sts=4 :

import logging

import urwid


class CommandInput(urwid.Edit):
    """A specialized urwid.Edit widget that implements the rocket.term command
    input box."""

    PROMPT = u"> "

    def __init__(self, cmd_callback, complete_callback, keymap):
        """
        :param cmd_callback: A callback function to be invoked once a command
                             has been entered. The callback will receive the
                             full command line that was entered as a string.
        :param complete_callback: A callback function to be invoked once a
                             command completion is requested by the user. The
                             callback will receive thef ull command line that
                             was entered as a string. The callback needs to
                             return the line text to display or None if
                             nothing could be completed.
        """
        super().__init__(caption=self.PROMPT)
        self.m_logger = logging.getLogger("CommandInput")
        self.m_cmd_callback = cmd_callback
        self.m_complete_callback = complete_callback
        # here a command history is maintained that can be scrolled back to
        self.m_history = []
        # the current history position we have
        self.m_cur_history_pos = -1
        # when scrolling through the history while we're already having new
        # input then this new input is stored here to allow it to be restored
        # via selectNewerHistoryEntry() later on, without having to actually
        # submit the command.
        self.m_pending_cmd = ""
        self.m_keymap = keymap
        self.m_next_input_verbatim = False

    def addPrompt(self, text):
        """Prepends text to the command input prompt."""
        self.set_caption("{} {}".format(text, self.PROMPT))

    def resetPrompt(self):
        """Resets the command input prompt to its default value."""
        self.set_caption(self.PROMPT)

    def _replaceText(self, line):
        """Replace the command input text by the given string and adjust
        the cursor accordingly."""
        self.set_edit_text(line)
        self.set_edit_pos(len(line))

    def _addText(self, to_add):
        current = self.text[len(self.caption):]
        new = current + to_add
        self.set_edit_text(new)
        self.set_edit_pos(len(new))

    def _getCommandLine(self):
        """Returns the net command line input from the input box."""
        return self.text[len(self.caption):]

    def _addToHistory(self, line):
        """Adds the given command to the command history."""
        self.m_history.append(line)
        self.m_cur_history_pos = -1
        self.m_pending_cmd = ""

    def _selectOlderHistoryEntry(self):
        if not self.m_history:
            return

        if self.m_cur_history_pos == -1:
            self.m_cur_history_pos = len(self.m_history) - 1
            self.m_pending_cmd = self._getCommandLine()
        elif self.m_cur_history_pos == 0:
            return
        else:
            self.m_cur_history_pos -= 1

        histline = self.m_history[self.m_cur_history_pos]

        self._replaceText(histline)

    def _selectNewerHistoryEntry(self):
        if not self.m_history:
            return

        if self.m_cur_history_pos == -1:
            return
        elif self.m_cur_history_pos == len(self.m_history) - 1:
            if self.m_pending_cmd:
                self._replaceText(self.m_pending_cmd)
                self.m_pending_cmd = ""
            else:
                self._replaceText("")
            self.m_cur_history_pos = -1
            return
        else:
            self.m_cur_history_pos += 1

        histline = self.m_history[self.m_cur_history_pos]

        self._replaceText(histline)

    def keypress(self, size, key):

        if self.m_logger.isEnabledFor(logging.DEBUG):
            self.m_logger.debug("key event: {}".format(key))

        if self.m_next_input_verbatim:
            return self._handleVerbatimKey(key)

        # first let the EditBox handle the event to e.g. move the cursor
        # around or add new characters.
        if super().keypress(size, key) is None:
            return None

        return self._handleRegularKey(key)

    def _handleRegularKey(self, key):
        command = self._getCommandLine()

        if key == 'enter':
            if command:
                self.set_edit_text(u"")
                self.m_cmd_callback(command)
                self._addToHistory(command)
            return None
        elif key == 'tab':
            new_line = self.m_complete_callback(command)
            if new_line:
                self._replaceText(new_line)
            return None
        elif key == self.m_keymap['cmd_history_older']:
            self._selectOlderHistoryEntry()
            return None
        elif key == self.m_keymap['cmd_history_newer']:
            self._selectNewerHistoryEntry()
            return None
        elif key == 'ctrl v':
            self.m_next_input_verbatim = True

        return key

    def _handleVerbatimKey(self, key):

        self.m_next_input_verbatim = False

        if key == 'enter':
            self._addText("\n")
        elif key == 'tab':
            self._addText("\t")

        return None


class SizedListBox(urwid.ListBox):
    """A ListBox that knows its last size.

    urwid widgets by default have no known size. The size is only known
    while in the process of rendering. This is quite stupid for certain
    situations. Therefore this specialization of ListBox stores its last
    size seen during rendering for clients of the ListBox to refer to.

    This specialized ListBox also makes the widget non-selectable, because we
    don't want the focus to jump around, it needs to stick on the command
    input widget.
    """

    def __init__(self, *args, **kwargs):
        """
        :param size_callback: An optional callback function that will be
        invoked when a size change is encountered.
        """

        try:
            self.m_size_cb = kwargs.pop("size_callback")
        except KeyError:
            self.m_size_cb = None

        super().__init__(*args, **kwargs)

        self.m_last_size_seen = None

    def _invokeSizeCB(self, old_size):
        if self.m_size_cb:
            self.m_size_cb(self)

    def getLastSizeSeen(self):
        return self.m_last_size_seen

    def getNumRows(self):
        return self.m_last_size_seen[1] if self.m_last_size_seen else 0

    def getNumCols(self):
        return self.m_last_size_seen[0] if self.m_last_size_seen else 0

    def render(self, size, focus=False):

        if self.m_last_size_seen != size:
            old = self.m_last_size_seen
            self.m_last_size_seen = size
            self._invokeSizeCB(old)

        return super().render(size, focus)

    def selectable(self):
        return False

    def scrollUp(self, small_increments):
        """Wrapper to explicitly scroll the list up.

        :param bool small_increments: If set then not a whole page but only a
                                      single list item will be scrolled.
        """
        if not self.m_last_size_seen:
            return

        if small_increments:
            self._keypress_up(self.m_last_size_seen)
        else:
            self._keypress_page_up(self.m_last_size_seen)

    def scrollDown(self, small_increments):
        """Wrapper to explicitly scroll the list down.

        See scrollUp().
        """
        if not self.m_last_size_seen:
            return

        if small_increments:
            self._keypress_down(self.m_last_size_seen)
        else:
            self._keypress_page_down(self.m_last_size_seen)

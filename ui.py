"""Curses-based TUI for the IRC client."""

import curses
import curses.textpad
from collections import deque


class ChatUI:
    """Layout:

    +----------------------------------+
    |  message area (scrollable)       |
    |                                  |
    +----------------------------------+
    |  status bar                      |
    +----------------------------------+
    |  input bar                       |
    +----------------------------------+
    """

    def __init__(self, stdscr: curses.window):
        self.stdscr = stdscr
        curses.curs_set(1)
        curses.use_default_colors()

        self.messages: deque[str] = deque(maxlen=500)
        self._scroll_offset = 0  # how many lines scrolled back from bottom

        self.current_channel = "SYSTEM"
        self.nick = ""

        # will be set by main loop
        self.on_input = None  # type: ignore

        self._layout()

    # -- layout helpers --

    def _layout(self) -> None:
        self.height, self.width = self.stdscr.getmaxyx()
        self.input_y = self.height - 1
        self.status_y = self.height - 2
        self.msg_height = self.height - 3  # -1 input, -1 status, -1 for 0-index

    def add_message(self, tag: str, text: str) -> None:
        self.messages.append(f"[{tag}] {text}")
        if self._scroll_offset > 0:
            # keep following if user hasn't scrolled up
            pass  # don't auto-scroll when user is reading history
        self._draw_messages()

    def set_status(self, channel: str, nick: str) -> None:
        self.current_channel = channel
        self.nick = nick
        self._draw_status()

    # -- drawing --

    def _draw_messages(self) -> None:
        self.stdscr.erase()
        self._layout()

        total = len(self.messages)
        visible = self.msg_height
        start = max(0, total - visible - self._scroll_offset)
        end = max(0, total - self._scroll_offset)

        for i, line in enumerate(range(start, end)):
            y = i
            if y >= visible:
                break
            try:
                self.stdscr.addnstr(y, 0, self.messages[line], self.width - 1)
            except curses.error:
                pass

        self._draw_status()
        self._draw_input()
        self.stdscr.refresh()

    def _draw_status(self) -> None:
        bar = f" [{self.nick}] | Channel: {self.current_channel} | /join #chan  /nick name  /quit"
        bar = bar[: self.width - 1]
        try:
            self.stdscr.addstr(self.status_y, 0, bar, curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_input(self) -> None:
        try:
            self.stdscr.addstr(self.input_y, 0, "> ")
            self.stdscr.clrtoeol()
        except curses.error:
            pass

    # -- input loop --

    def read_input(self) -> str:
        """Blocking read of one line from the input bar. Returns empty string on error."""
        curses.echo()
        try:
            self.stdscr.move(self.input_y, 2)
            self.stdscr.clrtoeol()
            s = self.stdscr.getstr(self.input_y, 2).decode("utf-8", errors="replace")
        except curses.error:
            s = ""
        curses.noecho()
        return s

    def scroll_up(self) -> None:
        if self._scroll_offset < len(self.messages) - 1:
            self._scroll_offset += min(5, len(self.messages) - 1 - self._scroll_offset)
            self._draw_messages()

    def scroll_down(self) -> None:
        if self._scroll_offset > 0:
            self._scroll_offset = max(0, self._scroll_offset - 5)
            self._draw_messages()

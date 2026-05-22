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

        self.messages: deque[tuple[str, str]] = deque(maxlen=500)
        self._scroll_offset = 0  # how many lines scrolled back from bottom

        self.current_channel = "SYSTEM"
        self.nick = ""
        self.input_buffer = ""

        # will be set by main loop
        self.on_input = None  # type: ignore

        self._layout()

    # -- layout helpers --

    def _layout(self) -> None:
        self.height, self.width = self.stdscr.getmaxyx()
        self.input_y = self.height - 1
        self.status_y = self.height - 2
        self.msg_height = self.height - 3  # -1 input, -1 status, -1 for 0-index

    def add_message(self, tag: str, text: str, redraw: bool = True) -> None:
        self.messages.append((tag, text))
        if redraw:
            self._draw_messages()

    def set_status(self, channel: str, nick: str) -> None:
        if channel == self.current_channel and nick == self.nick:
            return  # avoid pointless redraw every main-loop tick
        if channel != self.current_channel:
            self._scroll_offset = 0
        self.current_channel = channel
        self.nick = nick
        self._draw_messages()

    # -- drawing --

    def _draw_messages(self) -> None:
        self.stdscr.erase()
        self._layout()

        filtered_msgs = []
        for tag, text in self.messages:
            tag_clean = tag.lstrip("#").lower()
            current_clean = self.current_channel.lstrip("#").lower()
            if tag_clean == current_clean:
                filtered_msgs.append(f"[{tag}] {text}")

        total = len(filtered_msgs)
        visible = self.msg_height
        start = max(0, total - visible - self._scroll_offset)
        end = max(0, total - self._scroll_offset)

        for i, line in enumerate(range(start, end)):
            y = i
            if y >= visible:
                break
            try:
                self.stdscr.addnstr(y, 0, filtered_msgs[line], self.width - 1)
            except curses.error:
                pass

        self._draw_status()
        self._draw_input()
        self.stdscr.refresh()

    def _draw_status(self) -> None:
        bar = f" [{self.nick}] | View: {self.current_channel} | /join #chan  /switch X  /msg nick text  /quit"
        bar = bar[: self.width - 1]
        try:
            self.stdscr.addstr(self.status_y, 0, bar, curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_input(self) -> None:
        try:
            self.stdscr.move(self.input_y, 0)
            self.stdscr.addstr(self.input_y, 0, "> ")
            self.stdscr.clrtoeol()
            # Draw input buffer (cropped to fit the screen width)
            max_len = self.width - 3
            visible_buf = self.input_buffer[-max_len:] if max_len > 0 else ""
            self.stdscr.addstr(self.input_y, 2, visible_buf)
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
        filtered_count = sum(1 for tag, _ in self.messages if tag.lstrip("#").lower() == self.current_channel.lstrip("#").lower())
        if self._scroll_offset < filtered_count - 1:
            self._scroll_offset += min(5, filtered_count - 1 - self._scroll_offset)
            self._draw_messages()

    def scroll_down(self) -> None:
        if self._scroll_offset > 0:
            self._scroll_offset = max(0, self._scroll_offset - 5)
            self._draw_messages()

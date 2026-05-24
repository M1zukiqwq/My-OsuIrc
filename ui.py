"""Curses-based TUI for the IRC client."""

import curses
import curses.textpad
import unicodedata
from collections import deque


def _display_width(s: str) -> int:
    """Return the terminal display width of a string (CJK = 2 columns)."""
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _truncate_to_width(s: str, max_width: int) -> str:
    """Truncate string so its display width <= max_width, never splitting a CJK char."""
    w = 0
    for i, ch in enumerate(s):
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > max_width:
            return s[:i]
        w += cw
    return s


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
        self._last_display_name = "SYSTEM"

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

    def set_status(self, display_name: str, nick: str, force_redraw: bool = False) -> None:
        # Extract the stable channel name (before any status suffix like "waiting BanchoBot")
        base_channel = display_name.split("  ")[0] if "  " in display_name else display_name

        if not force_redraw and base_channel == self.current_channel and nick == self.nick and display_name == self._last_display_name:
            return

        switched = base_channel != self.current_channel
        if switched:
            self._scroll_offset = 0
        self.current_channel = base_channel
        self._last_display_name = display_name
        self.nick = nick
        # Only full redraw if channel actually changed or forced (e.g. new messages)
        if switched or force_redraw:
            self._draw_messages()
        else:
            # Same channel, just status text changed — update status bar only
            self._draw_status()
            self._draw_input()
            self.stdscr.refresh()

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
        max_col = self.width - 1

        blank = " " * self.width
        for i, line in enumerate(range(start, end)):
            y = i
            if y >= visible:
                break
            try:
                s = _truncate_to_width(filtered_msgs[line], max_col)
                # Write blank first to clear any CJK double-width residue,
                # then write the actual content on top.
                self.stdscr.addstr(y, 0, blank)
                self.stdscr.addstr(y, 0, s)
            except curses.error:
                pass

        # Clear remaining lines so stale content doesn't linger
        for y in range(end - start, visible):
            try:
                self.stdscr.addstr(y, 0, blank)
            except curses.error:
                pass

        self._draw_status()
        self._draw_input()
        self.stdscr.refresh()

    def _draw_status(self) -> None:
        view_text = self._last_display_name if self._last_display_name else self.current_channel
        bar = f" [{self.nick}] | View: {view_text} | /join #chan  /switch X  /msg nick text  /quit"
        bar = _truncate_to_width(bar, self.width - 1)
        try:
            self.stdscr.addstr(self.status_y, 0, " " * self.width, curses.A_REVERSE)
            self.stdscr.addstr(self.status_y, 0, bar, curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_input(self) -> None:
        try:
            self.stdscr.move(self.input_y, 0)
            self.stdscr.clrtoeol()
            self.stdscr.addstr("> ")
            # Draw input buffer (cropped to fit the screen width by display columns)
            # Reserve 1 extra col to avoid curses error on last cell of last row
            max_col = self.width - 4
            buf = ""
            if max_col > 0 and self.input_buffer:
                # Take as many chars from the right as fit in max_col display width
                buf = self.input_buffer
                while buf and _display_width(buf) > max_col:
                    buf = buf[1:]
                try:
                    self.stdscr.addstr(buf)
                except curses.error:
                    # Last cell edge — trim one more char and retry
                    if buf:
                        buf = buf[:-1]
                        try:
                            self.stdscr.addstr(buf)
                        except curses.error:
                            pass
            # Position cursor at end of input text
            cursor_col = 2 + _display_width(buf) if buf else 2
            cursor_col = min(cursor_col, self.width - 1)
            self.stdscr.move(self.input_y, cursor_col)
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

"""Curses-based TUI for the IRC client."""

import curses
import locale
import unicodedata
from collections import deque

HISTORY_LIMIT = 5000


def _char_width(ch: str) -> int:
    if not ch or unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch)[0] == "C":
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _display_width(s: str) -> int:
    """Return the terminal display width of a string (CJK = 2 columns)."""
    return sum(_char_width(ch) for ch in s)


def _truncate_to_width(s: str, max_width: int) -> str:
    """Truncate string so its display width <= max_width, never splitting a CJK char."""
    if max_width <= 0:
        return ""
    w = 0
    for i, ch in enumerate(s):
        cw = _char_width(ch)
        if w + cw > max_width:
            return s[:i]
        w += cw
    return s


def _wrap_to_width(s: str, max_width: int) -> list[str]:
    if max_width <= 0:
        return []

    lines: list[str] = []
    current: list[str] = []
    width = 0

    for ch in s:
        cw = _char_width(ch)
        if cw == 0:
            current.append(ch)
            continue
        if cw > max_width:
            if current:
                lines.append("".join(current))
                current = []
                width = 0
            clipped = _truncate_to_width(ch, max_width)
            if clipped:
                lines.append(clipped)
            continue
        if current and width + cw > max_width:
            lines.append("".join(current))
            current = [ch]
            width = cw
        else:
            current.append(ch)
            width += cw

    if current or not lines:
        lines.append("".join(current))
    return lines


def _sanitize_display_text(s: str) -> str:
    """Keep control characters from moving the curses cursor while drawing."""
    out: list[str] = []
    for ch in s:
        if ch == "\t":
            out.append("    ")
        elif ch in ("\r", "\n"):
            out.append(" ")
        elif unicodedata.category(ch)[0] == "C":
            continue
        else:
            out.append(ch)
    return "".join(out)


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
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        try:
            self.stdscr.keypad(True)
            self.stdscr.nodelay(True)
        except curses.error:
            pass

        self.messages: deque[tuple[str, str]] = deque(maxlen=HISTORY_LIMIT)
        self._scroll_offset = 0  # how many visual lines scrolled back from bottom

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
        self.input_y = max(0, self.height - 1)
        self.status_y = max(0, self.height - 2)
        self.msg_height = max(0, self.height - 2)

    def add_message(self, tag: str, text: str, redraw: bool = True) -> None:
        old_line_count = self._current_display_line_count() if self._scroll_offset else 0
        self.messages.append((tag, text))
        if self._scroll_offset:
            new_line_count = self._current_display_line_count()
            self._scroll_offset += max(0, new_line_count - old_line_count)
            self._clamp_scroll_offset(new_line_count)
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

    def _current_display_lines(self, max_col: int) -> list[str]:
        lines: list[str] = []
        if max_col <= 0:
            return lines
        for tag, text in self.messages:
            tag_clean = tag.lstrip("#").lower()
            current_clean = self.current_channel.lstrip("#").lower()
            if tag_clean == current_clean:
                message = _sanitize_display_text(f"[{tag}] {text}")
                lines.extend(_wrap_to_width(message, max_col))
        return lines

    def _current_display_line_count(self) -> int:
        self._layout()
        max_col = max(0, self.width - 1)
        return len(self._current_display_lines(max_col))

    def _clamp_scroll_offset(self, total_lines: int) -> None:
        max_offset = max(0, total_lines - self.msg_height)
        self._scroll_offset = min(max(0, self._scroll_offset), max_offset)

    def _draw_messages(self) -> None:
        self.stdscr.erase()
        self._layout()
        if self.width <= 0 or self.height <= 0:
            return

        max_col = max(0, self.width - 1)
        display_lines = self._current_display_lines(max_col)
        total = len(display_lines)
        visible = self.msg_height
        self._clamp_scroll_offset(total)
        start = max(0, total - visible - self._scroll_offset)
        end = max(0, total - self._scroll_offset)

        for i, line in enumerate(range(start, end)):
            y = i
            if y >= visible:
                break
            try:
                s = display_lines[line]
                self.stdscr.move(y, 0)
                self.stdscr.clrtoeol()
                if s:
                    self.stdscr.addstr(y, 0, s)
            except curses.error:
                pass

        # Clear remaining lines so stale content doesn't linger
        for y in range(end - start, visible):
            try:
                self.stdscr.move(y, 0)
                self.stdscr.clrtoeol()
            except curses.error:
                pass

        self._draw_status()
        self._draw_input()
        self.stdscr.refresh()

    def _draw_status(self) -> None:
        if self.width <= 0 or self.height < 2:
            return
        view_text = self._last_display_name if self._last_display_name else self.current_channel
        scroll_limit = max(0, self._current_display_line_count() - self.msg_height)
        scroll_text = f" | Scroll: {self._scroll_offset}/{scroll_limit}" if self._scroll_offset else ""
        bar = _sanitize_display_text(
            f" [{self.nick}] | View: {view_text}{scroll_text} | /join #chan  /switch X  /msg nick text  /quit"
        )
        bar = _truncate_to_width(bar, max(0, self.width - 1))
        try:
            self.stdscr.move(self.status_y, 0)
            fill = " " * max(0, self.width - 1)
            if fill:
                self.stdscr.addstr(fill, curses.A_REVERSE)
            self.stdscr.move(self.status_y, 0)
            if bar:
                self.stdscr.addstr(bar, curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_input(self) -> None:
        if self.width <= 0 or self.height <= 0:
            return
        try:
            self.stdscr.move(self.input_y, 0)
            self.stdscr.clrtoeol()
            prompt = _truncate_to_width("> ", max(0, self.width - 1))
            if prompt:
                self.stdscr.addstr(prompt)
            # Draw input buffer (cropped to fit the screen width by display columns)
            # Reserve 1 extra col to avoid curses error on last cell of last row
            prompt_width = _display_width(prompt)
            max_col = max(0, self.width - prompt_width - 1)
            buf = ""
            if max_col > 0 and self.input_buffer:
                # Take as many chars from the right as fit in max_col display width
                buf = _sanitize_display_text(self.input_buffer)
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
            cursor_col = prompt_width + _display_width(buf)
            cursor_col = min(cursor_col, self.width - 1)
            self.stdscr.move(self.input_y, cursor_col)
        except curses.error:
            pass

    # -- input loop --

    def read_input(self) -> str:
        """Blocking read of one line from the input bar. Returns empty string on error."""
        curses.echo()
        try:
            self.stdscr.nodelay(False)
            self.stdscr.move(self.input_y, 2)
            self.stdscr.clrtoeol()
            encoding = locale.getpreferredencoding(False) or "utf-8"
            s = self.stdscr.getstr(self.input_y, 2).decode(encoding, errors="replace")
        except curses.error:
            s = ""
        finally:
            try:
                self.stdscr.nodelay(True)
            except curses.error:
                pass
            curses.noecho()
        return s

    def scroll_up(self, amount: int = 1) -> None:
        self._layout()
        total_lines = self._current_display_line_count()
        before = self._scroll_offset
        self._scroll_offset += max(1, amount)
        self._clamp_scroll_offset(total_lines)
        if self._scroll_offset != before:
            self._draw_messages()

    def scroll_down(self, amount: int = 1) -> None:
        if self._scroll_offset > 0:
            self._scroll_offset = max(0, self._scroll_offset - max(1, amount))
            self._draw_messages()

    def scroll_page_up(self) -> None:
        self._layout()
        self.scroll_up(max(1, self.msg_height - 1))

    def scroll_page_down(self) -> None:
        self._layout()
        self.scroll_down(max(1, self.msg_height - 1))

    def scroll_to_top(self) -> None:
        self._layout()
        total_lines = self._current_display_line_count()
        self._scroll_offset = max(0, total_lines - self.msg_height)
        self._draw_messages()

    def scroll_to_bottom(self) -> None:
        if self._scroll_offset:
            self._scroll_offset = 0
            self._draw_messages()

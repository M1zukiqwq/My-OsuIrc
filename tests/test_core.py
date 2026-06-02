import unittest

from my_osuirc.chat.ui import ChatUI, _display_width, _sanitize_display_text, _truncate_to_width, _wrap_to_width
from my_osuirc.irc.client import IrcClient, parse_irc_message


class FakeScreen:
    def __init__(self, height: int = 5, width: int = 20) -> None:
        self.height = height
        self.width = width

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def keypad(self, value: bool) -> None:
        pass

    def nodelay(self, value: bool) -> None:
        pass

    def erase(self) -> None:
        pass

    def move(self, y: int, x: int) -> None:
        pass

    def clrtoeol(self) -> None:
        pass

    def addstr(self, *args) -> None:
        pass

    def refresh(self) -> None:
        pass


class DisplayWidthTest(unittest.TestCase):
    def test_cjk_width_and_truncation(self) -> None:
        self.assertEqual(_display_width("a中文b"), 6)
        self.assertEqual(_truncate_to_width("ab中文cd", 5), "ab中")
        self.assertEqual(_truncate_to_width("ab中文cd", 6), "ab中文")
        self.assertEqual(_wrap_to_width("ab中文cd", 4), ["ab中", "文cd"])

    def test_zero_width_and_control_characters(self) -> None:
        self.assertEqual(_display_width("e\u0301"), 1)
        self.assertEqual(_display_width("\x1b"), 0)
        self.assertEqual(_sanitize_display_text("a\tb\nc\r\x1bd"), "a    b c d")


class IrcQueueTest(unittest.TestCase):
    def test_parse_utf8_message(self) -> None:
        prefix, command, params = parse_irc_message(":nick!u@h PRIVMSG #osu :你好 世界")

        self.assertEqual(prefix, "nick!u@h")
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(params, ["#osu", "你好 世界"])

    def test_drain_messages_flushes_queue_once(self) -> None:
        client = IrcClient("me", "pw")
        wakeups = []
        client.on_message = lambda: wakeups.append("wake")

        client._enqueue("#osu", "<nick> 你好")
        client._enqueue("SYSTEM", "刷新")

        self.assertEqual(wakeups, ["wake", "wake"])
        self.assertEqual(client.drain_messages(), [("#osu", "<nick> 你好"), ("SYSTEM", "刷新")])
        self.assertEqual(client.drain_messages(), [])


class ScrollTest(unittest.TestCase):
    def make_ui(self) -> ChatUI:
        ui = ChatUI(FakeScreen(height=5, width=20))
        ui.current_channel = "#osu"
        ui._last_display_name = "#osu"
        ui.nick = "me"
        return ui

    def test_scrolls_past_one_screen(self) -> None:
        ui = self.make_ui()
        for i in range(8):
            ui.add_message("#osu", f"line {i}", redraw=False)

        ui.scroll_to_top()
        self.assertEqual(ui._scroll_offset, 5)

        ui.scroll_down(2)
        self.assertEqual(ui._scroll_offset, 3)

        ui.scroll_to_bottom()
        self.assertEqual(ui._scroll_offset, 0)

    def test_new_messages_preserve_scrolled_history_position(self) -> None:
        ui = self.make_ui()
        for i in range(8):
            ui.add_message("#osu", f"line {i}", redraw=False)

        ui.scroll_to_top()
        ui.add_message("#osu", "new line", redraw=False)

        self.assertEqual(ui._scroll_offset, 6)


if __name__ == "__main__":
    unittest.main()

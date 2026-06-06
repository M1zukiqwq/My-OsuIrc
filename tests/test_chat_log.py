import tempfile
import unittest
from pathlib import Path

from my_osuirc.irc.client import IrcClient, _chat_log_filename


class ChatLogTest(unittest.TestCase):
    def test_filename_sanitised(self) -> None:
        self.assertEqual(_chat_log_filename("#mp_121270745"), "mp_121270745.log")
        self.assertEqual(_chat_log_filename("BanchoBot"), "BanchoBot.log")
        self.assertEqual(_chat_log_filename("../evil"), ".._evil.log")

    def test_inbound_and_outbound_logged_per_room(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = IrcClient("RefBot", "pw", chat_log_dir=temp)
            client._enqueue("#mp_1", "<Alice> hello there")           # inbound chat
            client._enqueue("#mp_1", "<BanchoBot> All players are ready")
            client.send("#mp_1", "!mp start 7")                       # outbound (socket is None; still logged)
            client._enqueue("BanchoBot", "Created the tournament match")  # PM -> own file

            room = Path(temp, "mp_1.log").read_text(encoding="utf-8")
            self.assertIn("<Alice> hello there", room)
            self.assertIn("All players are ready", room)
            self.assertIn("<RefBot> !mp start 7", room)
            self.assertRegex(room, r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ")  # timestamped
            self.assertIn("Created the tournament match", Path(temp, "BanchoBot.log").read_text(encoding="utf-8"))

    def test_disabled_when_dir_is_none(self) -> None:
        client = IrcClient("RefBot", "pw", chat_log_dir=None)
        client._enqueue("#mp_1", "<Alice> hi")  # must not raise
        client.send("#mp_1", "noop")


if __name__ == "__main__":
    unittest.main()

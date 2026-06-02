import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from my_osuirc.referee.agent import RefereeAgent
from my_osuirc.referee.api import RefereeApiClient
from my_osuirc.referee.client import ServerRefereeCli
from my_osuirc.referee.core import RefereeStore, RulePack, rulepack_from_draft
from my_osuirc.referee.server import RefereeHTTPServer, SQLiteRefereeStore, import_json_store


class FakeIrcClient:
    def __init__(self, nick: str = "RefBot") -> None:
        self.nick = nick
        self.sent = []
        self.joined = []
        self.messages = []
        self._running = True

    def send(self, target: str, text: str) -> None:
        self.sent.append((target, text))

    def join(self, channel: str) -> None:
        self.joined.append(channel)

    def drain_messages(self):
        messages = list(self.messages)
        self.messages.clear()
        return messages


class ServerCase(unittest.TestCase):
    def make_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = SQLiteRefereeStore(Path(temp.name) / "referee.db")
        self.addCleanup(store.close)
        return store

    def make_http(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = SQLiteRefereeStore(Path(temp.name) / "referee.db")
        server = RefereeHTTPServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup() -> None:
            server.shutdown()
            server.server_close()
            store.close()
            thread.join(timeout=2)

        self.addCleanup(cleanup)
        host, port = server.server_address
        return RefereeApiClient(f"http://{host}:{port}"), store

    def test_sqlite_confirm_gate_and_crud(self) -> None:
        store = self.make_store()
        draft = rulepack_from_draft("Draft Rules", draft={"bp_timer": 80})
        store.save_rulepack(draft)

        with self.assertRaises(PermissionError):
            store.create_scheduled_session("Blocked", draft.id, [])

        confirmed = store.confirm_rulepack(draft.id)
        self.assertIsNotNone(confirmed)
        session = store.create_scheduled_session(
            "Allowed",
            draft.id,
            [{"name": "A", "players": ["Alice"]}, {"name": "B", "players": ["Bob"]}],
        )

        self.assertEqual(store.load_session(session.id).rulepack.bp_timer, 80)
        self.assertEqual(len(store.list_sessions()), 1)

    def test_due_claim_uses_lead_time_and_is_atomic(self) -> None:
        store = self.make_store()
        now = time.time()
        store.create_scheduled_session("Future", "default", [], match_time=str(now + 3600), room_lead_time_sec=600)
        due = store.create_scheduled_session("Due", "default", [], match_time=str(now + 300), room_lead_time_sec=600)

        claimed = store.claim_due_sessions("agent-a", limit=5, now=now)
        second = store.claim_due_sessions("agent-b", limit=5, now=now)

        self.assertEqual([record["config"]["id"] for record in claimed], [due.id])
        self.assertEqual(second, [])
        self.assertEqual(store.load_session_record(due.id)["assigned_agent_id"], "agent-a")

    def test_http_draft_confirm_and_session_create(self) -> None:
        api, _store = self.make_http()
        draft = api.draft_rulepack({"name": "HTTP Rules", "draft": {"join_timer": "130"}})["rulepack"]

        with self.assertRaises(Exception):
            api.create_session({"name": "Nope", "rulepack_id": draft["id"], "teams": []})

        api.confirm_rulepack(draft["id"])
        created = api.create_session(
            {
                "name": "HTTP Match",
                "rulepack_id": draft["id"],
                "teams": [{"name": "A", "players": ["Alice"]}, {"name": "B", "players": ["Bob"]}],
                "match_time": "",
            }
        )

        self.assertEqual(created["session"]["config"]["name"], "HTTP Match")
        self.assertEqual(created["session"]["rulepack"]["join_timer"], 130)

    def test_agent_claims_controls_room_and_reports_events(self) -> None:
        api, store = self.make_http()
        created = api.create_session(
            {
                "name": "Agent Match",
                "rulepack_id": "default",
                "teams": [{"name": "A", "players": ["Alice"]}, {"name": "B", "players": ["Bob"]}],
                "match_time": "",
            }
        )["session"]
        session_id = created["config"]["id"]
        irc = FakeIrcClient()
        agent = RefereeAgent(irc, api, agent_id="agent-test", output_func=lambda text: None)

        agent.claim_due_sessions()
        agent.tick_sessions()
        self.assertEqual(irc.sent, [("BanchoBot", f"!mp make {created['config']['name']}")])
        self.assertEqual(store.load_session(session_id).state.stage, "creating_room")

        irc.messages.append(("BanchoBot", "Created the tournament match https://osu.ppy.sh/mp/321"))
        agent.drain_irc()
        self.assertIn("#mp_321", irc.joined)
        self.assertEqual(store.load_session(session_id).state.stage, "inviting")

        agent.tick_sessions()
        sent_text = [text for _, text in irc.sent]
        self.assertIn("!mp invite Alice", sent_text)
        self.assertIn("!mp timer 120", sent_text)
        self.assertEqual(store.load_session(session_id).state.stage, "waiting_players")

        irc.messages.append(("#mp_321", "<BanchoBot> All players are ready"))
        agent.drain_irc()
        agent.tick_sessions()
        self.assertIn(("#mp_321", "!mp start 7"), irc.sent)

    def test_human_cli_control_blocks_agent_automation(self) -> None:
        api, store = self.make_http()
        session_id = api.create_session({"name": "Manual", "rulepack_id": "default", "teams": [], "match_time": ""})[
            "session"
        ]["config"]["id"]
        irc = FakeIrcClient()
        agent = RefereeAgent(irc, api, agent_id="agent-manual", output_func=lambda text: None)
        agent.claim_due_sessions()
        outputs = []
        cli = ServerRefereeCli(api, output_func=outputs.append)
        cli.handle_input(f"#join {session_id}")

        agent.refresh_tasks()
        agent.tick_sessions()

        self.assertEqual(irc.sent, [])
        self.assertTrue(store.load_session(session_id).state.human_controlled)

        cli.handle_input("/ai")
        agent.refresh_tasks()
        agent.tick_sessions()
        self.assertEqual(irc.sent, [("BanchoBot", "!mp make Manual")])

    def test_json_import(self) -> None:
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
            json_store = RefereeStore(source)
            rulepack = RulePack(id="imported", name="Imported", confirmed=True)
            json_store.save_rulepack(rulepack)
            default_pack = json_store.ensure_default_rulepack()
            self.assertEqual(default_pack.id, "default")
            sqlite_store = SQLiteRefereeStore(Path(dest) / "referee.db")
            try:
                counts = import_json_store(source, sqlite_store)
                self.assertGreaterEqual(counts["rulepacks"], 1)
                self.assertIsNotNone(sqlite_store.load_rulepack("imported"))
            finally:
                sqlite_store.close()

    def test_main_modes_have_help(self) -> None:
        for mode in ("server", "agent", "referee"):
            result = subprocess.run(
                [sys.executable, "main.py", mode, "--help"],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

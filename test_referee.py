import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_referee import OpenAICompatibleClient, load_ai_config
from referee import (
    RefereeEngine,
    RefereeStore,
    SessionState,
    Team,
    default_rulepack,
    parse_menu_command,
    rulepack_from_draft,
)
from referee_cli import RefereeSupervisor


class FakeClient:
    def __init__(self) -> None:
        self.sent = []
        self.joined = []
        self.messages = []
        self._running = True
        self.on_message = None

    def send(self, target: str, text: str) -> None:
        self.sent.append((target, text))

    def join(self, channel: str) -> None:
        self.joined.append(channel)

    def drain_messages(self):
        messages = list(self.messages)
        self.messages.clear()
        return messages

    def disconnect(self) -> None:
        self._running = False


class RefereeRulesTest(unittest.TestCase):
    def test_default_rules_fill_missing_rulepack_fields(self) -> None:
        pack = rulepack_from_draft("Cup Rules", draft={"bp_timer": "", "format": {"best_of": 7}})

        self.assertEqual(pack.bp_timer, 90)
        self.assertEqual(pack.join_timer, 120)
        self.assertEqual(pack.ready_start_countdown, 7)
        self.assertEqual(pack.format, {"best_of": 7})
        self.assertEqual(pack.team_template, ["red", "blue"])
        self.assertIn("bp_timer", pack.defaulted_fields)
        self.assertIn("join_timer", pack.defaulted_fields)
        self.assertFalse(pack.confirmed)

    def test_non_default_timers_parse_as_integers(self) -> None:
        pack = rulepack_from_draft(
            "Fast Rules",
            draft={"bp_timer": "80", "join_timer": 130, "ready_start_countdown": "5"},
        )

        self.assertEqual(pack.bp_timer, 80)
        self.assertEqual(pack.join_timer, 130)
        self.assertEqual(pack.ready_start_countdown, 5)
        self.assertNotIn("bp_timer", pack.defaulted_fields)
        self.assertNotIn("join_timer", pack.defaulted_fields)

    def test_menu_command_parser(self) -> None:
        self.assertEqual(parse_menu_command("list").name, "list")
        self.assertEqual(parse_menu_command("#join session-abc").args, ["session-abc"])
        self.assertEqual(parse_menu_command("resume session-abc").name, "resume")
        self.assertEqual(parse_menu_command("wat").name, "unknown")

    def test_ai_config_file_and_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                '{"ai":{"base_url":"https://example.test","api_key":"file-key","model":"file-model",'
                '"thinking":{"type":"disabled"}}}',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"AI_MODEL": "env-model"}, clear=False):
                config = load_ai_config(path)

        self.assertEqual(config["base_url"], "https://example.test")
        self.assertEqual(config["api_key"], "file-key")
        self.assertEqual(config["model"], "env-model")
        self.assertEqual(config["extra_body"]["thinking"], {"type": "disabled"})

    def test_ai_client_uses_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                '{"ai":{"base_url":"https://vendor.test","api_key":"secret","model":"vendor-model"}}',
                encoding="utf-8",
            )
            with patch("ai_referee.DEFAULT_CONFIG_PATH", path):
                client = OpenAICompatibleClient.from_env()

        self.assertIsNotNone(client)
        self.assertEqual(client.base_url, "https://vendor.test")
        self.assertEqual(client.api_key, "secret")
        self.assertEqual(client.model, "vendor-model")


class RefereeEngineTest(unittest.TestCase):
    def make_supervisor(self):
        temp = tempfile.TemporaryDirectory()
        store = RefereeStore(temp.name)
        supervisor = RefereeSupervisor(
            FakeClient(),
            store=store,
            input_func=lambda prompt: "",
            output_func=lambda text: None,
        )
        supervisor.load()
        self.addCleanup(temp.cleanup)
        return supervisor

    def test_low_risk_room_flow_uses_default_timers(self) -> None:
        supervisor = self.make_supervisor()
        pack = default_rulepack()
        session = supervisor.create_session(
            name="Alpha vs Beta",
            rulepack=pack,
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )

        supervisor.tick_sessions()
        self.assertEqual(supervisor.client.sent, [("BanchoBot", "!mp make Alpha vs Beta")])
        self.assertEqual(session.state.stage, "creating_room")

        supervisor.route_message("BanchoBot", "Created the tournament match https://osu.ppy.sh/mp/123")
        supervisor.tick_sessions()
        sent_text = [text for _, text in supervisor.client.sent]

        self.assertIn("!mp invite Alice", sent_text)
        self.assertIn("!mp invite Bob", sent_text)
        self.assertIn("!mp timer 120", sent_text)
        self.assertIn("!mp timer 90", sent_text)
        self.assertEqual(session.state.stage, "waiting_players")

    def test_ref_ready_requests_settings_not_start(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Ready Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(session_id=session.id, stage="waiting_players", channel="#mp_1")

        engine.handle_message(session, "#mp_1", "<Alice> ready")
        self.assertEqual(session.state.ready_players, [])

        engine.handle_message(session, "#mp_1", "<Alice> !ref ready")
        actions = engine.next_actions(session)

        self.assertEqual([action.text for action in actions], ["!mp settings"])

    def test_system_all_ready_triggers_start_7(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="System Ready Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(session_id=session.id, stage="waiting_players", channel="#mp_1")

        engine.handle_message(session, "#mp_1", "<BanchoBot> All players are ready")
        actions = engine.next_actions(session)

        self.assertEqual([action.text for action in actions], ["!mp start 7"])

    def test_system_tag_all_ready_triggers_start_7(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="System Tag Ready Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(session_id=session.id, stage="waiting_players", channel="#mp_1")

        engine.handle_message(session, "SYSTEM", "All players are ready")
        actions = engine.next_actions(session)

        self.assertEqual([action.text for action in actions], ["!mp start 7"])

    def test_ref_ready_can_request_settings_again(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Repeated Settings",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(session_id=session.id, stage="waiting_players", channel="#mp_1")

        engine.handle_message(session, "#mp_1", "<Alice> !ref ready")
        first = engine.next_actions(session)[0]
        engine.mark_action_sent(session, first)
        engine.handle_message(session, "#mp_1", "<Bob> !ref ready")
        second = engine.next_actions(session)

        self.assertEqual([action.text for action in second], ["!mp settings"])

    def test_join_timer_end_triggers_start_without_pause(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Timer Ready Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(
            session_id=session.id,
            stage="waiting_players",
            channel="#mp_1",
            join_timer_deadline=time.time() - 1,
        )

        actions = engine.next_actions(session)

        self.assertEqual([action.text for action in actions], ["!mp start 7"])

    def test_join_timer_end_does_not_start_after_player_pause(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Paused Timer Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(
            session_id=session.id,
            stage="waiting_players",
            channel="#mp_1",
            join_timer_deadline=time.time() - 1,
        )

        engine.handle_message(session, "#mp_1", "<Alice> !ref pause")

        self.assertEqual(engine.next_actions(session), [])
        self.assertTrue(session.state.paused)

    def test_unprefixed_player_messages_are_ignored(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Ignored Chat",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state = SessionState(session_id=session.id, stage="waiting_players", channel="#mp_1")

        engine.handle_message(session, "#mp_1", "<Alice> please pause")
        engine.handle_message(session, "#mp_1", "<Alice> ready")

        self.assertEqual(session.state.ready_players, [])

    def test_human_takeover_blocks_automation_until_resume(self) -> None:
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Manual Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state.stage = "waiting_players"
        session.state.channel = "#mp_7"
        session.state.all_ready_confirmed = True

        supervisor.join_for_human(session.id)
        supervisor.tick_sessions()
        self.assertEqual(supervisor.client.sent, [])
        self.assertTrue(session.state.human_controlled)

        supervisor.handle_manual_input("/ai")
        supervisor.tick_sessions()
        self.assertEqual(supervisor.client.sent, [("#mp_7", "!mp start 7")])

    def test_pause_from_human_control_resumes_previous_stage(self) -> None:
        engine = RefereeEngine()
        supervisor = self.make_supervisor()
        session = supervisor.create_session(
            name="Pause Match",
            rulepack=default_rulepack(),
            teams=[Team("Alpha", ["Alice"]), Team("Beta", ["Bob"])],
        )
        session.state.stage = "waiting_players"

        engine.enter_human_control(session)
        engine.pause(session, "exit")
        engine.resume(session)

        self.assertFalse(session.state.human_controlled)
        self.assertEqual(session.state.stage, "waiting_players")

    def test_multi_session_messages_route_by_room(self) -> None:
        supervisor = self.make_supervisor()
        one = supervisor.create_session(
            name="One",
            rulepack=default_rulepack(),
            teams=[Team("A", ["Alice"]), Team("B", ["Bob"])],
        )
        two = supervisor.create_session(
            name="Two",
            rulepack=default_rulepack(),
            teams=[Team("C", ["Carol"]), Team("D", ["Dave"])],
        )
        one.state.stage = two.state.stage = "waiting_players"
        one.state.channel = "#mp_1"
        two.state.channel = "#mp_2"

        supervisor.route_message("#mp_1", "<Alice> !ref ready")
        supervisor.route_message("#mp_2", "<Dave> !ref ready")

        self.assertEqual(one.state.ready_players, ["Alice"])
        self.assertEqual(two.state.ready_players, ["Dave"])


if __name__ == "__main__":
    unittest.main()

"""End-to-end replay of a real BO11 match (#mp_121270745) through the referee.

tests/fixtures/match_121270745.log holds the BanchoBot lines from an actual
human-refereed Best-of-11. The mappool/rulebook the referee uses is parsed (in
production by an LLM) from tests/fixtures/mappool_121270745.txt; the confirmed
structured result is cached as tests/fixtures/rulebook_121270745.golden.json so
this test stays deterministic and offline.

best_of is supplied when the room/session is opened (not baked into the
rulebook). Given that and structured !ref pick decisions reconstructed from the
log, the engine must reproduce the full match: 8 formal maps, the score going
0-0 -> 6-2, FireBanana winning, and the room being closed.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from my_osuirc.ai.client import OpenAICompatibleClient
from my_osuirc.referee.core import (
    MATCH_FINISHED_RE,
    RefereeEngine,
    RulePack,
    mappool_lookup,
    parse_chat_sender,
    parse_play_result,
    rulepack_from_draft,
)
from my_osuirc.referee.server import SQLiteRefereeStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_PATH = FIXTURES / "rulebook_121270745.golden.json"
MAPPOOL_SRC_PATH = FIXTURES / "mappool_121270745.txt"
FIXTURE_PATH = FIXTURES / "match_121270745.log"
CHANNEL = "#mp_121270745"
BEATMAP_RE = re.compile(r"Changed beatmap to https://osu\.ppy\.sh/b/(\d+)")


class FakeIrcClient:
    def __init__(self, nick: str = "RefBot") -> None:
        self.nick = nick
        self.sent: list[tuple[str, str]] = []

    def send(self, target: str, text: str) -> None:
        self.sent.append((target, text))


def load_rulebook() -> RulePack:
    """The confirmed structured rulepack (what a human accepts after AI parsing)."""
    draft = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return rulepack_from_draft(name=draft["name"], draft=draft, confirmed=True)


def parse_log_rounds(pool_ids: set[int]) -> list[tuple[int, list[tuple[str, int]]]]:
    """Walk the fixture and return (beatmap_id, [(player, score), ...]) per formal map.

    Maps not present in the rulebook mappool (e.g. the warmup) are skipped, which
    is exactly how an autonomous referee distinguishes a counted map from a warmup.
    """
    rounds: list[tuple[int, list[tuple[str, int]]]] = []
    current_id: int | None = None
    results: list[tuple[str, int]] = []
    for raw in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        parsed = parse_chat_sender(raw.strip())
        if not parsed:
            continue
        sender, message = parsed
        if sender.lower() != "banchobot":
            continue
        beatmap = BEATMAP_RE.search(message)
        if beatmap:
            current_id = int(beatmap.group(1))
            results = []
            continue
        play = parse_play_result(message)
        if play:
            results.append(play)
            continue
        if MATCH_FINISHED_RE.search(message):
            if current_id is not None and current_id in pool_ids:
                rounds.append((current_id, list(results)))
            current_id, results = None, []
    return rounds


class Bo11ReplayTest(unittest.TestCase):
    def _build_session(self, best_of: int = 11):
        """Import the rulebook through the SQLite store, then open the match room."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = SQLiteRefereeStore(Path(temp.name) / "referee.db")
        self.addCleanup(store.close)

        pack = load_rulebook()
        store.save_rulepack(pack)
        store.confirm_rulepack(pack.id)

        session = store.create_scheduled_session(
            name="o!TA:N S1: (FireBanana) vs (fate80016)",
            rulepack_id=pack.id,
            teams=[
                {"name": "FireBanana", "players": ["FireBanana"]},
                {"name": "fate80016", "players": ["fate80016"]},
            ],
            best_of=best_of,  # specified when the room is opened
        )
        return store.load_session(session.id)

    def _tick(self, engine, session, irc) -> None:
        for action in engine.next_actions(session):
            if action.risk != "low":
                continue
            irc.send(action.target, action.text)
            engine.mark_action_sent(session, action)

    def _settle(self, engine, session, irc, max_iters: int = 12) -> None:
        for _ in range(max_iters):
            actions = engine.next_actions(session)
            if not actions:
                return
            for action in actions:
                if action.risk != "low":
                    continue
                irc.send(action.target, action.text)
                engine.mark_action_sent(session, action)
        self.fail("engine did not settle; possible action loop")

    def _feed(self, engine, session, text: str) -> None:
        engine.handle_message(session, CHANNEL, text)

    def _advance_to_bo_pick(self, engine, session, irc) -> None:
        self._tick(engine, session, irc)  # !mp make
        engine.handle_message(
            session, "BanchoBot", "Created the tournament match https://osu.ppy.sh/mp/121270745"
        )
        self._tick(engine, session, irc)  # invites + timers -> bo_pick
        self.assertEqual(session.state.stage, "bo_pick")
        self.assertEqual(session.state.channel, CHANNEL)

    def test_best_of_comes_from_the_session_not_the_rulebook(self) -> None:
        session = self._build_session(best_of=11)
        self.assertNotIn("best_of", session.rulepack.format)  # rulebook stays best-of agnostic
        self.assertTrue(session.is_bo_mode)
        self.assertEqual(session.best_of, 11)
        self.assertEqual(session.first_to, 6)
        self.assertEqual(len(session.rulepack.mappool), 18)
        self.assertEqual(mappool_lookup(session.rulepack, "dt3")["map_command"], "!mp map 3618329 0")

        plain = self._build_session(best_of=0)  # no best_of -> legacy (non-BO) flow
        self.assertFalse(plain.is_bo_mode)

    def test_full_match_replays_to_six_two_and_closes(self) -> None:
        session = self._build_session(best_of=11)
        engine = RefereeEngine()
        irc = FakeIrcClient()

        self._advance_to_bo_pick(engine, session, irc)

        pool_ids = {entry["beatmap_id"] for entry in session.rulepack.mappool}
        id_to_code = {entry["beatmap_id"]: entry["code"] for entry in session.rulepack.mappool}
        rounds = parse_log_rounds(pool_ids)
        self.assertEqual(len(rounds), 8, "expected 8 formal maps (warmup skipped)")

        expected_progression = [
            (0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (5, 2), (6, 2),
        ]
        played_map_cmds: list[str] = []

        for index, (beatmap_id, results) in enumerate(rounds):
            code = id_to_code[beatmap_id]
            # A captain picks the map via the structured referee command.
            self._feed(engine, session, f"<FireBanana> !ref pick {code}")
            self._settle(engine, session, irc)
            self.assertEqual(session.state.stage, "bo_ready")
            self.assertIn((CHANNEL, f"!mp map {beatmap_id} 0"), irc.sent)
            played_map_cmds.append(f"!mp map {beatmap_id} 0")

            # Players ready up; the referee runs settings then starts the map.
            self._feed(engine, session, "<BanchoBot> All players are ready")
            self._settle(engine, session, irc)
            self.assertEqual(session.state.stage, "bo_playing")
            self.assertIn((CHANNEL, "!mp start 7"), irc.sent)

            # Replay the real per-player scores and the match-finished line.
            for player, score in results:
                self._feed(
                    engine, session, f"<BanchoBot> {player} finished playing (Score: {score}, PASSED)."
                )
            self._feed(engine, session, "<BanchoBot> The match has finished!")
            self._settle(engine, session, irc)

            red, blue = expected_progression[index]
            self.assertEqual(session.state.score["FireBanana"], red, f"map {index + 1} red score")
            self.assertEqual(session.state.score["fate80016"], blue, f"map {index + 1} blue score")

        # Final outcome: FireBanana 6-2, match closed.
        self.assertEqual(session.state.match_winner, "FireBanana")
        self.assertEqual(session.state.stage, "finished")
        self.assertIn((CHANNEL, "!mp close"), irc.sent)
        announces = [text for target, text in irc.sent if target == CHANNEL and " // " in text]
        self.assertEqual(announces[-1], "FireBanana | 6 - 2 | fate80016 // FireBanana wins! GGWP")
        # The 8 picked maps were set in the exact order they were played.
        self.assertEqual(
            played_map_cmds,
            [
                "!mp map 1636388 0",
                "!mp map 5698550 0",
                "!mp map 5223058 0",
                "!mp map 5497651 0",
                "!mp map 4869621 0",
                "!mp map 3634672 0",
                "!mp map 5235326 0",
                "!mp map 3618329 0",
            ],
        )


class RulebookExtractionTest(unittest.TestCase):
    def test_extract_rulepack_parses_local_mappool_text(self) -> None:
        """The model parses the rulebook text; here chat is stubbed for determinism."""
        mappool_text = MAPPOOL_SRC_PATH.read_text(encoding="utf-8")
        golden_json = GOLDEN_PATH.read_text(encoding="utf-8")
        captured: dict[str, str] = {}

        def fake_chat(prompt: str) -> str:
            captured["prompt"] = prompt
            return golden_json

        client = OpenAICompatibleClient(base_url="http://x", api_key="k", model="m")
        with patch.object(client, "chat", side_effect=fake_chat):
            draft = client.extract_rulepack(name="o!TA:N S1 Mappool", mappool_text=mappool_text)

        # The local text reached the prompt (no network fetch needed).
        self.assertIn("NM1", captured["prompt"])
        self.assertIn("!mp map 5223058 0", captured["prompt"])

        pack = rulepack_from_draft(name="o!TA:N S1 Mappool", draft=draft, confirmed=True)
        self.assertEqual(len(pack.mappool), 18)
        self.assertEqual(mappool_lookup(pack, "TB")["mod_command"], "!mp mods NF Freemod")


class Bo11CoreUnitTest(unittest.TestCase):
    def test_parse_play_result(self) -> None:
        self.assertEqual(
            parse_play_result("FireBanana finished playing (Score: 371840, PASSED)."),
            ("FireBanana", 371840),
        )
        self.assertEqual(
            parse_play_result("[SHK]Dragon-Fox finished playing (Score: 1,234,567, FAILED)"),
            ("[SHK]Dragon-Fox", 1234567),
        )
        self.assertIsNone(parse_play_result("The match has finished!"))

    def test_mappool_lookup_is_case_insensitive(self) -> None:
        pack = load_rulebook()
        self.assertEqual(mappool_lookup(pack, "Nm3")["beatmap_id"], 1636388)
        self.assertIsNone(mappool_lookup(pack, "ZZ9"))

    def test_single_map_scores_and_announces(self) -> None:
        pack = load_rulebook()
        from my_osuirc.referee.core import RefereeSession, SessionConfig, SessionState, Team

        config = SessionConfig(
            id="s1",
            name="m",
            rulepack_id=pack.id,
            teams=[Team("FireBanana", ["FireBanana"]), Team("fate80016", ["fate80016"])],
            best_of=11,
        )
        state = SessionState(
            session_id="s1",
            stage="bo_playing",
            channel=CHANNEL,
            current_pick_code="NM3",
            score={"FireBanana": 0, "fate80016": 0},
        )
        session = RefereeSession(config=config, rulepack=pack, state=state)
        engine = RefereeEngine()

        engine.handle_message(session, CHANNEL, "<BanchoBot> FireBanana finished playing (Score: 100, PASSED).")
        engine.handle_message(session, CHANNEL, "<BanchoBot> fate80016 finished playing (Score: 200, PASSED).")
        engine.handle_message(session, CHANNEL, "<BanchoBot> The match has finished!")

        self.assertEqual(session.state.score["fate80016"], 1)
        self.assertEqual(session.state.stage, "bo_pick")
        self.assertIn("0 - 1", session.state.pending_announce)


if __name__ == "__main__":
    unittest.main()

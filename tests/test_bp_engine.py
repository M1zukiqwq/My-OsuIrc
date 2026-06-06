"""Managed ban/pick engine tests (rulebook chapter 三).

Active only when the rulebook declares a `format.bp_order`. Covers roll order
resolution, configurable BP templates (variable ban count), turn enforcement,
ban/pick validity, and TB gating until both teams reach match point.
"""

import unittest

from my_osuirc.referee.core import (
    RefereeEngine,
    RefereeSession,
    RulePack,
    SessionConfig,
    SessionState,
    Team,
)

CHANNEL = "#mp_1"
POOL = [
    {"code": "NM1", "beatmap_id": 101, "mods": "NF", "map_command": "!mp map 101 0", "mod_command": "!mp mods NF"},
    {"code": "NM2", "beatmap_id": 102, "mods": "NF", "map_command": "!mp map 102 0", "mod_command": "!mp mods NF"},
    {"code": "NM3", "beatmap_id": 103, "mods": "NF", "map_command": "!mp map 103 0", "mod_command": "!mp mods NF"},
    {"code": "NM4", "beatmap_id": 104, "mods": "NF", "map_command": "!mp map 104 0", "mod_command": "!mp mods NF"},
    {"code": "TB", "beatmap_id": 999, "mods": "NF Freemod", "map_command": "!mp map 999 0",
     "mod_command": "!mp mods NF Freemod"},
]


class FakeIrc:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, target: str, text: str) -> None:
        self.sent.append((target, text))


class BpEngineTest(unittest.TestCase):
    def make_session(self, bp_order, best_of=3, first_to=2) -> RefereeSession:
        pack = RulePack(
            id="p", name="P", confirmed=True,
            format={"bp_order": bp_order, "team_mode": "TeamVs", "win_condition": "ScoreV2"},
            mappool=[dict(entry) for entry in POOL],
        )
        config = SessionConfig(
            id="s", name="m", rulepack_id="p",
            teams=[Team("Red", ["Alice"]), Team("Blue", ["Bob"])],
            best_of=best_of, first_to=first_to,
        )
        state = SessionState(session_id="s", stage="bo_roll", channel=CHANNEL, score={"Red": 0, "Blue": 0})
        return RefereeSession(config=config, rulepack=pack, state=state)

    def setUp(self) -> None:
        self.engine = RefereeEngine()
        self.irc = FakeIrc()

    def feed(self, session, text: str) -> None:
        self.engine.handle_message(session, CHANNEL, text)

    def settle(self, session, max_iters: int = 20) -> None:
        for _ in range(max_iters):
            actions = self.engine.next_actions(session)
            if not actions:
                return
            for action in actions:
                self.irc.send(action.target, action.text)
                self.engine.mark_action_sent(session, action)
        self.fail("engine did not settle")

    def resolve_order(self, session, winner_choice=("Alice", "pick", "first"),
                      loser_choice=("Bob", "ban", "first"), alice_roll=60, bob_roll=40) -> None:
        self.feed(session, f"<BanchoBot> Alice rolls {alice_roll} point(s)")
        self.feed(session, f"<BanchoBot> Bob rolls {bob_roll} point(s)")
        self.feed(session, f"<{winner_choice[0]}> !ref {winner_choice[1]} {winner_choice[2]}")
        self.feed(session, f"<{loser_choice[0]}> !ref {loser_choice[1]} {loser_choice[2]}")

    def play_map(self, session, picker: str, code: str, alice_score: int, bob_score: int) -> None:
        self.feed(session, f"<{picker}> !ref pick {code}")
        self.settle(session)
        self.feed(session, "<BanchoBot> All players are ready")
        self.settle(session)
        self.feed(session, f"<BanchoBot> Alice finished playing (Score: {alice_score}, PASSED).")
        self.feed(session, f"<BanchoBot> Bob finished playing (Score: {bob_score}, PASSED).")
        self.feed(session, "<BanchoBot> The match has finished!")
        self.settle(session)

    def test_roll_resolves_pick_and_ban_order(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)
        self.assertEqual(session.state.pick_order, ["Red", "Blue"])  # winner Red chose pick first
        self.assertEqual(session.state.ban_order, ["Blue", "Red"])   # loser Blue chose ban first
        self.assertEqual(session.state.stage, "bo_pick")

    def test_roll_tie_forces_reroll(self) -> None:
        session = self.make_session(["pick"])
        self.feed(session, "<BanchoBot> Alice rolls 50 point(s)")
        self.feed(session, "<BanchoBot> Bob rolls 50 point(s)")
        self.assertEqual(session.state.rolls, {})  # tie cleared
        self.assertIn("平局", session.state.pending_announce)

    def test_only_team_on_turn_can_pick(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)  # pick_order [Red, Blue] -> Red (Alice) picks first
        self.settle(session)
        self.feed(session, "<Bob> !ref pick NM1")        # wrong team
        self.assertEqual(session.state.current_pick_code, "")
        self.feed(session, "<Alice> !ref pick NM1")      # correct team
        self.assertEqual(session.state.current_pick_code, "NM1")
        self.settle(session)
        self.assertIn((CHANNEL, "!mp map 101 0"), self.irc.sent)

    def test_ban_rules_two_bans_and_tb_protected(self) -> None:
        session = self.make_session(["ban", "ban", "pick"])
        # winner Red picks-first; loser Blue bans-first -> ban_order [Blue, Red]
        self.resolve_order(session)
        self.settle(session)
        self.assertEqual(self.engine._bp_current(session), ("ban", "Blue"))

        self.feed(session, "<Bob> !ref ban TB")          # cannot ban TB
        self.assertEqual(session.state.banned_codes, [])
        self.feed(session, "<Alice> !ref ban NM4")       # not Blue's... Red's turn? no, Blue first
        self.assertEqual(session.state.banned_codes, [])
        self.feed(session, "<Bob> !ref ban NM4")         # Blue bans
        self.assertEqual(session.state.banned_codes, ["NM4"])
        self.settle(session)
        self.assertEqual(self.engine._bp_current(session), ("ban", "Red"))
        self.feed(session, "<Alice> !ref ban NM3")       # Red bans
        self.assertEqual(session.state.banned_codes, ["NM4", "NM3"])
        self.settle(session)

        # Now pick phase; a banned map cannot be picked.
        self.assertEqual(self.engine._bp_current(session), ("pick", "Red"))
        self.feed(session, "<Alice> !ref pick NM4")      # banned
        self.assertEqual(session.state.current_pick_code, "")
        self.feed(session, "<Alice> !ref pick NM1")
        self.assertEqual(session.state.current_pick_code, "NM1")

    def test_tb_only_pickable_at_double_match_point(self) -> None:
        session = self.make_session(["pick"], best_of=3, first_to=2)
        self.resolve_order(session)  # pick_order [Red, Blue]
        self.settle(session)
        self.play_map(session, "Alice", "NM1", alice_score=100, bob_score=500)  # Blue wins -> 0-1
        self.play_map(session, "Bob", "NM2", alice_score=500, bob_score=100)    # Red wins -> 1-1
        self.assertTrue(self.engine._both_match_point(session))

        # At double match point, a normal map is refused; only TB is allowed.
        self.feed(session, "<Alice> !ref pick NM3")
        self.assertEqual(session.state.current_pick_code, "")
        self.feed(session, "<Alice> !ref pick TB")
        self.assertEqual(session.state.current_pick_code, "TB")
        self.settle(session)
        self.assertIn((CHANNEL, "!mp map 999 0"), self.irc.sent)

    def test_ban_timeout_waives(self) -> None:
        session = self.make_session(["ban", "ban"])
        self.resolve_order(session)  # ban_order [Blue, Red]; first ban turn = Blue
        self.settle(session)
        self.assertEqual(self.engine._bp_current(session), ("ban", "Blue"))
        deadline = session.state.turn_deadline
        self.assertGreater(deadline, 0)
        self.engine.tick_timeouts(session, now=deadline + 1)
        self.assertEqual(session.state.banned_codes, [])  # nothing banned
        self.assertEqual(session.state.ban_count, 1)      # turn advanced (waived)
        self.assertEqual(self.engine._bp_current(session), ("ban", "Red"))

    def test_pick_timeout_auto_picks(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)
        self.settle(session)
        self.assertEqual(self.engine._bp_current(session), ("pick", "Red"))
        deadline = session.state.turn_deadline
        self.assertGreater(deadline, 0)
        self.engine.tick_timeouts(session, now=deadline + 1)
        self.assertEqual(session.state.current_pick_code, "NM1")  # first available map
        self.assertIn("超时", session.state.pending_announce)

    def test_forfeit_ends_match_for_opponent(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)
        self.settle(session)
        self.play_map(session, "Alice", "NM1", alice_score=900, bob_score=100)  # Red 1-0
        self.feed(session, "<Bob> !ref ff")
        self.settle(session)
        self.assertEqual(session.state.match_winner, "Red")
        self.assertEqual(session.state.stage, "finished")
        self.assertIn((CHANNEL, "!mp close"), self.irc.sent)
        self.assertTrue(any("FF" in t for _, t in self.irc.sent))

    def test_pause_budget_phase_gate_and_auto_resume(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)
        self.settle(session)  # bo_pick, Red to pick
        self.feed(session, "<Bob> !ref pause")
        self.assertTrue(session.state.paused)
        self.assertEqual(session.state.pause_counts.get("Bob"), 1)
        # auto-resume after the pause window returns to the previous stage
        self.engine.tick_timeouts(session, now=session.state.pause_deadline + 1)
        self.assertFalse(session.state.paused)
        self.assertEqual(session.state.stage, "bo_pick")
        # same side cannot pause twice in the same map
        self.feed(session, "<Bob> !ref pause")
        self.assertFalse(session.state.paused)

    def test_pause_refused_during_play(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)
        self.settle(session)
        self.feed(session, "<Alice> !ref pick NM1")
        self.settle(session)
        self.feed(session, "<BanchoBot> All players are ready")
        self.settle(session)
        self.assertEqual(session.state.stage, "bo_playing")
        self.feed(session, "<Bob> !ref pause")  # not allowed once the map is running
        self.assertFalse(session.state.paused)

    def test_abort_within_window_replays_same_map(self) -> None:
        session = self.make_session(["pick"])
        self.resolve_order(session)
        self.settle(session)
        self.feed(session, "<Alice> !ref pick NM1")
        self.settle(session)
        self.feed(session, "<BanchoBot> All players are ready")
        self.settle(session)
        self.assertEqual(session.state.stage, "bo_playing")
        self.feed(session, "<Alice> !ref abort")
        self.assertTrue(session.state.pending_abort)
        self.settle(session)
        self.assertIn((CHANNEL, "!mp abort"), self.irc.sent)
        self.assertEqual(session.state.stage, "bo_ready")
        self.assertEqual(session.state.current_pick_code, "NM1")  # same map kept
        self.feed(session, "<BanchoBot> All players are ready")
        self.settle(session)
        self.assertGreaterEqual(len([t for _, t in self.irc.sent if t == "!mp start 7"]), 2)

    def test_full_bracket_with_bans_runs_to_winner_and_closes(self) -> None:
        session = self.make_session(["ban", "ban", "pick"], best_of=3, first_to=2)
        self.resolve_order(session)
        self.settle(session)
        self.feed(session, "<Bob> !ref ban NM4")
        self.settle(session)
        self.feed(session, "<Alice> !ref ban NM3")
        self.settle(session)
        # Red picks first, wins; then Blue picks, Red wins again -> Red 2-0.
        self.play_map(session, "Alice", "NM1", alice_score=900, bob_score=100)
        self.play_map(session, "Bob", "NM2", alice_score=900, bob_score=100)
        self.assertEqual(session.state.score, {"Red": 2, "Blue": 0})
        self.assertEqual(session.state.match_winner, "Red")
        self.assertEqual(session.state.stage, "finished")
        self.assertIn((CHANNEL, "!mp close"), self.irc.sent)


if __name__ == "__main__":
    unittest.main()

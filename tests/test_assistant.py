"""AI assistant layer: read-only Q&A / summary grounded on a deterministic snapshot.

The assistant never computes score/turn/legality and never sends !mp; it only
phrases/explains. These tests stub the LLM (no network) and verify grounding,
graceful fallback, and that the agent routes !ref ? / !ref ask correctly.
"""

import unittest

from my_osuirc.referee.agent import RefereeAgent
from my_osuirc.referee.assistant import RefereeAssistant
from my_osuirc.referee.core import (
    RefereeEngine,
    RefereeSession,
    RulePack,
    SessionConfig,
    SessionState,
    Team,
    bp_command_error,
    classify_mp_command,
    classify_ref_message,
    format_status,
    mappool_status,
)

CHANNEL = "#mp_1"


class StubAI:
    """Records the prompt/system and returns a canned reply (no network)."""

    def __init__(self, reply: str = "测试回答") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def chat(self, prompt: str, system: str | None = None) -> str:
        self.calls.append((system or "", prompt))
        return self.reply


class FakeIrc:
    def __init__(self) -> None:
        self.nick = "RefBot"
        self.sent: list[tuple[str, str]] = []
        self._running = True

    def send(self, target: str, text: str) -> None:
        self.sent.append((target, text))

    def join(self, channel: str) -> None:
        pass


def make_session() -> RefereeSession:
    pack = RulePack(
        id="p", name="P", confirmed=True,
        format={"bp_order": ["pick", "ban", "pick", "ban"], "team_mode": "TeamVs", "win_condition": "ScoreV2"},
        mappool=[
            {"code": "NM1", "beatmap_id": 101, "mods": "NF"},
            {"code": "NM2", "beatmap_id": 102, "mods": "NF"},
            {"code": "TB", "beatmap_id": 999, "mods": "NF"},
        ],
    )
    config = SessionConfig(id="s", name="m", rulepack_id="p",
                           teams=[Team("Red", ["Alice"]), Team("Blue", ["Bob"])], best_of=3)
    state = SessionState(session_id="s", stage="bo_pick", channel=CHANNEL, score={"Red": 1, "Blue": 0},
                         pick_order=["Red", "Blue"], ban_order=["Blue", "Red"], banned_codes=["NM1"])
    return RefereeSession(config=config, rulepack=pack, state=state)


class DescribeStateTest(unittest.TestCase):
    def test_snapshot_is_deterministic_facts(self) -> None:
        session = make_session()
        snap = RefereeEngine().describe_state(session)
        self.assertEqual(snap["score"], {"Red": 1, "Blue": 0})
        self.assertEqual(snap["turn"], {"action": "pick", "team": "Red"})
        self.assertEqual(snap["banned"], ["NM1"])
        self.assertFalse(snap["tb_available"])  # not at double match point
        self.assertEqual(snap["first_to"], 2)

    def test_format_status_one_liner(self) -> None:
        snap = RefereeEngine().describe_state(make_session())
        line = format_status(snap)
        self.assertIn("轮到 Red pick", line)
        self.assertIn("比分", line)
        self.assertIn("TB 未解锁", line)


class AssistantTest(unittest.TestCase):
    def test_answer_grounds_on_snapshot_and_rules(self) -> None:
        ai = StubAI("还能 ban 1 张")
        assistant = RefereeAssistant(ai, rules_text="不可 ban TB；TB 仅双方赛点后可选。")
        snap = RefereeEngine().describe_state(make_session())
        out = assistant.answer("还能ban几张？", snap)
        self.assertEqual(out, "还能 ban 1 张")
        system, prompt = ai.calls[0]
        self.assertIn("裁判助理", system)
        self.assertIn("还能ban几张", prompt)
        self.assertIn("不可 ban TB", prompt)   # rules text included
        self.assertIn("\"turn\"", prompt)       # snapshot included

    def test_answer_empty_without_ai(self) -> None:
        assistant = RefereeAssistant(None)
        self.assertEqual(assistant.answer("?", {}), "")

    def test_summarize_uses_log_and_state(self) -> None:
        ai = StubAI("Red 2-0 获胜，无争议。")
        assistant = RefereeAssistant(ai)
        out = assistant.summarize("<Alice> gg\n<Bob> gg", {"score": {"Red": 2, "Blue": 0}})
        self.assertEqual(out, "Red 2-0 获胜，无争议。")
        self.assertIn("复盘", ai.calls[0][0])


class AgentRoutingTest(unittest.TestCase):
    def _agent(self, assistant):
        irc = FakeIrc()
        agent = RefereeAgent(irc, api=None, agent_id="a", output_func=lambda *_: None, assistant=assistant)
        session = make_session()
        agent.sessions[session.id] = session
        return agent, irc, session

    def test_ref_question_mark_gives_deterministic_status(self) -> None:
        agent, irc, _ = self._agent(RefereeAssistant(None))
        agent._maybe_assist(agent.sessions["s"], "<Alice> !ref ?")
        self.assertEqual(len(irc.sent), 1)
        self.assertIn("轮到 Red pick", irc.sent[0][1])

    def test_ref_ask_with_ai_answers(self) -> None:
        agent, irc, _ = self._agent(RefereeAssistant(StubAI("轮到红队 pick")))
        agent._maybe_assist(agent.sessions["s"], "<Bob> !ref ask 现在该谁")
        # answer runs in a background thread; wait briefly for it to post
        for _ in range(50):
            if irc.sent:
                break
            import time
            time.sleep(0.01)
        self.assertEqual(irc.sent[-1], (CHANNEL, "轮到红队 pick"))

    def test_ban_command_is_not_treated_as_question(self) -> None:
        agent, irc, _ = self._agent(RefereeAssistant(StubAI("should not fire")))
        agent._maybe_assist(agent.sessions["s"], "<Bob> !ref ban TB")  # a real command
        self.assertEqual(irc.sent, [])
        agent._maybe_assist(agent.sessions["s"], "<Alice> 普通聊天不触发")
        self.assertEqual(irc.sent, [])


class MappoolTableTest(unittest.TestCase):
    def test_status_table(self) -> None:
        table = {row["code"]: row["status"] for row in mappool_status(make_session())}
        self.assertEqual(table, {"NM1": "banned", "NM2": "available", "TB": "available"})

    def test_command_error(self) -> None:
        session = make_session()  # NM1 banned
        self.assertEqual(bp_command_error(session, "ban", "NM9"), "not_in_pool")
        self.assertEqual(bp_command_error(session, "pick", "NM1"), "already_banned")
        self.assertEqual(bp_command_error(session, "pick", "NM2"), "")


class RoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = make_session()

    def _kind(self, sender: str, text: str) -> str:
        return classify_ref_message(self.session, sender, text)

    def test_engine_commands(self) -> None:
        # NM2 is available; pick/ban of a valid map-state -> engine.
        for body in ["!ref pick NM2", "!ref ban NM2", "!ref pause", "!ref ready",
                     "!ref ff", "!ref abort", "!ref pick first", "!ref ban skip"]:
            self.assertEqual(self._kind("Alice", body), "engine", body)

    def test_bp_errors_route_to_ai(self) -> None:
        # NM1 is banned in make_session; NM9 doesn't exist.
        self.assertEqual(self._kind("Alice", "!ref pick NM1"), "bp_error")   # already banned
        self.assertEqual(self._kind("Alice", "!ref ban NM1"), "bp_error")    # already banned
        self.assertEqual(self._kind("Alice", "!ref pick NM9"), "bp_error")   # not in pool

    def test_queries(self) -> None:
        for body in ["!ref ?", "!ref status", "!ref ask 还能ban几张", "!ref"]:
            self.assertEqual(self._kind("Alice", body), "query", body)

    def test_freeform_goes_to_controller(self) -> None:
        self.assertEqual(self._kind("Alice", "!ref 对面一直不ready"), "freeform")
        self.assertEqual(self._kind("Bob", "!ref 我掉线了能重开吗"), "freeform")

    def test_non_command_and_non_player(self) -> None:
        self.assertEqual(self._kind("Alice", "随便聊天"), "none")        # no !ref prefix
        self.assertEqual(self._kind("BanchoBot", "!ref pick NM1"), "none")  # not a session player
        self.assertEqual(self._kind("Stranger", "!ref ?"), "none")          # not on a team


class ControllerTest(unittest.TestCase):
    def _agent(self, ai):
        irc = FakeIrc()
        agent = RefereeAgent(irc, api=None, agent_id="a", output_func=lambda *_: None,
                             assistant=RefereeAssistant(ai, rules_text="规则书原文"))
        session = make_session()
        agent.sessions[session.id] = session
        return agent, irc, session

    def _wait(self, irc, predicate, tries=80):
        import time
        for _ in range(tries):
            if predicate(irc):
                return
            time.sleep(0.01)

    def test_classify_command_whitelist(self) -> None:
        self.assertEqual(classify_mp_command("!mp timer 120"), "auto")
        self.assertEqual(classify_mp_command("!mp map 101 0"), "auto")
        self.assertEqual(classify_mp_command("!mp start 7"), "auto")
        self.assertEqual(classify_mp_command("!mp abort"), "hold")
        self.assertEqual(classify_mp_command("!mp close"), "hold")
        self.assertEqual(classify_mp_command("!mp set 2 3 3"), "hold")
        self.assertEqual(classify_mp_command("just chatting"), "reject")

    def test_decide_parses_json(self) -> None:
        ai = StubAI('{"reasoning":"超时","say":"延长30秒","command":"!mp timer 30","needs_human":false}')
        decision = RefereeAssistant(ai).decide({"situation": "对面还没ready"})
        self.assertEqual(decision["command"], "!mp timer 30")
        self.assertFalse(decision["needs_human"])

    def test_unknown_ref_triggers_controller_auto_command(self) -> None:
        ai = StubAI('{"say":"给对面再加60秒","command":"!mp timer 60","needs_human":false}')
        agent, irc, _ = self._agent(ai)
        agent._maybe_assist(agent.sessions["s"], "<Alice> !ref 对面一直不ready怎么办")
        self._wait(irc, lambda c: (CHANNEL, "!mp timer 60") in c.sent)
        self.assertIn((CHANNEL, "给对面再加60秒"), irc.sent)
        self.assertIn((CHANNEL, "!mp timer 60"), irc.sent)  # whitelisted -> auto-sent

    def test_destructive_command_is_held_for_human(self) -> None:
        ai = StubAI('{"say":"建议放弃本局","command":"!mp abort","needs_human":true}')
        agent, irc, _ = self._agent(ai)
        agent._maybe_assist(agent.sessions["s"], "<Bob> !ref 我刚刚掉线了能重开吗")
        self._wait(irc, lambda c: any("需人工确认" in t for _, t in c.sent))
        self.assertNotIn((CHANNEL, "!mp abort"), irc.sent)  # never auto-sent
        self.assertTrue(any("需人工确认" in t and "!mp abort" in t for _, t in irc.sent))

    def test_engine_verb_does_not_trigger_controller(self) -> None:
        ai = StubAI('{"command":"!mp start 7","needs_human":false}')
        agent, irc, _ = self._agent(ai)
        agent._maybe_assist(agent.sessions["s"], "<Alice> !ref ban NM2")  # valid -> engine's job
        import time
        time.sleep(0.05)
        self.assertEqual(irc.sent, [])

    def test_full_history_is_fed_to_controller_uncapped(self) -> None:
        ai = StubAI('{"say":"收到","command":"","needs_human":false}')
        agent, irc, session = self._agent(ai)
        for i in range(60):  # well over the old 40 cap
            agent._record_context(session, f"<Alice> msg{i}")
        agent._maybe_assist(session, "<Bob> !ref 帮我看下现在的情况")
        self._wait(irc, lambda c: c.sent)
        prompt = ai.calls[-1][1]
        self.assertIn("msg0", prompt)    # earliest kept
        self.assertIn("msg59", prompt)   # latest kept

    def test_bp_error_is_forwarded_to_ai(self) -> None:
        ai = StubAI('{"say":"NM1 已经被 ban 了，请换一张","command":"","needs_human":false}')
        agent, irc, _ = self._agent(ai)
        agent._maybe_assist(agent.sessions["s"], "<Alice> !ref pick NM1")  # NM1 banned -> bp_error
        self._wait(irc, lambda c: c.sent)
        self.assertTrue(any("NM1" in t for _, t in irc.sent))

    def test_valid_pick_not_forwarded_to_ai(self) -> None:
        ai = StubAI('{"say":"should not fire"}')
        agent, irc, _ = self._agent(ai)
        agent._maybe_assist(agent.sessions["s"], "<Bob> !ref pick NM2")  # valid map-state -> engine
        import time
        time.sleep(0.05)
        self.assertEqual(irc.sent, [])

    def test_controller_without_ai_shows_status(self) -> None:
        irc = FakeIrc()
        agent = RefereeAgent(irc, api=None, agent_id="a", output_func=lambda *_: None,
                             assistant=RefereeAssistant(None))
        session = make_session()
        agent.sessions[session.id] = session
        agent._maybe_assist(session, "<Alice> !ref 对面掉线了")
        self.assertEqual(len(irc.sent), 1)
        self.assertIn("阶段", irc.sent[0][1])


class ConsoleTest(unittest.TestCase):
    def _agent(self, outputs=None):
        irc = FakeIrc()
        agent = RefereeAgent(irc, api=None, output_func=(outputs.append if outputs is not None else (lambda *_: None)),
                             assistant=RefereeAssistant(None))
        agent.sessions["s"] = make_session()
        return agent, irc

    def test_human_pauses_auto_and_relays_input(self) -> None:
        agent, irc = self._agent()
        agent.handle_console("/human")
        self.assertFalse(agent.auto)
        agent.tick_sessions()                       # AI paused -> sends nothing
        self.assertEqual(irc.sent, [])
        agent.handle_console("!mp settings")        # operator relays to the room
        self.assertIn((CHANNEL, "!mp settings"), irc.sent)

    def test_ai_resumes_auto(self) -> None:
        agent, irc = self._agent()
        agent.handle_console("/human")
        agent.handle_console("/ai")
        self.assertTrue(agent.auto)
        agent.tick_sessions()                       # resumed -> emits the turn prompt
        self.assertTrue(irc.sent)

    def test_plain_text_ignored_while_ai_in_control(self) -> None:
        agent, irc = self._agent()
        agent.handle_console("随便说点什么")        # auto on -> not relayed
        self.assertEqual(irc.sent, [])

    def test_state_command_reports_status(self) -> None:
        outputs: list[str] = []
        agent, _ = self._agent(outputs)
        agent.handle_console("/state")
        self.assertTrue(any("阶段" in line for line in outputs))


if __name__ == "__main__":
    unittest.main()

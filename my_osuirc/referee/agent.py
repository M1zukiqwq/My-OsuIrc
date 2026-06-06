"""Bancho IRC agent that polls the referee server for scheduled matches."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict
from typing import Any

from my_osuirc.irc.client import IrcClient
from my_osuirc.referee.api import RefereeApiClient, RefereeApiError
from my_osuirc.referee.assistant import RefereeAssistant
from my_osuirc.referee.core import (
    MP_ROOM_RE,
    RefereeEngine,
    RefereeSession,
    classify_mp_command,
    classify_ref_message,
    format_status,
    parse_chat_sender,
    session_from_dict,
    strip_player_command_prefix,
)


class RefereeAgent:
    def __init__(
        self,
        client: IrcClient,
        api: RefereeApiClient,
        agent_id: str | None = None,
        output_func=print,
        assistant: RefereeAssistant | None = None,
    ) -> None:
        self.client = client
        self.api = api
        self.agent_id = agent_id or f"agent-{client.nick}-{uuid.uuid4().hex[:8]}"
        self.output = output_func
        self.engine = RefereeEngine()
        self.assistant = assistant
        self.sessions: dict[str, RefereeSession] = {}
        self.running = False
        self._next_heartbeat = 0.0
        self._next_claim = 0.0
        self._next_tasks = 0.0
        self._query_lock = threading.Lock()
        self._query_inflight: set[str] = set()
        self._summarized: set[str] = set()
        self._context: dict[str, list[str]] = {}  # full room history per session (chat volume is small)
        # When False, the agent stops sending automatic actions/answers: a human
        # operator has taken over the room (local /human; /ai resumes).
        self.auto = True

    def run(self) -> None:
        self.running = True
        self.output(f"Referee agent {self.agent_id} connected to server.")
        while self.running and self.client._running:
            self.step()
            time.sleep(0.2)
        self.safe_heartbeat("stopped")

    def step(self) -> None:
        now = time.time()
        self.drain_irc()
        if self.api:  # server-backed mode; standalone single-room agent has no API
            if now >= self._next_heartbeat:
                self.safe_heartbeat("running")
                self._next_heartbeat = now + 3
            if now >= self._next_claim:
                self.claim_due_sessions()
                self._next_claim = now + 5
            if now >= self._next_tasks:
                self.refresh_tasks()
                self._next_tasks = now + 2
        self.tick_sessions()

    def safe_heartbeat(self, status: str) -> None:
        if not self.api:
            return
        try:
            current = next(iter(self.sessions), "")
            self.api.heartbeat(self.agent_id, status=status, current_session_id=current)
        except Exception as exc:
            self.output(f"heartbeat failed: {exc}")

    def claim_due_sessions(self) -> None:
        try:
            records = self.api.claim(self.agent_id, limit=5)
        except RefereeApiError as exc:
            self.output(f"claim failed: {exc}")
            return
        for record in records:
            session = session_from_dict(record)
            self.sessions[session.id] = session
            self.output(f"claimed {session.id}: {session.config.name}")

    def refresh_tasks(self) -> None:
        try:
            records = self.api.tasks(self.agent_id)
        except RefereeApiError as exc:
            self.output(f"task poll failed: {exc}")
            return
        live_ids = set()
        for record in records:
            session = session_from_dict(record)
            live_ids.add(session.id)
            self.sessions[session.id] = session
            if session.state.channel:
                self.client.join(session.state.channel)
        for session_id in list(self.sessions):
            if session_id not in live_ids and self.sessions[session_id].state.stage == "finished":
                self.sessions.pop(session_id, None)
                self._context.pop(session_id, None)

    def drain_irc(self) -> None:
        for tag, text in self.client.drain_messages():
            self.route_message(tag, text)
            self.output(f"[{tag}] {text}")  # surface every room line so the operator can read along

    # -- local operator console (merged agent = one process per room) --

    def solo_session(self) -> RefereeSession | None:
        return next(iter(self.sessions.values()), None)

    def handle_console(self, line: str) -> None:
        """Local operator input. /human pauses the AI, /ai resumes it; in human
        mode any other text is sent into the room by the host (this account)."""
        text = line.strip()
        if not text:
            return
        low = text.lower()
        if low in {"/human", "/h"}:
            self.auto = False
            self.output("⏸ 人工接管：AI 已暂停转发。直接输入将作为房主发到房间；输入 /ai 交回。")
            return
        if low in {"/ai", "/resume"}:
            self.auto = True
            self.output("▶ AI 已重新接管。")
            return
        if low in {"/state", "/s"}:
            session = self.solo_session()
            self.output(format_status(self.engine.describe_state(session)) if session else "(还没有房间)")
            return
        if low in {"/quit", "/q"}:
            self.running = False
            return
        if text.startswith("/"):
            self.output("命令：/human 接管 | /ai 交回 | /state 状态 | /quit 退出；人工模式下其余输入直接发到房间。")
            return
        # Plain text from the operator.
        if self.auto:
            self.output("AI 接管中；先 /human 再发言。")
            return
        session = self.solo_session()
        if session and session.state.channel:
            self.client.send(session.state.channel, text)
            self.output(f"[{session.state.channel}] <{self.client.nick}> {text}")  # echo own send
        else:
            self.output("房间还没建好，暂时无法发送。")

    def route_message(self, tag: str, text: str) -> bool:
        routed = False
        if tag == "BanchoBot" and MP_ROOM_RE.search(text):
            for session in self.sessions.values():
                if session.state.stage == "creating_room" and not session.state.room_id:
                    self.engine.handle_message(session, tag, text)
                    if session.state.channel:
                        self.client.join(session.state.channel)
                    self.post_session_event(session, "irc", {"tag": tag, "text": text})
                    routed = True
                    break

        for session in self.sessions.values():
            if session.state.channel and tag.lower() == session.state.channel.lower():
                self.engine.handle_message(session, tag, text)
                self.post_session_event(session, "irc", {"tag": tag, "text": text})
                self._record_context(session, text)
                self._maybe_assist(session, text)
                routed = True
        return routed

    def _record_context(self, session: RefereeSession, text: str) -> None:
        self._context.setdefault(session.id, []).append(text)

    def tick_sessions(self) -> None:
        if not self.auto:
            return  # human has taken over; the engine must not send anything
        for session in list(self.sessions.values()):
            self.engine.tick_timeouts(session)
            for action in self.engine.next_actions(session):
                if action.risk != "low":
                    continue
                self.client.send(action.target, action.text)
                self.engine.mark_action_sent(session, action)
                self.output(f"AUTO {session.id}: {action.target} <- {action.text}")
                self.post_session_event(
                    session,
                    "auto_action",
                    {"action_id": action.id, "target": action.target, "text": action.text, "reason": action.reason},
                )
            if session.state.stage == "finished" and session.id not in self._summarized:
                self._summarized.add(session.id)
                self._dispatch_summary(session)

    # -- AI assistant (read-only language/judgment layer; runs off the hot loop) --

    def _maybe_assist(self, session: RefereeSession, text: str) -> None:
        if not self.auto:
            return  # human in control; the AI stays quiet
        parsed = parse_chat_sender(text)
        if not parsed:
            return
        sender, message = parsed
        kind = classify_ref_message(session, sender, message)
        # 'none' (plain chat / non-player) and 'engine' (pick/ban/…) need no AI.
        if kind in {"none", "engine"}:
            return
        body = (strip_player_command_prefix(message) or "").strip()
        if kind == "query":
            snapshot = self.engine.describe_state(session)
            question = body[3:].strip() if body.lower().startswith("ask") else body.lstrip("?").strip()
            if not question or not (self.assistant and self.assistant.enabled):
                self.client.send(session.state.channel, format_status(snapshot))
            else:
                self._dispatch_query(session, question, snapshot, list(self._context.get(session.id, [])))
            return
        # kind == 'freeform' (uncovered/ambiguous) or 'bp_error' (pick/ban failed the
        # mappool table) -> AI controller handles it as an engine-level problem.
        self._dispatch_decision(session, sender, body)

    def _dispatch_decision(self, session: RefereeSession, sender: str, situation: str) -> None:
        channel = session.state.channel
        snapshot = self.engine.describe_state(session)
        if not (self.assistant and self.assistant.enabled):
            self.client.send(channel, format_status(snapshot))  # no AI -> at least show state
            return
        session_id = session.id
        with self._query_lock:
            if session_id in self._query_inflight:
                return
            self._query_inflight.add(session_id)
        context = {
            "situation": situation,
            "raised_by": sender,
            "state": snapshot,
            "history": list(self._context.get(session_id, [])),  # full room history (chronological)
            "rulebook": self.assistant.rules_text[:4000],
        }

        def worker() -> None:
            try:
                decision = self.assistant.decide(context)
                say = str(decision.get("say") or "").strip()
                command = str(decision.get("command") or "").strip()
                needs_human = bool(decision.get("needs_human"))
                if say:
                    self.client.send(channel, say)
                if command:
                    kind = classify_mp_command(command)
                    if kind == "auto" and not needs_human:
                        self.client.send(channel, command)
                        self.output(f"AI-CTL {session_id}: {command}")
                        self._post_event_safe(session, "ai_action", {"command": command, "situation": situation})
                    elif kind != "reject":
                        self.client.send(channel, f"[需人工确认] 建议指令：{command}")
                        self.output(f"AI-HOLD {session_id}: {command} (kind={kind}, needs_human={needs_human})")
                        self._post_event_safe(
                            session, "ai_suggestion", {"command": command, "kind": kind, "situation": situation}
                        )
            except Exception as exc:  # never let the controller crash the agent
                self.output(f"AI controller failed: {exc}")
            finally:
                with self._query_lock:
                    self._query_inflight.discard(session_id)

        threading.Thread(target=worker, daemon=True).start()

    def _post_event_safe(self, session: RefereeSession, event_type: str, payload: dict[str, Any]) -> None:
        if not self.api:
            return
        try:
            self.post_session_event(session, event_type, payload)
        except Exception:
            pass

    def _dispatch_query(
        self, session: RefereeSession, question: str, snapshot: dict[str, Any], history: list[str]
    ) -> None:
        session_id, channel = session.id, session.state.channel
        with self._query_lock:
            if session_id in self._query_inflight:
                return  # one in-flight answer per room; avoid spam
            self._query_inflight.add(session_id)

        def worker() -> None:
            try:
                answer = self.assistant.answer(question, snapshot, history)
                self.client.send(channel, answer or format_status(snapshot))
            except Exception as exc:  # never let the assistant crash the agent
                self.output(f"assistant answer failed: {exc}")
            finally:
                with self._query_lock:
                    self._query_inflight.discard(session_id)

        threading.Thread(target=worker, daemon=True).start()

    def _dispatch_summary(self, session: RefereeSession) -> None:
        if not (self.assistant and self.assistant.enabled):
            return
        path = self.client.chat_log_path(session.state.channel) if hasattr(self.client, "chat_log_path") else None
        snapshot = self.engine.describe_state(session)

        def worker() -> None:
            try:
                log_text = path.read_text(encoding="utf-8", errors="replace") if path and path.exists() else ""
                summary = self.assistant.summarize(log_text, snapshot)
                if summary and path:
                    summary_path = path.with_suffix(".summary.md")
                    summary_path.write_text(summary, encoding="utf-8")
                    self.output(f"match summary written: {summary_path}")
            except Exception as exc:
                self.output(f"summary failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def post_session_event(self, session: RefereeSession, event_type: str, payload: dict[str, Any]) -> None:
        if not self.api:
            return
        try:
            self.api.post_event(session.id, event_type, payload, state=asdict(session.state))
        except RefereeApiError as exc:
            self.output(f"event post failed for {session.id}: {exc}")

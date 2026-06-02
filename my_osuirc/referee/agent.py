"""Bancho IRC agent that polls the referee server for scheduled matches."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from my_osuirc.irc.client import IrcClient
from my_osuirc.referee.api import RefereeApiClient, RefereeApiError
from my_osuirc.referee.core import MP_ROOM_RE, RefereeEngine, RefereeSession, session_from_dict


class RefereeAgent:
    def __init__(
        self,
        client: IrcClient,
        api: RefereeApiClient,
        agent_id: str | None = None,
        output_func=print,
    ) -> None:
        self.client = client
        self.api = api
        self.agent_id = agent_id or f"agent-{client.nick}-{uuid.uuid4().hex[:8]}"
        self.output = output_func
        self.engine = RefereeEngine()
        self.sessions: dict[str, RefereeSession] = {}
        self.running = False
        self._next_heartbeat = 0.0
        self._next_claim = 0.0
        self._next_tasks = 0.0

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

    def drain_irc(self) -> None:
        for tag, text in self.client.drain_messages():
            routed = self.route_message(tag, text)
            if not routed and tag != "SYSTEM":
                self.output(f"[{tag}] {text}")

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
                routed = True
        return routed

    def tick_sessions(self) -> None:
        for session in list(self.sessions.values()):
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

    def post_session_event(self, session: RefereeSession, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self.api.post_event(session.id, event_type, payload, state=asdict(session.state))
        except RefereeApiError as exc:
            self.output(f"event post failed for {session.id}: {exc}")

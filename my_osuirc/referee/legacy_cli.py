"""CLI supervisor for the AI osu! referee."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from my_osuirc.ai.client import OpenAICompatibleClient
from my_osuirc.irc.client import IrcClient
from my_osuirc.referee.core import (
    MP_ROOM_RE,
    RefereeEngine,
    RefereeSession,
    RefereeStore,
    RulePack,
    SessionConfig,
    SessionState,
    Team,
    default_rulepack,
    new_id,
    parse_menu_command,
    rulepack_from_draft,
)


InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


class RefereeSupervisor:
    """Single-process, multi-session CLI controller."""

    def __init__(
        self,
        client: IrcClient,
        store: RefereeStore | None = None,
        input_func: InputFunc = input,
        output_func: OutputFunc = print,
    ) -> None:
        self.client = client
        self.store = store or RefereeStore(Path("."))
        self.input_func = input_func
        self.output = output_func
        self.engine = RefereeEngine()
        self.sessions: dict[str, RefereeSession] = {}
        self.input_queue: queue.Queue[str | None] = queue.Queue()
        self.running = False
        self.manual_session_id = ""

    def load(self) -> None:
        self.store.ensure_default_rulepack()
        self.sessions = {session.id: session for session in self.store.list_sessions()}

    def run(self) -> None:
        self.load()
        self.client.on_message = lambda: None
        self.output("AI osu! referee CLI. Type 'help' for commands.")
        self.running = True
        reader = threading.Thread(target=self._read_input_loop, daemon=True)
        reader.start()

        while self.running and self.client._running:
            self.drain_irc()
            self.tick_sessions()
            self._drain_cli_input()
            time.sleep(0.1)

        self.pause_running_sessions("CLI exit")
        self.client.disconnect()

    def _read_input_loop(self) -> None:
        while self.running:
            try:
                prompt = f"{self.manual_session_id}> " if self.manual_session_id else "ref> "
                line = self.input_func(prompt)
            except (EOFError, KeyboardInterrupt):
                self.input_queue.put("quit")
                return
            self.input_queue.put(line)

    def _drain_cli_input(self) -> None:
        while True:
            try:
                line = self.input_queue.get_nowait()
            except queue.Empty:
                return
            if line is None:
                continue
            self.handle_input(line)

    def handle_input(self, line: str) -> None:
        if self.manual_session_id:
            self.handle_manual_input(line)
            return

        command = parse_menu_command(line)
        match command.name:
            case "empty":
                return
            case "help":
                self.print_help()
            case "list":
                self.print_session_list()
            case "#join":
                self.join_for_human(command.args[0] if command.args else "")
            case "resume":
                self.resume_session(command.args[0] if command.args else "")
            case "state":
                self.print_session_state(command.args[0] if command.args else "")
            case "new":
                self.create_new_session_interactive()
            case "add-config":
                self.add_config_interactive()
            case "quit":
                self.running = False
            case _:
                self.output("Unknown command. Type 'help' for commands.")

    def print_help(self) -> None:
        self.output(
            "Commands: list | #join <session_id|#mp_room> | new | add-config | "
            "resume <session_id> | state <session_id> | quit"
        )

    def print_session_list(self) -> None:
        if not self.sessions:
            self.output("No sessions yet. Use 'new' to create one.")
            return
        for session in self.sessions.values():
            state = session.state
            channel = state.channel or "(no room)"
            takeover = " human" if state.human_controlled else ""
            paused = f" paused:{state.paused_reason}" if state.paused else ""
            self.output(f"{session.id} | {session.config.name} | {state.stage}{takeover}{paused} | {channel}")

    def print_session_state(self, identifier: str) -> None:
        session = self.find_session(identifier)
        if not session:
            self.output("Session not found.")
            return
        state = session.state
        self.output(f"Session: {session.id} / {session.config.name}")
        self.output(f"Stage: {state.stage}; channel: {state.channel or '(no room)'}; score: {state.score}")
        self.output(f"Ready: {', '.join(state.ready_players) if state.ready_players else '(none)'}")
        self.output(f"Last event: {state.last_event or '(none)'}")
        next_actions = self.engine.next_actions(session)
        if next_actions:
            preview = "; ".join(f"{action.target}: {action.text}" for action in next_actions)
            self.output(f"Next low-risk actions: {preview}")
        else:
            self.output("Next low-risk actions: (none)")

    def join_for_human(self, identifier: str) -> None:
        if not identifier:
            self.output("Usage: #join <session_id|#mp_room>")
            return
        session = self.find_session(identifier)
        if not session and identifier.startswith("#mp_"):
            session = self.create_manual_session(identifier)
        if not session:
            self.output("Session not found.")
            return
        if session.state.channel:
            self.client.join(session.state.channel)
        self.engine.enter_human_control(session)
        self.store.save_session(session)
        self.store.append_log(session.id, "human_control_enter", {"channel": session.state.channel})
        self.manual_session_id = session.id
        self.output(f"Human takeover: {session.id}. Type /ai or /leave to return control to AI.")

    def handle_manual_input(self, line: str) -> None:
        session = self.find_session(self.manual_session_id)
        if not session:
            self.manual_session_id = ""
            self.output("Manual session disappeared; returning to main menu.")
            return
        text = line.strip()
        if text in {"/ai", "/leave"}:
            self.engine.leave_human_control(session)
            self.store.save_session(session)
            self.store.append_log(session.id, "human_control_leave", {})
            self.output(f"AI resumed: {session.id}")
            self.manual_session_id = ""
            return
        if not session.state.channel:
            self.output("This session has no mp room yet.")
            return
        self.client.send(session.state.channel, text)
        self.store.append_log(session.id, "human_send", {"target": session.state.channel, "text": text})

    def resume_session(self, identifier: str) -> None:
        session = self.find_session(identifier)
        if not session:
            self.output("Session not found.")
            return
        self.engine.resume(session)
        self.store.save_session(session)
        self.store.append_log(session.id, "resume", {})
        if self.manual_session_id == session.id:
            self.manual_session_id = ""
        self.output(f"Resumed {session.id}.")

    def create_new_session_interactive(self) -> RefereeSession | None:
        rulepacks = [pack for pack in self.store.list_rulepacks() if pack.confirmed]
        if not rulepacks:
            rulepacks = [self.store.ensure_default_rulepack()]
        self.output("Rulepacks:")
        for pack in rulepacks:
            defaulted = f" defaulted={','.join(pack.defaulted_fields)}" if pack.defaulted_fields else ""
            self.output(f"- {pack.id}: {pack.name}{defaulted}")
        rulepack_id = self.input_func("rulepack id [default]: ").strip() or "default"
        rulepack = self.store.load_rulepack(rulepack_id)
        if not rulepack or not rulepack.confirmed:
            self.output("Confirmed rulepack not found.")
            return None
        name = self.input_func("match name: ").strip()
        if not name:
            self.output("Match name is required.")
            return None
        match_time = self.input_func("match time: ").strip()
        best_of_raw = self.input_func("best of (e.g. 11, blank = not a bracket match): ").strip()
        best_of = int(best_of_raw) if best_of_raw.isdigit() else 0
        teams = self._prompt_teams(rulepack)
        override = self.input_func("mappool override link/note (optional): ").strip()
        session = self.create_session(
            name=name,
            rulepack=rulepack,
            teams=teams,
            match_time=match_time,
            best_of=best_of,
            mappool_override=[{"source": override}] if override else None,
        )
        self.output(f"Created session {session.id}. AI will create the mp room on next tick.")
        return session

    def add_config_interactive(self) -> RulePack | None:
        name = self.input_func("rulepack name: ").strip()
        if not name:
            self.output("Rulepack name is required.")
            return None
        rules_url = self.input_func("rules url (optional): ").strip()
        mappool_url = self.input_func("mappool url (optional): ").strip()
        draft: dict = {}
        ai_client = OpenAICompatibleClient.from_env()
        if ai_client:
            self.output("AI extracting rulepack draft...")
            try:
                draft = ai_client.extract_rulepack(name=name, rules_url=rules_url, mappool_url=mappool_url)
            except Exception as exc:  # keep the CLI usable when the AI provider fails
                self.output(f"AI extraction failed; using defaults: {exc}")
                draft = {}
        else:
            self.output("AI_API_KEY not set; using a default draft that still requires confirmation.")

        rulepack = rulepack_from_draft(name=name, rules_url=rules_url, mappool_url=mappool_url, draft=draft)
        self.print_rulepack_summary(rulepack)
        answer = self.input_func("confirm rulepack? [y/N]: ").strip().lower()
        if answer != "y":
            self.output("Rulepack discarded.")
            return None
        rulepack.confirmed = True
        self.store.save_rulepack(rulepack)
        self.output(f"Saved rulepack {rulepack.id}.")
        return rulepack

    def create_session(
        self,
        name: str,
        rulepack: RulePack,
        teams: list[Team],
        match_time: str = "",
        mappool_override: list[dict] | None = None,
        best_of: int = 0,
        first_to: int = 0,
    ) -> RefereeSession:
        config = SessionConfig(
            id=new_id("session", name),
            name=name,
            rulepack_id=rulepack.id,
            teams=teams,
            match_time=match_time,
            mappool_override=mappool_override or [],
            best_of=int(best_of or 0),
            first_to=int(first_to or 0),
        )
        state = SessionState(
            session_id=config.id,
            score={team.name: 0 for team in teams},
        )
        session = RefereeSession(config=config, rulepack=rulepack, state=state)
        self.sessions[session.id] = session
        self.store.save_session(session)
        self.store.append_log(session.id, "session_created", {"name": name, "rulepack_id": rulepack.id})
        return session

    def create_manual_session(self, channel: str) -> RefereeSession:
        room_id = channel.lower().removeprefix("#mp_")
        rulepack = self.store.load_rulepack("default") or default_rulepack()
        session = self.create_session(
            name=f"Manual {channel}",
            rulepack=rulepack,
            teams=[],
            match_time="",
        )
        self.engine.attach_room(session, room_id)
        self.store.save_session(session)
        return session

    def print_rulepack_summary(self, rulepack: RulePack) -> None:
        defaulted = ", ".join(rulepack.defaulted_fields) if rulepack.defaulted_fields else "(none)"
        self.output(f"RulePack draft: {rulepack.name}")
        self.output(f"- bp timer: {rulepack.bp_timer}s")
        self.output(f"- join timer: {rulepack.join_timer}s")
        self.output(f"- ready start: !mp start {rulepack.ready_start_countdown}")
        self.output(f"- !ref ready checks settings: {rulepack.start_on_ready_settings_check}")
        self.output(f"- system all-ready starts match: {rulepack.start_on_system_all_ready}")
        self.output(f"- join timer end starts match: {rulepack.start_on_join_timer_end}")
        self.output(f"- defaulted fields: {defaulted}")
        self.output(f"- team template: {', '.join(rulepack.team_template)}")
        self.output(f"- maps extracted: {len(rulepack.mappool)}")

    def _prompt_teams(self, rulepack: RulePack) -> list[Team]:
        teams: list[Team] = []
        labels = rulepack.team_template or ["red", "blue"]
        for label in labels:
            team_name = self.input_func(f"{label} team name: ").strip() or label
            players_text = self.input_func(f"{team_name} players (comma separated): ").strip()
            players = [player.strip() for player in players_text.split(",") if player.strip()]
            teams.append(Team(name=team_name, players=players))
        return teams

    def drain_irc(self) -> None:
        for tag, text in self.client.drain_messages():
            routed = self.route_message(tag, text)
            if self.manual_session_id:
                manual = self.find_session(self.manual_session_id)
                if manual and (tag == manual.state.channel or tag == "BanchoBot"):
                    self.output(f"[{tag}] {text}")
            elif not routed and tag != "SYSTEM":
                self.output(f"[{tag}] {text}")

    def route_message(self, tag: str, text: str) -> bool:
        routed = False
        if tag == "BanchoBot" and MP_ROOM_RE.search(text):
            for session in self.sessions.values():
                if session.state.stage == "creating_room" and not session.state.room_id:
                    self.engine.handle_message(session, tag, text)
                    self.client.join(session.state.channel)
                    self.store.save_session(session)
                    self.store.append_log(session.id, "irc", {"tag": tag, "text": text})
                    routed = True
                    break

        for session in self.sessions.values():
            if session.state.channel and tag.lower() == session.state.channel.lower():
                self.engine.handle_message(session, tag, text)
                self.store.save_session(session)
                self.store.append_log(session.id, "irc", {"tag": tag, "text": text})
                routed = True
        return routed

    def tick_sessions(self) -> None:
        for session in list(self.sessions.values()):
            self.engine.tick_timeouts(session)
            for action in self.engine.next_actions(session):
                if action.risk != "low":
                    continue
                self.send_action(session, action.target, action.text)
                self.engine.mark_action_sent(session, action)
                self.store.save_session(session)
                self.store.append_log(
                    session.id,
                    "auto_action",
                    {"action_id": action.id, "target": action.target, "text": action.text, "reason": action.reason},
                )

    def send_action(self, session: RefereeSession, target: str, text: str) -> None:
        self.client.send(target, text)
        self.output(f"AUTO {session.id}: {target} <- {text}")

    def find_session(self, identifier: str) -> RefereeSession | None:
        if not identifier:
            return None
        if identifier in self.sessions:
            return self.sessions[identifier]
        lowered = identifier.lower()
        for session in self.sessions.values():
            if session.state.channel.lower() == lowered:
                return session
        matches = [session for session in self.sessions.values() if session.id.startswith(identifier)]
        return matches[0] if len(matches) == 1 else None

    def pause_running_sessions(self, reason: str) -> None:
        for session in self.sessions.values():
            if session.state.stage not in {"finished", "paused"}:
                self.engine.pause(session, reason)
                self.store.save_session(session)
                self.store.append_log(session.id, "pause", {"reason": reason})

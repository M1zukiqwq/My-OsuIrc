"""Human referee CLI that talks to the local referee server."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from my_osuirc.referee.api import RefereeApiClient, RefereeApiError
from my_osuirc.referee.core import DEFAULT_ROOM_LEAD_TIME_SEC, parse_menu_command


InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


class ServerRefereeCli:
    def __init__(
        self,
        api: RefereeApiClient,
        input_func: InputFunc = input,
        output_func: OutputFunc = print,
    ) -> None:
        self.api = api
        self.input_func = input_func
        self.output = output_func
        self.manual_session_id = ""
        self.running = False

    def run(self) -> None:
        try:
            self.api.health()
        except Exception as exc:
            self.output(f"Cannot reach referee server: {exc}")
            return
        self.running = True
        self.output("AI osu! referee server CLI. Type 'help' for commands.")
        while self.running:
            prompt = f"{self.manual_session_id}> " if self.manual_session_id else "ref> "
            try:
                line = self.input_func(prompt)
            except (EOFError, KeyboardInterrupt):
                line = "quit"
            self.handle_input(line)

    def handle_input(self, line: str) -> None:
        if self.manual_session_id:
            self.handle_manual_input(line)
            return

        command = parse_menu_command(line)
        try:
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
        except RefereeApiError as exc:
            self.output(f"Server error ({exc.status}): {exc}")

    def print_help(self) -> None:
        self.output(
            "Commands: list | #join <session_id|#mp_room> | new | add-config | "
            "resume <session_id> | state <session_id> | quit"
        )

    def print_session_list(self) -> None:
        sessions = self.api.list_sessions()
        if not sessions:
            self.output("No sessions yet. Use 'new' to create one.")
            return
        for record in sessions:
            config = record["config"]
            state = record["state"]
            channel = state.get("channel") or "(no room)"
            assigned = record.get("assigned_agent_id") or "(unassigned)"
            self.output(
                f"{config['id']} | {config['name']} | {state.get('stage')} | {channel} | agent={assigned}"
            )

    def print_session_state(self, identifier: str) -> None:
        record = self.find_session_record(identifier)
        if not record:
            self.output("Session not found.")
            return
        config = record["config"]
        state = record["state"]
        self.output(f"Session: {config['id']} / {config['name']}")
        self.output(f"Stage: {state.get('stage')}; channel: {state.get('channel') or '(no room)'}")
        self.output(f"Score: {state.get('score')}")
        self.output(f"Ready: {', '.join(state.get('ready_players') or []) or '(none)'}")
        self.output(f"Assigned agent: {record.get('assigned_agent_id') or '(unassigned)'}")
        self.output(f"Last event: {state.get('last_event') or '(none)'}")

    def join_for_human(self, identifier: str) -> None:
        record = self.find_session_record(identifier)
        if not record:
            self.output("Session not found.")
            return
        session_id = record["config"]["id"]
        self.api.control_session(session_id, "human_control", "human referee joined")
        self.manual_session_id = session_id
        self.output(f"Human control: {session_id}. Type /ai or /leave to return control to AI.")

    def handle_manual_input(self, line: str) -> None:
        text = line.strip()
        if text in {"/ai", "/leave"}:
            self.api.control_session(self.manual_session_id, "ai_control", "human referee left")
            self.output(f"AI resumed: {self.manual_session_id}")
            self.manual_session_id = ""
            return
        if text == "/state":
            self.print_session_state(self.manual_session_id)
            return
        if not text:
            return
        event_type = "human_command" if text.startswith("!") else "human_note"
        self.api.post_event(self.manual_session_id, event_type, {"text": text})
        self.output("Recorded. Agent stays paused while this session is under human control.")

    def resume_session(self, identifier: str) -> None:
        record = self.find_session_record(identifier)
        if not record:
            self.output("Session not found.")
            return
        session_id = record["config"]["id"]
        self.api.control_session(session_id, "ai_control", "resume command")
        if self.manual_session_id == session_id:
            self.manual_session_id = ""
        self.output(f"Resumed {session_id}.")

    def create_new_session_interactive(self) -> dict[str, Any] | None:
        rulepacks = [pack for pack in self.api.list_rulepacks() if pack.get("confirmed")]
        if not rulepacks:
            self.output("No confirmed rulepacks. Run add-config first, or use the default rulepack.")
            return None
        self.output("Rulepacks:")
        for pack in rulepacks:
            defaulted = ",".join(pack.get("defaulted_fields") or [])
            suffix = f" defaulted={defaulted}" if defaulted else ""
            self.output(f"- {pack['id']}: {pack['name']}{suffix}")
        rulepack_id = self.input_func("rulepack id [default]: ").strip() or "default"
        selected = next((pack for pack in rulepacks if pack["id"] == rulepack_id), None)
        if not selected:
            self.output("Confirmed rulepack not found.")
            return None
        name = self.input_func("match name: ").strip()
        if not name:
            self.output("Match name is required.")
            return None
        match_time = self.input_func("match time (ISO/local, optional): ").strip()
        teams = self._prompt_teams(selected)
        override = self.input_func("mappool override link/note (optional): ").strip()
        payload = {
            "name": name,
            "rulepack_id": rulepack_id,
            "teams": teams,
            "match_time": match_time,
            "room_lead_time_sec": DEFAULT_ROOM_LEAD_TIME_SEC,
            "mappool_override": [{"source": override}] if override else [],
        }
        created = self.api.create_session(payload)
        session_id = created["session"]["config"]["id"]
        self.output(f"Created scheduled session {session_id}.")
        return created

    def add_config_interactive(self) -> dict[str, Any] | None:
        name = self.input_func("rulepack name: ").strip()
        if not name:
            self.output("Rulepack name is required.")
            return None
        rules_url = self.input_func("rules url (optional): ").strip()
        mappool_url = self.input_func("mappool url (optional): ").strip()
        result = self.api.draft_rulepack({"name": name, "rules_url": rules_url, "mappool_url": mappool_url})
        rulepack = result["rulepack"]
        self.print_rulepack_summary(rulepack)
        if result.get("ai_error"):
            self.output(f"AI extraction failed; defaults were used: {result['ai_error']}")
        answer = self.input_func("confirm rulepack? [y/N]: ").strip().lower()
        if answer != "y":
            self.output("Rulepack draft saved as unconfirmed.")
            return result
        confirmed = self.api.confirm_rulepack(rulepack["id"])
        self.output(f"Confirmed rulepack {confirmed['rulepack']['id']}.")
        return confirmed

    def print_rulepack_summary(self, rulepack: dict[str, Any]) -> None:
        defaulted = ", ".join(rulepack.get("defaulted_fields") or []) or "(none)"
        self.output(f"RulePack draft: {rulepack.get('name')}")
        self.output(f"- bp timer: {rulepack.get('bp_timer')}s")
        self.output(f"- join timer: {rulepack.get('join_timer')}s")
        self.output(f"- ready start: !mp start {rulepack.get('ready_start_countdown')}")
        self.output(f"- !ref ready checks settings: {rulepack.get('start_on_ready_settings_check')}")
        self.output(f"- system all-ready starts match: {rulepack.get('start_on_system_all_ready')}")
        self.output(f"- join timer end starts match: {rulepack.get('start_on_join_timer_end')}")
        self.output(f"- defaulted fields: {defaulted}")

    def find_session_record(self, identifier: str) -> dict[str, Any] | None:
        if not identifier:
            return None
        lowered = identifier.lower()
        sessions = self.api.list_sessions()
        for record in sessions:
            config = record["config"]
            state = record["state"]
            if config["id"] == identifier or state.get("channel", "").lower() == lowered:
                return record
        matches = [record for record in sessions if record["config"]["id"].startswith(identifier)]
        return matches[0] if len(matches) == 1 else None

    def _prompt_teams(self, rulepack: dict[str, Any]) -> list[dict[str, Any]]:
        teams: list[dict[str, Any]] = []
        labels = rulepack.get("team_template") or ["red", "blue"]
        for label in labels:
            team_name = self.input_func(f"{label} team name: ").strip() or str(label)
            players_text = self.input_func(f"{team_name} players (comma separated): ").strip()
            players = [player.strip() for player in players_text.split(",") if player.strip()]
            teams.append({"name": team_name, "players": players})
        return teams

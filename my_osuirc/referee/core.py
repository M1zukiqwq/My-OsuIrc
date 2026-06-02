"""Core state and deterministic logic for the osu! referee agent."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_BP_TIMER = 90
DEFAULT_JOIN_TIMER = 120
DEFAULT_READY_START_COUNTDOWN = 7
DEFAULT_ROOM_LEAD_TIME_SEC = 600
PLAYER_COMMAND_PREFIX = "!ref"

SESSION_STAGES = {
    "configured",
    "scheduled",
    "creating_room",
    "inviting",
    "waiting_players",
    "bp",
    "playing",
    "finished",
    "human_controlled",
    "paused",
}

MP_ROOM_RE = re.compile(r"https://osu\.ppy\.sh/mp/(\d+)")
CHAT_LINE_RE = re.compile(r"^-?<([^>]+)>-?\s+(.*)$")


@dataclass
class Team:
    name: str
    players: list[str] = field(default_factory=list)


@dataclass
class RulePack:
    id: str
    name: str
    rules_url: str = ""
    mappool_url: str = ""
    bp_timer: int = DEFAULT_BP_TIMER
    join_timer: int = DEFAULT_JOIN_TIMER
    ready_start_countdown: int = DEFAULT_READY_START_COUNTDOWN
    start_on_ready_settings_check: bool = True
    start_on_system_all_ready: bool = True
    start_on_join_timer_end: bool = True
    defaulted_fields: list[str] = field(default_factory=list)
    confirmed: bool = False
    mappool: list[dict[str, Any]] = field(default_factory=list)
    format: dict[str, Any] = field(default_factory=dict)
    team_template: list[str] = field(default_factory=lambda: ["red", "blue"])
    raw_rules: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class SessionConfig:
    id: str
    name: str
    rulepack_id: str
    teams: list[Team] = field(default_factory=list)
    match_time: str = ""
    room_lead_time_sec: int = DEFAULT_ROOM_LEAD_TIME_SEC
    mappool_override: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionState:
    session_id: str
    stage: str = "configured"
    room_id: str = ""
    channel: str = ""
    score: dict[str, int] = field(default_factory=dict)
    ready_players: list[str] = field(default_factory=list)
    ready_settings_requested: bool = False
    all_ready_confirmed: bool = False
    player_pause_requested: bool = False
    join_timer_deadline: float = 0.0
    sent_actions: list[str] = field(default_factory=list)
    human_controlled: bool = False
    paused: bool = False
    paused_reason: str = ""
    previous_stage: str = ""
    last_event: str = ""
    updated_at: float = field(default_factory=time.time)


@dataclass
class RefereeSession:
    config: SessionConfig
    rulepack: RulePack
    state: SessionState

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def room_name(self) -> str:
        return self.config.name or f"osu referee {self.id}"

    @property
    def all_players(self) -> list[str]:
        players: list[str] = []
        for team in self.config.teams:
            players.extend(team.players)
        return players


@dataclass(frozen=True)
class RefereeAction:
    id: str
    target: str
    text: str
    risk: str = "low"
    reason: str = ""


@dataclass(frozen=True)
class MenuCommand:
    name: str
    args: list[str] = field(default_factory=list)


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug or fallback


def new_id(prefix: str, name: str = "") -> str:
    return f"{prefix}-{slugify(name, prefix)}-{uuid.uuid4().hex[:8]}"


def parse_menu_command(line: str) -> MenuCommand:
    parts = line.strip().split()
    if not parts:
        return MenuCommand("empty", [])
    command = parts[0].lower()
    known = {"list", "#join", "resume", "state", "new", "add-config", "quit", "help"}
    if command not in known:
        return MenuCommand("unknown", parts)
    return MenuCommand(command, parts[1:])


def parse_chat_sender(text: str) -> tuple[str, str] | None:
    match = CHAT_LINE_RE.match(text)
    if not match:
        return None
    return match.group(1), match.group(2)


def strip_player_command_prefix(message: str) -> str | None:
    stripped = message.strip()
    prefix = PLAYER_COMMAND_PREFIX.lower()
    lowered = stripped.lower()
    if lowered == prefix:
        return ""
    if lowered.startswith(prefix + " "):
        return stripped[len(PLAYER_COMMAND_PREFIX) :].strip()
    return None


def is_ready_message(message: str) -> bool:
    normalized = message.strip().lower()
    return normalized in {"ready", "r", "!ready", "!mp ready"}


def is_pause_message(message: str) -> bool:
    normalized = message.strip().lower()
    return normalized in {"pause", "p", "!pause", "!mp pause"}


def is_all_ready_system_message(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if "all players" in normalized and "ready" in normalized:
        return True
    if "everyone" in normalized and "ready" in normalized:
        return True
    if "所有" in normalized and "准备" in normalized:
        return True
    return False


def rulepack_from_draft(
    name: str,
    rules_url: str = "",
    mappool_url: str = "",
    draft: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> RulePack:
    draft = draft or {}
    defaulted: list[str] = []

    def int_field(field_name: str, default: int) -> int:
        value = draft.get(field_name)
        if value in (None, ""):
            defaulted.append(field_name)
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            defaulted.append(field_name)
            return default

    def bool_field(field_name: str, default: bool) -> bool:
        value = draft.get(field_name)
        if value in (None, ""):
            defaulted.append(field_name)
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
        return bool(value)

    return RulePack(
        id=str(draft.get("id") or new_id("rulepack", name)),
        name=str(draft.get("name") or name or "Untitled RulePack"),
        rules_url=str(draft.get("rules_url") or rules_url or ""),
        mappool_url=str(draft.get("mappool_url") or mappool_url or ""),
        bp_timer=int_field("bp_timer", DEFAULT_BP_TIMER),
        join_timer=int_field("join_timer", DEFAULT_JOIN_TIMER),
        ready_start_countdown=int_field("ready_start_countdown", DEFAULT_READY_START_COUNTDOWN),
        start_on_ready_settings_check=bool_field("start_on_ready_settings_check", True),
        start_on_system_all_ready=bool_field("start_on_system_all_ready", True),
        start_on_join_timer_end=bool_field("start_on_join_timer_end", True),
        defaulted_fields=defaulted,
        confirmed=confirmed,
        mappool=list(draft.get("mappool") or []),
        format=dict(draft.get("format") or {}),
        team_template=list(draft.get("team_template") or ["red", "blue"]),
        raw_rules=dict(draft.get("raw_rules") or draft),
    )


def default_rulepack() -> RulePack:
    return RulePack(
        id="default",
        name="Default osu! Referee Rules",
        confirmed=True,
        start_on_ready_settings_check=True,
        start_on_system_all_ready=True,
        start_on_join_timer_end=True,
        defaulted_fields=[
            "bp_timer",
            "join_timer",
            "ready_start_countdown",
            "start_on_ready_settings_check",
            "start_on_system_all_ready",
            "start_on_join_timer_end",
        ],
        team_template=["red", "blue"],
    )


def session_to_dict(session: RefereeSession) -> dict[str, Any]:
    return {
        "config": asdict(session.config),
        "rulepack": asdict(session.rulepack),
        "state": asdict(session.state),
    }


def session_from_dict(data: dict[str, Any]) -> RefereeSession:
    config_data = dict(data["config"])
    config_data["teams"] = [Team(**team) for team in config_data.get("teams", [])]
    config = SessionConfig(**config_data)
    rulepack = RulePack(**data["rulepack"])
    state = SessionState(**data["state"])
    return RefereeSession(config=config, rulepack=rulepack, state=state)


class RefereeStore:
    """JSON-backed local persistence for rulepacks, sessions, and logs."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root)
        self.rulepacks_dir = self.root / "rulepacks"
        self.sessions_dir = self.root / "sessions"
        self.logs_dir = self.root / "logs"
        for path in (self.rulepacks_dir, self.sessions_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def ensure_default_rulepack(self) -> RulePack:
        existing = self.load_rulepack("default")
        if existing:
            return existing
        rulepack = default_rulepack()
        self.save_rulepack(rulepack)
        return rulepack

    def save_rulepack(self, rulepack: RulePack) -> None:
        self._write_json(self.rulepacks_dir / f"{rulepack.id}.json", asdict(rulepack))

    def load_rulepack(self, rulepack_id: str) -> RulePack | None:
        path = self.rulepacks_dir / f"{rulepack_id}.json"
        if not path.exists():
            return None
        return RulePack(**self._read_json(path))

    def list_rulepacks(self) -> list[RulePack]:
        packs = [RulePack(**self._read_json(path)) for path in sorted(self.rulepacks_dir.glob("*.json"))]
        return sorted(packs, key=lambda pack: (pack.id != "default", pack.name.lower()))

    def save_session(self, session: RefereeSession) -> None:
        session.state.updated_at = time.time()
        self._write_json(self.sessions_dir / f"{session.id}.json", session_to_dict(session))

    def load_session(self, session_id: str) -> RefereeSession | None:
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return session_from_dict(self._read_json(path))

    def list_sessions(self) -> list[RefereeSession]:
        sessions = [session_from_dict(self._read_json(path)) for path in sorted(self.sessions_dir.glob("*.json"))]
        return sorted(sessions, key=lambda session: session.state.updated_at, reverse=True)

    def append_log(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        entry = {"time": time.time(), "type": event_type, "payload": payload}
        path = self.logs_dir / f"{session_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)


class RefereeEngine:
    """Deterministic referee state machine and low-risk command generator."""

    def handle_message(self, session: RefereeSession, tag: str, text: str) -> None:
        state = session.state
        state.last_event = f"[{tag}] {text}"

        if tag in {"BanchoBot", "SYSTEM"}:
            match = MP_ROOM_RE.search(text)
            if tag == "BanchoBot" and match and state.stage == "creating_room":
                state.room_id = match.group(1)
                state.channel = f"#mp_{state.room_id}"
                state.stage = "inviting"
            if session.rulepack.start_on_system_all_ready and is_all_ready_system_message(text):
                state.all_ready_confirmed = True
            return

        if state.channel and tag.lower() == state.channel.lower():
            parsed = parse_chat_sender(text)
            if not parsed:
                if session.rulepack.start_on_system_all_ready and is_all_ready_system_message(text):
                    state.all_ready_confirmed = True
                return
            sender, message = parsed
            if sender.lower() in {"banchobot", "system"}:
                if session.rulepack.start_on_system_all_ready and is_all_ready_system_message(message):
                    state.all_ready_confirmed = True
                return
            command = strip_player_command_prefix(message)
            if command is None:
                return
            if self._is_session_player(session, sender) and is_pause_message(command):
                state.player_pause_requested = True
                self.pause(session, f"player requested pause: {sender}")
                return
            if self._is_session_player(session, sender) and is_ready_message(command):
                state.ready_settings_requested = True
                if sender not in state.ready_players:
                    state.ready_players.append(sender)

    def attach_room(self, session: RefereeSession, room_id: str) -> None:
        session.state.room_id = room_id
        session.state.channel = f"#mp_{room_id}"
        if session.state.stage in {"configured", "scheduled", "creating_room"}:
            session.state.stage = "inviting"

    def enter_human_control(self, session: RefereeSession) -> None:
        state = session.state
        if state.human_controlled:
            return
        state.previous_stage = state.stage if state.stage != "human_controlled" else state.previous_stage
        state.stage = "human_controlled"
        state.human_controlled = True

    def leave_human_control(self, session: RefereeSession) -> None:
        state = session.state
        state.human_controlled = False
        state.stage = state.previous_stage or "waiting_players"
        state.previous_stage = ""

    def pause(self, session: RefereeSession, reason: str = "") -> None:
        state = session.state
        if state.human_controlled:
            state.human_controlled = False
        elif state.stage != "paused":
            state.previous_stage = state.stage
        state.stage = "paused"
        state.paused = True
        state.paused_reason = reason

    def resume(self, session: RefereeSession) -> None:
        state = session.state
        state.paused = False
        state.paused_reason = ""
        state.player_pause_requested = False
        if state.human_controlled:
            self.leave_human_control(session)
        elif state.stage == "paused":
            state.stage = state.previous_stage or "waiting_players"
            state.previous_stage = ""

    def next_actions(self, session: RefereeSession) -> list[RefereeAction]:
        state = session.state
        if state.human_controlled or state.paused or state.stage in {"finished", "human_controlled", "paused"}:
            return []
        if state.stage in {"configured", "scheduled"}:
            return self._missing_actions(
                session,
                [RefereeAction("make_room", "BanchoBot", f"!mp make {session.room_name}", reason="create mp room")],
            )
        if state.stage == "inviting" and state.channel:
            actions = [
                RefereeAction(
                    f"invite:{player}",
                    state.channel,
                    f"!mp invite {player}",
                    reason="invite player",
                )
                for player in session.all_players
            ]
            actions.extend(
                [
                    RefereeAction(
                        "join_timer",
                        state.channel,
                        f"!mp timer {session.rulepack.join_timer}",
                        reason="player join timer",
                    ),
                    RefereeAction(
                        "bp_timer",
                        state.channel,
                        f"!mp timer {session.rulepack.bp_timer}",
                        reason="default bp timer",
                    ),
                ]
            )
            return self._missing_actions(session, actions)
        if state.stage == "waiting_players":
            actions: list[RefereeAction] = []
            if (
                session.rulepack.start_on_ready_settings_check
                and state.ready_settings_requested
                and "settings_check" not in state.sent_actions
            ):
                actions.append(
                    RefereeAction(
                        "settings_check",
                        state.channel,
                        "!mp settings",
                        reason="player requested ready check",
                    )
                )
            if self.should_start_match(session):
                actions.append(
                    RefereeAction(
                        "ready_start",
                        state.channel,
                        f"!mp start {session.rulepack.ready_start_countdown}",
                        reason="default start condition met",
                    )
                )
            return self._missing_actions(session, actions)
        return []

    def should_start_match(self, session: RefereeSession) -> bool:
        state = session.state
        if state.player_pause_requested:
            return False
        if session.rulepack.start_on_system_all_ready and state.all_ready_confirmed:
            return True
        if session.rulepack.start_on_join_timer_end and state.join_timer_deadline:
            return time.time() >= state.join_timer_deadline
        return False

    def mark_action_sent(self, session: RefereeSession, action: RefereeAction) -> None:
        state = session.state
        if action.id == "settings_check":
            state.ready_settings_requested = False
            return
        if action.id not in state.sent_actions:
            state.sent_actions.append(action.id)
        if action.id == "make_room" and state.stage in {"configured", "scheduled"}:
            state.stage = "creating_room"
        elif action.id == "join_timer":
            state.join_timer_deadline = time.time() + session.rulepack.join_timer
            if state.stage == "inviting" and self._inviting_complete(session):
                state.stage = "waiting_players"
        elif state.stage == "inviting" and self._inviting_complete(session):
            state.stage = "waiting_players"
        elif action.id == "ready_start":
            state.stage = "playing"

    def all_players_ready(self, session: RefereeSession) -> bool:
        players = {player.lower() for player in session.all_players}
        ready = {player.lower() for player in session.state.ready_players}
        return bool(players) and players.issubset(ready)

    def _missing_actions(self, session: RefereeSession, actions: list[RefereeAction]) -> list[RefereeAction]:
        sent = set(session.state.sent_actions)
        return [action for action in actions if action.id not in sent]

    def _inviting_complete(self, session: RefereeSession) -> bool:
        required = {f"invite:{player}" for player in session.all_players}
        required.update({"join_timer", "bp_timer"})
        return required.issubset(set(session.state.sent_actions))

    def _is_session_player(self, session: RefereeSession, nick: str) -> bool:
        return nick.lower() in {player.lower() for player in session.all_players}

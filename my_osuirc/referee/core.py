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
DEFAULT_PICK_BAN_TIMER = 60
DEFAULT_PREP_TIMER = 120
DEFAULT_TB_PREP_TIMER = 180
DEFAULT_PAUSE_TIMER = 120
DEFAULT_PAUSE_PER_PLAYER = 2
DEFAULT_ABORT_WINDOW = 30
PLAYER_COMMAND_PREFIX = "!ref"

SESSION_STAGES = {
    "configured",
    "scheduled",
    "creating_room",
    "inviting",
    "waiting_players",
    "bp",
    "playing",
    "bo_roll",
    "bo_pick",
    "bo_ready",
    "bo_playing",
    "finished",
    "human_controlled",
    "paused",
}

MP_ROOM_RE = re.compile(r"https://osu\.ppy\.sh/mp/(\d+)")
CHAT_LINE_RE = re.compile(r"^-?<([^>]+)>-?\s+(.*)$")

# BanchoBot match-result lines (best-of bracket support).
MP_RESULT_RE = re.compile(r"^(.+?) finished playing \(Score:\s*([\d,]+),\s*(PASSED|FAILED)\)\.?$")
MATCH_FINISHED_RE = re.compile(r"\bmatch has finished\b", re.IGNORECASE)
MATCH_STARTED_RE = re.compile(r"\bmatch has started\b", re.IGNORECASE)
MP_ROLL_RE = re.compile(r"^(.+?) rolls (\d+) point", re.IGNORECASE)


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
    pick_ban_timer: int = DEFAULT_PICK_BAN_TIMER
    prep_timer: int = DEFAULT_PREP_TIMER
    tb_prep_timer: int = DEFAULT_TB_PREP_TIMER
    pause_timer: int = DEFAULT_PAUSE_TIMER
    pause_per_player: int = DEFAULT_PAUSE_PER_PLAYER
    abort_window: int = DEFAULT_ABORT_WINDOW
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
    # Best-of is a property of *this match*, chosen when the room is opened —
    # the same rulepack/mappool can be used for BO9 / BO11 / BO13 stages.
    best_of: int = 0
    first_to: int = 0


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
    # Best-of bracket state (only used when the session declares a best_of).
    current_pick_code: str = ""
    current_map_scores: dict[str, int] = field(default_factory=dict)
    banned_codes: list[str] = field(default_factory=list)
    played_codes: list[str] = field(default_factory=list)
    pending_announce: str = ""
    match_winner: str = ""
    # Managed ban/pick state (only used when the rulebook declares a bp_order).
    rolls: dict[str, int] = field(default_factory=dict)
    pick_order: list[str] = field(default_factory=list)  # [first team, second team]
    ban_order: list[str] = field(default_factory=list)
    bp_step: int = 0
    pick_count: int = 0
    ban_count: int = 0
    roll_phase: bool = False  # only while True does the engine recognise roll messages
    turn_deadline: float = 0.0
    pause_counts: dict[str, int] = field(default_factory=dict)
    paused_this_map: list[str] = field(default_factory=list)
    pause_deadline: float = 0.0
    play_started_at: float = 0.0
    pending_abort: bool = False
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

    @property
    def is_bo_mode(self) -> bool:
        return self.best_of > 0

    @property
    def best_of(self) -> int:
        try:
            return int(self.config.best_of or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def first_to(self) -> int:
        try:
            declared = int(self.config.first_to or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > 0:
            return declared
        return (self.best_of // 2) + 1 if self.best_of else 0

    def team_for_player(self, nick: str) -> Team | None:
        lowered = nick.lower()
        for team in self.config.teams:
            if lowered in {player.lower() for player in team.players}:
                return team
        return None

    @property
    def bp_template(self) -> list[str]:
        raw = self.rulepack.format.get("bp_order") or []
        return [str(item).lower() for item in raw if str(item).lower() in {"pick", "ban"}]

    @property
    def is_bp_managed(self) -> bool:
        return self.is_bo_mode and bool(self.bp_template) and len(self.config.teams) >= 2


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


def banchobot_relevant(text: str) -> bool:
    """The single allowlist of BanchoBot/SYSTEM lines the engine recognises.

    Only these are processed by handle_message; every other BanchoBot line
    (joins, slot moves, countdown ticks, the !mp settings dump, glhf, ...) is
    dropped at the entrance. Edit this one place to change what the engine sees.
    """
    stripped = text.strip()
    if not stripped:
        return False
    return bool(
        MP_ROOM_RE.search(stripped)  # 1) "Created the tournament match ..."
        or MP_ROLL_RE.match(stripped)  # 2) "<player> rolls N point(s)"
        or MP_RESULT_RE.match(stripped)  # 3) "<player> finished playing (Score: N, ...)"
        or MATCH_FINISHED_RE.search(stripped)  # 4) "The match has finished"
        or is_all_ready_system_message(stripped)  # 5) "All players are ready"
    )


def parse_play_result(text: str) -> tuple[str, int] | None:
    """Parse a BanchoBot "<player> finished playing (Score: N, PASSED)" line."""
    match = MP_RESULT_RE.match(text.strip())
    if not match:
        return None
    player = match.group(1).strip()
    try:
        score = int(match.group(2).replace(",", ""))
    except ValueError:
        return None
    return player, score


def parse_pick_ban(command: str) -> tuple[str, str] | None:
    """Parse a stripped !ref body like "pick NM3" / "ban DT2" into (verb, code)."""
    parts = command.split()
    if len(parts) < 2:
        return None
    verb = parts[0].lower()
    if verb not in {"pick", "ban"}:
        return None
    return verb, parts[1].upper()


def mappool_lookup(rulepack: RulePack, code: str) -> dict[str, Any] | None:
    """Case-insensitive lookup of a mappool entry by its pick code."""
    wanted = str(code).strip().lower()
    if not wanted:
        return None
    for entry in rulepack.mappool:
        if str(entry.get("code", "")).strip().lower() == wanted:
            return entry
    return None


def is_tb_code(code: str) -> bool:
    return str(code).strip().upper().startswith("TB")


def mappool_status(session: "RefereeSession") -> list[dict[str, Any]]:
    """Authoritative mappool table shared by the engine, the router, and the AI.

    Each entry: {code, beatmap_id, mods, status} where status is one of
    'banned' / 'played' / 'current' / 'available'. Derived from the single
    canonical state (rulepack.mappool + banned_codes/played_codes/current_pick).
    """
    state = session.state
    banned = {code.lower() for code in state.banned_codes}
    played = {code.lower() for code in state.played_codes}
    current = (state.current_pick_code or "").lower()
    table: list[dict[str, Any]] = []
    for entry in session.rulepack.mappool:
        code = str(entry.get("code", ""))
        lowered = code.lower()
        if lowered in banned:
            status = "banned"
        elif lowered in played:
            status = "played"
        elif lowered and lowered == current:
            status = "current"
        else:
            status = "available"
        table.append(
            {"code": code, "beatmap_id": entry.get("beatmap_id"), "mods": entry.get("mods", ""), "status": status}
        )
    return table


def bp_command_error(session: "RefereeSession", verb: str, code: str) -> str:
    """Mappool-level error for a pick/ban, or '' if the map state is fine.

    Only checks the *mappool table* (nonexistent / already banned / already
    played). Turn order and TB gating are NOT checked here — the engine owns those.
    """
    if not mappool_lookup(session.rulepack, code):
        return "not_in_pool"
    lowered = code.lower()
    if lowered in {banned.lower() for banned in session.state.banned_codes}:
        return "already_banned"
    if lowered in {played.lower() for played in session.state.played_codes}:
        return "already_played"
    return ""


TEAM_MODE_CODES = {"headtohead": 0, "tagcoop": 1, "teamvs": 2, "tagteamvs": 3}
WIN_CONDITION_CODES = {"score": 0, "accuracy": 1, "combo": 2, "scorev2": 3, "v2": 3, "scorev1": 0, "v1": 0}


def mp_set_command(rulepack: RulePack, player_count: int) -> str:
    """Build "!mp set <team_mode> <win_condition> <size>" from the rulebook format.

    Returns "" when the rulebook does not declare a team mode or win condition.
    """
    fmt = rulepack.format
    team_mode = TEAM_MODE_CODES.get(str(fmt.get("team_mode", "")).lower().replace(" ", ""))
    win_condition = WIN_CONDITION_CODES.get(str(fmt.get("win_condition", "")).lower().replace(" ", ""))
    if team_mode is None and win_condition is None:
        return ""
    try:
        size = int(fmt.get("slots") or 0)
    except (TypeError, ValueError):
        size = 0
    size = size or max(player_count, 2)
    return f"!mp set {team_mode or 0} {win_condition or 0} {size}"


def parse_roll(text: str) -> tuple[str, int] | None:
    """Parse a BanchoBot "<player> rolls N point(s)" line."""
    match = MP_ROLL_RE.match(text.strip())
    if not match:
        return None
    try:
        return match.group(1).strip(), int(match.group(2))
    except ValueError:
        return None


def parse_bp_order_choice(command: str) -> tuple[str, str] | None:
    """Parse a roll-winner/loser order choice: "pick first" / "last ban" / ...

    Returns (phase, position) with phase in {pick, ban} and position in {first, last}.
    """
    parts = command.lower().split()
    if len(parts) < 2:
        return None
    a, b = parts[0], parts[1]
    phases = {"pick", "ban"}
    positions = {"first": "first", "1st": "first", "last": "last", "second": "last", "2nd": "last"}
    if a in phases and b in positions:
        return a, positions[b]
    if a in positions and b in phases:
        return b, positions[a]
    return None


def is_ban_waive(command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    return normalized in {"ban skip", "skip ban", "ban pass", "pass ban", "ban none", "no ban"}


# Single source of truth for !ref routing. Verbs the deterministic engine owns
# (handled inside RefereeEngine.handle_message); keep this in sync with that.
ENGINE_REF_VERBS = {
    "pick", "ban", "pass", "skip", "first", "last", "second", "1st", "2nd",
    "pause", "p", "ready", "r", "ff", "forfeit", "abort", "roll",
}
# !ref bodies that ask the read-only assistant rather than drive the match.
QUERY_REF_KEYWORDS = {"ask", "?", "status", "状态", "help"}


def classify_ref_message(session: "RefereeSession", sender: str, message: str) -> str:
    """Authoritative routing for a room message. Returns one of:

    - 'none'     : not a !ref command from a session player → nobody acts (plain chat).
    - 'engine'   : a referee command the deterministic engine owns (valid pick/ban, pause/…).
    - 'query'    : a question for the read-only assistant (!ref ?, !ref ask …, !ref status).
    - 'bp_error' : a pick/ban that fails the mappool table (nonexistent / already banned /
                   already played) → AI handles it as an engine-level error.
    - 'freeform' : any other !ref text → the AI controller (residual / ambiguous case).
    """
    if not session.team_for_player(sender):
        return "none"
    body = strip_player_command_prefix(message)
    if body is None:
        return "none"
    low = body.strip().lower()
    first = low.split()[0] if low.split() else ""
    if not low or first in QUERY_REF_KEYWORDS or low.startswith("?"):
        return "query"
    if first in ENGINE_REF_VERBS:
        # A pick/ban that names a map is checked against the shared mappool table:
        # valid map-state → engine; mappool error → AI. (Order choices / ban-waive
        # and turn/TB rules remain the engine's.)
        if first in {"pick", "ban"} and session.is_bo_mode:
            if not parse_bp_order_choice(low) and not (first == "ban" and is_ban_waive(low)):
                tokens = body.split()
                code = tokens[1] if len(tokens) > 1 else ""
                if code and bp_command_error(session, first, code):
                    return "bp_error"
        return "engine"
    return "freeform"


# Commands the AI controller may send automatically. Anything else (abort, close,
# set, kick, score-affecting, or unknown) is held for human confirmation.
CONTROLLER_AUTO_VERBS = {"timer", "settings", "map", "mods", "start", "invite"}


def classify_mp_command(command: str) -> str:
    """Classify an AI-proposed command: 'auto' (whitelisted), 'hold' (needs human), 'reject'."""
    parts = command.strip().split()
    if len(parts) < 2 or parts[0].lower() != "!mp":
        return "reject"
    return "auto" if parts[1].lower() in CONTROLLER_AUTO_VERBS else "hold"


def format_status(snapshot: dict[str, Any]) -> str:
    """Deterministic one-line status (for !ref ? and the no-LLM fallback)."""
    parts = [f"阶段 {snapshot.get('stage', '?')}"]
    score = snapshot.get("score") or {}
    if score:
        parts.append("比分 " + " ".join(f"{name} {value}" for name, value in score.items()))
    turn = snapshot.get("turn")
    if turn and turn.get("team"):
        parts.append(f"轮到 {turn['team']} {turn['action']}")
    if snapshot.get("banned"):
        parts.append("已ban " + ",".join(snapshot["banned"]))
    if snapshot.get("managed_bp"):
        parts.append("TB " + ("可选" if snapshot.get("tb_available") else "未解锁"))
    if snapshot.get("match_winner"):
        parts.append(f"胜者 {snapshot['match_winner']}")
    return " | ".join(parts)


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
        pick_ban_timer=int_field("pick_ban_timer", DEFAULT_PICK_BAN_TIMER),
        prep_timer=int_field("prep_timer", DEFAULT_PREP_TIMER),
        tb_prep_timer=int_field("tb_prep_timer", DEFAULT_TB_PREP_TIMER),
        pause_timer=int_field("pause_timer", DEFAULT_PAUSE_TIMER),
        pause_per_player=int_field("pause_per_player", DEFAULT_PAUSE_PER_PLAYER),
        abort_window=int_field("abort_window", DEFAULT_ABORT_WINDOW),
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
            if not banchobot_relevant(text):
                return  # drop BanchoBot/SYSTEM noise before any processing
            match = MP_ROOM_RE.search(text)
            if tag == "BanchoBot" and match and state.stage == "creating_room":
                state.room_id = match.group(1)
                state.channel = f"#mp_{state.room_id}"
                state.stage = "inviting"
            if session.is_bo_mode:
                self._bo_observe_system(session, text)
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
                if not banchobot_relevant(message):
                    return  # drop BanchoBot noise relayed into the channel
                if session.is_bp_managed:
                    self._bp_observe_roll(session, message)
                if session.is_bo_mode:
                    self._bo_observe_system(session, message)
                if session.rulepack.start_on_system_all_ready and is_all_ready_system_message(message):
                    state.all_ready_confirmed = True
                return
            command = strip_player_command_prefix(message)
            if command is None:
                return
            if self._is_session_player(session, sender) and is_pause_message(command):
                if session.is_bo_mode:
                    self._bo_handle_pause(session, sender)
                else:
                    state.player_pause_requested = True
                    self.pause(session, f"player requested pause: {sender}")
                return
            if self._is_session_player(session, sender) and is_ready_message(command):
                state.ready_settings_requested = True
                if sender not in state.ready_players:
                    state.ready_players.append(sender)
                return
            if self._is_session_player(session, sender):
                normalized = command.strip().lower()
                if session.is_bo_mode and normalized in {"ff", "forfeit"}:
                    self._bo_handle_ff(session, sender)
                    return
                if session.is_bo_mode and normalized == "abort":
                    self._bo_handle_abort(session, sender)
                    return
                if session.is_bp_managed:
                    self._bp_handle_player(session, sender, command)
                elif session.is_bo_mode:
                    self._bo_handle_pick_ban(session, command)

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
        if session.is_bo_mode and state.stage in {"bo_roll", "bo_pick", "bo_ready", "bo_playing"}:
            return self._bo_next_actions(session)
        if state.stage in {"configured", "scheduled"}:
            return self._missing_actions(
                session,
                [RefereeAction("make_room", "BanchoBot", f"!mp make {session.room_name}", reason="create mp room")],
            )
        if state.stage == "inviting" and state.channel:
            actions: list[RefereeAction] = []
            set_cmd = mp_set_command(session.rulepack, len(session.all_players))
            if set_cmd:
                actions.append(RefereeAction("set_room", state.channel, set_cmd, reason="configure room"))
            actions += [
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
                self._enter_post_invite_stage(session)
        elif state.stage == "inviting" and self._inviting_complete(session):
            self._enter_post_invite_stage(session)
        elif action.id == "ready_start":
            state.stage = "playing"
        elif action.id.startswith("setmap:"):
            state.stage = "bo_ready"
            prep = session.rulepack.tb_prep_timer if is_tb_code(state.current_pick_code) else session.rulepack.prep_timer
            state.join_timer_deadline = time.time() + prep
            state.turn_deadline = 0.0
        elif action.id.startswith("bo_start:"):
            state.stage = "bo_playing"
            state.play_started_at = time.time()
        elif action.id.startswith(("turn_ban:", "turn_pick:")):
            state.turn_deadline = time.time() + session.rulepack.pick_ban_timer
        elif action.id == "bo_announce":
            state.pending_announce = ""
        elif action.id == "bo_abort":
            # Map aborted: keep the selection, re-run ready -> settings -> start.
            self._reset_start_actions(session, state.current_pick_code)
            state.current_map_scores = {}
            state.all_ready_confirmed = False
            state.ready_players = []
            state.pending_abort = False
            state.stage = "bo_ready"
            prep = session.rulepack.tb_prep_timer if is_tb_code(state.current_pick_code) else session.rulepack.prep_timer
            state.join_timer_deadline = time.time() + prep
        elif action.id == "bo_close":
            state.stage = "finished"

    def _post_invite_stage(self, session: RefereeSession) -> str:
        if session.is_bp_managed:
            return "bo_roll"
        return "bo_pick" if session.is_bo_mode else "waiting_players"

    def _enter_post_invite_stage(self, session: RefereeSession) -> None:
        session.state.stage = self._post_invite_stage(session)
        # The roll phase opens exactly when we reach bo_roll.
        session.state.roll_phase = session.state.stage == "bo_roll"

    def describe_state(self, session: RefereeSession) -> dict[str, Any]:
        """Deterministic snapshot of the match for the AI assistant to ground on.

        The assistant only *reads* this; it never computes score/turn/legality.
        """
        state = session.state
        snap: dict[str, Any] = {
            "stage": state.stage,
            "best_of": session.best_of,
            "first_to": session.first_to,
            "score": dict(state.score),
            "current_map": state.current_pick_code,
            "banned": list(state.banned_codes),
            "played": list(state.played_codes),
            "managed_bp": session.is_bp_managed,
            "paused": state.paused,
            "human_controlled": state.human_controlled,
            "match_winner": state.match_winner,
            "mappool": mappool_status(session),  # the shared mappool table the AI reads
        }
        if session.is_bp_managed:
            action, team = self._bp_current(session)
            snap["turn"] = {"action": action, "team": team} if team and state.stage == "bo_pick" else None
            template = session.bp_template
            snap["remaining_bans"] = sum(1 for entry in template[state.bp_step:] if entry == "ban")
            snap["tb_available"] = self._both_match_point(session)
            snap["pick_order"] = list(state.pick_order)
            snap["ban_order"] = list(state.ban_order)
        return snap

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

    # -- best-of bracket logic (active only when rulepack.format has best_of) --

    def _bo_handle_pick_ban(self, session: RefereeSession, command: str) -> None:
        parsed = parse_pick_ban(command)
        if not parsed:
            return
        state = session.state
        verb, code = parsed
        if verb == "ban":
            if not bp_command_error(session, "ban", code):
                state.banned_codes.append(code)
            return
        # verb == "pick": only accept while awaiting a pick and if the map is available.
        if state.stage != "bo_pick":
            return
        if bp_command_error(session, "pick", code):
            return
        state.current_pick_code = code

    def _bo_observe_system(self, session: RefereeSession, text: str) -> None:
        state = session.state
        if state.stage != "bo_playing":
            return
        result = parse_play_result(text)
        if result:
            player, score = result
            state.current_map_scores[player] = score
            return
        if MATCH_FINISHED_RE.search(text):
            self._bo_finish_map(session)

    def _bo_finish_map(self, session: RefereeSession) -> None:
        state = session.state
        teams = session.config.teams
        code = state.current_pick_code
        red_score, blue_score = 0, 0
        if len(teams) >= 2:
            red, blue = teams[0], teams[1]
            red_players = {player.lower() for player in red.players}
            blue_players = {player.lower() for player in blue.players}
            for player, score in state.current_map_scores.items():
                if player.lower() in red_players:
                    red_score += score
                elif player.lower() in blue_players:
                    blue_score += score
        # Reset per-map ready/score scratch before moving on.
        state.current_map_scores = {}
        state.all_ready_confirmed = False
        state.ready_players = []
        state.ready_settings_requested = False
        state.player_pause_requested = False
        state.join_timer_deadline = 0.0
        state.paused_this_map = []
        state.play_started_at = 0.0
        if len(teams) < 2:
            state.current_pick_code = ""
            state.stage = "bo_pick"
            return
        red, blue = teams[0], teams[1]
        if red_score == blue_score:
            # Tie on the map -> replay the same map (rule 六.7). Keep the pick and
            # clear its per-map action ids so the map/start commands re-emit.
            self._reset_map_actions(session, code)
            state.stage = "bo_pick"
            rs, bs = state.score.get(red.name, 0), state.score.get(blue.name, 0)
            state.pending_announce = f"{red.name} | {rs} - {bs} | {blue.name} // tie on {code}, replay"
            return
        # Decisive map: count it.
        if code and code not in state.played_codes:
            state.played_codes.append(code)
        state.current_pick_code = ""
        if session.is_bp_managed:
            state.pick_count += 1
            if state.bp_step < len(session.bp_template):
                state.bp_step += 1
        winner = red if red_score > blue_score else blue
        state.score[winner.name] = state.score.get(winner.name, 0) + 1
        rs, bs = state.score.get(red.name, 0), state.score.get(blue.name, 0)
        first_to = session.first_to
        if first_to and (rs >= first_to or bs >= first_to):
            state.match_winner = winner.name
            state.pending_announce = f"{red.name} | {rs} - {bs} | {blue.name} // {winner.name} wins! GGWP"
            # stage stays bo_playing; _bo_next_actions announces then closes the room.
            return
        state.stage = "bo_pick"
        directive = "next pick/ban"
        if session.is_bp_managed:
            action, team = self._bp_current(session)
            if team:
                directive = f"next: {team} to {action}"
        state.pending_announce = f"{red.name} | {rs} - {bs} | {blue.name} // Best of {session.best_of} - {directive}"

    def _reset_map_actions(self, session: RefereeSession, code: str) -> None:
        if not code:
            return
        ids = {f"setmap:{code}", f"setmods:{code}", f"settimer:{code}", f"bo_settings:{code}", f"bo_start:{code}"}
        session.state.sent_actions = [a for a in session.state.sent_actions if a not in ids]

    def _bo_next_actions(self, session: RefereeSession) -> list[RefereeAction]:
        state = session.state
        channel = state.channel
        if not channel:
            return []
        if state.pending_abort:
            return [RefereeAction("bo_abort", channel, "!mp abort", reason="incident abort")]
        if state.pending_announce:
            return [RefereeAction("bo_announce", channel, state.pending_announce, reason="score update")]
        if state.match_winner:
            return [RefereeAction("bo_close", channel, "!mp close", reason="match finished")]
        if state.stage == "bo_roll":
            if not self._bp_roll_done(session):
                return self._missing_actions(
                    session,
                    [RefereeAction("roll_prompt", channel, "请双方 !roll 决定 ban/pick 先后手", reason="roll")],
                )
            winner = self._bp_winner_team(session)
            if winner and not (len(state.pick_order) == 2 and len(state.ban_order) == 2):
                return self._missing_actions(
                    session,
                    [
                        RefereeAction(
                            "order_prompt",
                            channel,
                            f"{winner} roll 胜，请用 !ref pick first|last 或 !ref ban first|last 选择先后手",
                            reason="order choice",
                        )
                    ],
                )
            return []
        if state.stage == "bo_pick":
            if session.is_bp_managed and not state.current_pick_code:
                action, team = self._bp_current(session)
                if action == "ban" and team:
                    pid = f"turn_ban:{state.bp_step}"
                    prompt = f"轮到 {team} ban（!ref ban <code> 或 !ref ban skip）"
                    return self._missing_actions(session, [RefereeAction(pid, channel, prompt, reason="ban turn")])
                if action == "pick" and team:
                    pid = f"turn_pick:{state.bp_step}:{state.pick_count}"
                    prompt = f"轮到 {team} pick（!ref pick <code>）"
                    return self._missing_actions(session, [RefereeAction(pid, channel, prompt, reason="pick turn")])
            code = state.current_pick_code
            if not code or code in state.played_codes:
                return []
            entry = mappool_lookup(session.rulepack, code)
            if not entry:
                return []
            # Prefer the exact commands authored in the rulebook mappool; fall back
            # to constructing them from beatmap_id/mods when only those are present.
            map_cmd = str(entry.get("map_command") or f"!mp map {entry.get('beatmap_id')} 0").strip()
            actions = [RefereeAction(f"setmap:{code}", channel, map_cmd, reason=f"pick {code}")]
            mods = str(entry.get("mods", "")).strip()
            mod_cmd = str(entry.get("mod_command") or (f"!mp mods {mods.lower()}" if mods else "")).strip()
            if mod_cmd:
                actions.append(RefereeAction(f"setmods:{code}", channel, mod_cmd, reason=f"mods for {code}"))
            prep = session.rulepack.tb_prep_timer if is_tb_code(code) else session.rulepack.prep_timer
            actions.append(
                RefereeAction(f"settimer:{code}", channel, f"!mp timer {prep}", reason="prep timer")
            )
            return self._missing_actions(session, actions)
        if state.stage == "bo_ready" and self.should_start_match(session):
            code = state.current_pick_code or "map"
            actions = [
                RefereeAction(f"bo_settings:{code}", channel, "!mp settings", reason="verify room before start"),
                RefereeAction(
                    f"bo_start:{code}",
                    channel,
                    f"!mp start {session.rulepack.ready_start_countdown}",
                    reason="start condition met",
                ),
            ]
            return self._missing_actions(session, actions)
        return []

    # -- managed ban/pick (active only when the rulebook declares a bp_order) --

    def _bp_observe_roll(self, session: RefereeSession, text: str) -> None:
        state = session.state
        # Only recognise roll messages while the roll phase is open (players may
        # !roll for fun at other times — those must be ignored).
        if not state.roll_phase or (len(state.pick_order) == 2 and len(state.ban_order) == 2):
            return
        roll = parse_roll(text)
        if not roll:
            return
        player, value = roll
        team = session.team_for_player(player)
        if not team or team.name in state.rolls:  # first valid roll per team counts
            return
        state.rolls[team.name] = value
        if self._bp_roll_done(session):
            values = sorted(state.rolls.values(), reverse=True)
            if len(values) >= 2 and values[0] == values[1]:  # tie -> reroll (rule 1.4)
                state.rolls = {}
                state.pending_announce = "roll 平局，请双方重新 !roll"

    def _bp_roll_done(self, session: RefereeSession) -> bool:
        names = {team.name for team in session.config.teams}
        return bool(names) and names.issubset(set(session.state.rolls))

    def _bp_winner_team(self, session: RefereeSession) -> str:
        rolls = session.state.rolls
        if not self._bp_roll_done(session):
            return ""
        return max(rolls, key=lambda name: rolls[name])

    def _bp_apply_order_choice(self, session: RefereeSession, sender: str, phase: str, position: str) -> None:
        state = session.state
        winner = self._bp_winner_team(session)
        actor = session.team_for_player(sender)
        if not winner or not actor or len(session.config.teams) < 2:
            return
        other = next((t.name for t in session.config.teams if t.name != actor.name), "")
        pick_set = len(state.pick_order) == 2
        ban_set = len(state.ban_order) == 2

        def ordered() -> list[str]:
            return [actor.name, other] if position == "first" else [other, actor.name]

        if actor.name == winner:
            if phase == "pick" and not pick_set:
                state.pick_order = ordered()
            elif phase == "ban" and not ban_set:
                state.ban_order = ordered()
            else:
                return
        else:
            # The loser may only set the phase the winner did not take, and only
            # after the winner has chosen.
            if not (pick_set or ban_set):
                return
            if phase == "pick" and not pick_set:
                state.pick_order = ordered()
            elif phase == "ban" and not ban_set:
                state.ban_order = ordered()
            else:
                return
        if len(state.pick_order) == 2 and len(state.ban_order) == 2:
            state.stage = "bo_pick"
            state.roll_phase = False  # roll phase closes once order is decided
            action, team = self._bp_current(session)
            tail = f" // {team} to {action}" if team else ""
            state.pending_announce = (
                f"BP 顺序：pick {'>'.join(state.pick_order)}，ban {'>'.join(state.ban_order)}{tail}"
            )

    def _bp_current(self, session: RefereeSession) -> tuple[str, str]:
        state = session.state
        if len(state.pick_order) < 2 or len(state.ban_order) < 2:
            return "", ""
        template = session.bp_template
        action = template[state.bp_step] if state.bp_step < len(template) else "pick"
        if action == "ban":
            team = state.ban_order[state.ban_count % 2]
        else:
            team = state.pick_order[state.pick_count % 2]
        return action, team

    def _both_match_point(self, session: RefereeSession) -> bool:
        first_to = session.first_to
        if not first_to:
            return False
        return all(session.state.score.get(team.name, 0) >= first_to - 1 for team in session.config.teams[:2])

    def _bp_handle_player(self, session: RefereeSession, sender: str, command: str) -> None:
        state = session.state
        if state.stage == "bo_roll":
            if not (len(state.pick_order) == 2 and len(state.ban_order) == 2):
                choice = parse_bp_order_choice(command)
                if choice and self._bp_roll_done(session):
                    self._bp_apply_order_choice(session, sender, *choice)
            return
        if state.stage != "bo_pick":
            return
        action, team = self._bp_current(session)
        actor = session.team_for_player(sender)
        if not actor or not team or actor.name != team:
            return  # not this team's turn
        if action == "ban":
            if is_ban_waive(command):
                self._bp_advance_after_ban(session, team, None)
                return
            parsed = parse_pick_ban(command)
            if not parsed or parsed[0] != "ban":
                return
            code = parsed[1]
            if is_tb_code(code):
                return  # TB maps can never be banned (rule 4.2)
            if bp_command_error(session, "ban", code):
                return
            state.banned_codes.append(code)
            self._bp_advance_after_ban(session, team, code)
            return
        # action == "pick"
        parsed = parse_pick_ban(command)
        if not parsed or parsed[0] != "pick":
            return
        code = parsed[1]
        if bp_command_error(session, "pick", code):
            return
        both_match_point = self._both_match_point(session)
        if is_tb_code(code) and not both_match_point:
            return  # TB only after both reach match point (rule 5.1)
        if both_match_point and not is_tb_code(code):
            return  # at double match point the decider must be TB
        state.current_pick_code = code  # map is emitted by _bo_next_actions

    def _bp_advance_after_ban(self, session: RefereeSession, team: str, code: str | None) -> None:
        state = session.state
        state.bp_step += 1
        state.ban_count += 1
        state.turn_deadline = 0.0
        what = f"banned {code}" if code else "skipped ban"
        action, next_team = self._bp_current(session)
        tail = f" // next: {next_team} to {action}" if next_team else ""
        state.pending_announce = f"{team} {what}{tail}"

    def tick_timeouts(self, session: RefereeSession, now: float | None = None) -> None:
        """Apply ban/pick decision timeouts (rule 三.7) for managed brackets.

        Called every agent loop. Prep/start timeouts are handled separately via
        join_timer_deadline + should_start_match.
        """
        state = session.state
        now = time.time() if now is None else now
        # Pause auto-resume (rule 五): a pause lasts pause_timer seconds.
        if state.paused and state.pause_deadline and now >= state.pause_deadline:
            self.resume(session)
            state.pause_deadline = 0.0
            return
        if not session.is_bp_managed:
            return
        if state.stage != "bo_pick" or state.current_pick_code or not state.turn_deadline:
            return
        if now < state.turn_deadline:
            return
        action, team = self._bp_current(session)
        if action == "ban":
            self._bp_advance_after_ban(session, team, None)  # ban timeout = waive (rule 4.3)
        elif action == "pick":
            code = self._bp_auto_pick(session)
            state.turn_deadline = 0.0
            if code:
                state.current_pick_code = code
                state.pending_announce = f"{team} pick 超时，裁判选 {code}"  # rule 5.2

    def _bp_auto_pick(self, session: RefereeSession) -> str:
        state = session.state
        both_match_point = self._both_match_point(session)
        for entry in session.rulepack.mappool:
            code = str(entry.get("code", ""))
            if not code or code in state.banned_codes or code in state.played_codes:
                continue
            if is_tb_code(code) and not both_match_point:
                continue
            if both_match_point and not is_tb_code(code):
                continue
            return code
        return ""

    # -- incidents: pause (rule 五), forfeit (rule 四), abort (rule 六) --

    def _bo_handle_pause(self, session: RefereeSession, sender: str) -> None:
        state = session.state
        team = session.team_for_player(sender)
        if not team:
            return
        if state.stage not in {"bo_pick", "bo_ready"}:
            return  # only during ban/pick or prep (rule 五.2.1)
        if team.name in state.paused_this_map:
            return  # one pause per side per map (rule 五.2.2)
        used = state.pause_counts.get(sender, 0)
        if used >= session.rulepack.pause_per_player:
            return  # pause budget exhausted (rule 五.1)
        state.pause_counts[sender] = used + 1
        state.paused_this_map.append(team.name)
        state.pause_deadline = time.time() + session.rulepack.pause_timer
        self.pause(session, f"{sender} pause ({used + 1}/{session.rulepack.pause_per_player})")

    def _bo_handle_ff(self, session: RefereeSession, sender: str) -> None:
        state = session.state
        teams = session.config.teams
        loser = session.team_for_player(sender)
        if not loser or len(teams) < 2:
            return
        winner = next((team for team in teams if team.name != loser.name), None)
        if not winner:
            return
        red, blue = teams[0], teams[1]
        rs, bs = state.score.get(red.name, 0), state.score.get(blue.name, 0)
        state.match_winner = winner.name
        state.paused = False
        state.stage = "bo_playing"  # routes to _bo_next_actions -> announce + close
        state.pending_announce = f"{red.name} | {rs} - {bs} | {blue.name} // {loser.name} FF, {winner.name} wins! GGWP"

    def _bo_handle_abort(self, session: RefereeSession, sender: str) -> None:
        state = session.state
        if state.stage != "bo_playing" or not state.current_pick_code:
            return
        if state.play_started_at and (time.time() - state.play_started_at) > session.rulepack.abort_window:
            return  # past the abort window (rule 六.4.1: within 30s)
        state.pending_abort = True

    def _reset_start_actions(self, session: RefereeSession, code: str) -> None:
        if not code:
            return
        ids = {f"bo_settings:{code}", f"bo_start:{code}"}
        session.state.sent_actions = [a for a in session.state.sent_actions if a not in ids]

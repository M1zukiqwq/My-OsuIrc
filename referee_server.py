"""Local HTTP and SQLite backend for the osu! referee agent."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai_referee import OpenAICompatibleClient
from referee import (
    DEFAULT_ROOM_LEAD_TIME_SEC,
    RefereeEngine,
    RefereeSession,
    RefereeStore,
    RulePack,
    SessionConfig,
    SessionState,
    Team,
    default_rulepack,
    new_id,
    rulepack_from_draft,
    session_to_dict,
)


KEEP_ASSIGNED = object()


def utcish_now() -> float:
    return time.time()


def parse_match_time(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def session_from_parts(rulepack: RulePack, config_data: dict[str, Any], state_data: dict[str, Any]) -> RefereeSession:
    config_raw = dict(config_data)
    config_raw["teams"] = [Team(**team) for team in config_raw.get("teams", [])]
    config = SessionConfig(**config_raw)
    state = SessionState(**dict(state_data))
    return RefereeSession(config=config, rulepack=rulepack, state=state)


class SQLiteRefereeStore:
    """SQLite-backed source of truth for rulepacks, sessions, agents, and events."""

    def __init__(self, db_path: str | Path = "referee.db") -> None:
        self.db_path = str(db_path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self.ensure_default_rulepack()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _init_schema(self) -> None:
        with self.lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rulepacks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    confirmed INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    match_time TEXT NOT NULL,
                    room_lead_time_sec INTEGER NOT NULL,
                    assigned_agent_id TEXT,
                    config_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    last_seen_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    current_session_id TEXT
                );
                """
            )

    def ensure_default_rulepack(self) -> RulePack:
        existing = self.load_rulepack("default")
        if existing:
            return existing
        rulepack = default_rulepack()
        self.save_rulepack(rulepack)
        return rulepack

    def save_rulepack(self, rulepack: RulePack) -> RulePack:
        now = utcish_now()
        payload = json.dumps(asdict(rulepack), ensure_ascii=False, sort_keys=True)
        with self.lock, self.conn:
            current = self.conn.execute("SELECT created_at FROM rulepacks WHERE id = ?", (rulepack.id,)).fetchone()
            created_at = current["created_at"] if current else rulepack.created_at
            self.conn.execute(
                """
                INSERT INTO rulepacks (id, name, confirmed, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    confirmed=excluded.confirmed,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (rulepack.id, rulepack.name, int(rulepack.confirmed), payload, created_at, now),
            )
        return rulepack

    def load_rulepack(self, rulepack_id: str) -> RulePack | None:
        with self.lock:
            row = self.conn.execute("SELECT payload_json FROM rulepacks WHERE id = ?", (rulepack_id,)).fetchone()
        if not row:
            return None
        return RulePack(**json.loads(row["payload_json"]))

    def list_rulepacks(self) -> list[RulePack]:
        with self.lock:
            rows = self.conn.execute("SELECT payload_json FROM rulepacks ORDER BY id != 'default', name").fetchall()
        return [RulePack(**json.loads(row["payload_json"])) for row in rows]

    def confirm_rulepack(self, rulepack_id: str) -> RulePack | None:
        rulepack = self.load_rulepack(rulepack_id)
        if not rulepack:
            return None
        rulepack.confirmed = True
        return self.save_rulepack(rulepack)

    def save_session(self, session: RefereeSession, assigned_agent_id: Any = KEEP_ASSIGNED) -> RefereeSession:
        now = utcish_now()
        session.state.updated_at = now
        config_json = json.dumps(asdict(session.config), ensure_ascii=False, sort_keys=True)
        state_json = json.dumps(asdict(session.state), ensure_ascii=False, sort_keys=True)
        with self.lock, self.conn:
            current = self.conn.execute(
                "SELECT created_at, assigned_agent_id FROM sessions WHERE id = ?",
                (session.id,),
            ).fetchone()
            created_at = current["created_at"] if current else now
            if assigned_agent_id is KEEP_ASSIGNED:
                assigned = current["assigned_agent_id"] if current else None
            else:
                assigned = assigned_agent_id
            self.conn.execute(
                """
                INSERT INTO sessions (
                    id, status, match_time, room_lead_time_sec, assigned_agent_id,
                    config_json, state_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    match_time=excluded.match_time,
                    room_lead_time_sec=excluded.room_lead_time_sec,
                    assigned_agent_id=excluded.assigned_agent_id,
                    config_json=excluded.config_json,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    session.id,
                    session.state.stage,
                    session.config.match_time,
                    int(session.config.room_lead_time_sec),
                    assigned,
                    config_json,
                    state_json,
                    created_at,
                    now,
                ),
            )
        return session

    def load_session(self, session_id: str) -> RefereeSession | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def load_session_record(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_sessions(self) -> list[RefereeSession]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [session for row in rows if (session := self._row_to_session(row))]

    def list_session_records(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [record for row in rows if (record := self._row_to_record(row))]

    def update_session_state(self, session_id: str, state_data: dict[str, Any]) -> RefereeSession | None:
        session = self.load_session(session_id)
        if not session:
            return None
        merged = asdict(session.state)
        merged.update(state_data)
        session.state = SessionState(**merged)
        return self.save_session(session)

    def create_scheduled_session(
        self,
        name: str,
        rulepack_id: str,
        teams: list[dict[str, Any]],
        match_time: str = "",
        room_lead_time_sec: int = DEFAULT_ROOM_LEAD_TIME_SEC,
        mappool_override: list[dict[str, Any]] | None = None,
    ) -> RefereeSession:
        rulepack = self.load_rulepack(rulepack_id)
        if not rulepack:
            raise ValueError("rulepack not found")
        if not rulepack.confirmed:
            raise PermissionError("rulepack is not confirmed")
        team_objs = [Team(name=str(team.get("name", "")), players=list(team.get("players", []))) for team in teams]
        config = SessionConfig(
            id=new_id("session", name),
            name=name,
            rulepack_id=rulepack.id,
            teams=team_objs,
            match_time=match_time,
            room_lead_time_sec=int(room_lead_time_sec or DEFAULT_ROOM_LEAD_TIME_SEC),
            mappool_override=mappool_override or [],
        )
        state = SessionState(session_id=config.id, stage="scheduled", score={team.name: 0 for team in team_objs})
        session = RefereeSession(config=config, rulepack=rulepack, state=state)
        self.save_session(session, assigned_agent_id=None)
        self.append_event(session.id, "session_created", {"name": name, "rulepack_id": rulepack.id})
        return session

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float | None = None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO events (session_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at or utcish_now(),
                ),
            )

    def list_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def heartbeat(self, agent_id: str, status: str = "running", current_session_id: str = "") -> dict[str, Any]:
        now = utcish_now()
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO agents (id, last_seen_at, status, current_session_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    status=excluded.status,
                    current_session_id=excluded.current_session_id
                """,
                (agent_id, now, status, current_session_id or None),
            )
        return {"agent_id": agent_id, "last_seen_at": now, "status": status}

    def claim_due_sessions(self, agent_id: str, limit: int = 1, now: float | None = None) -> list[dict[str, Any]]:
        now = utcish_now() if now is None else now
        with self.lock, self.conn:
            rows = self.conn.execute(
                """
                SELECT * FROM sessions
                WHERE assigned_agent_id IS NULL
                  AND status IN ('configured', 'scheduled')
                ORDER BY match_time, created_at
                """
            ).fetchall()
            due_rows = []
            for row in rows:
                match_ts = parse_match_time(row["match_time"])
                due_at = match_ts - int(row["room_lead_time_sec"]) if match_ts else 0.0
                if due_at <= now:
                    due_rows.append(row)
                if len(due_rows) >= limit:
                    break
            claimed_ids = [row["id"] for row in due_rows]
            for session_id in claimed_ids:
                self.conn.execute(
                    "UPDATE sessions SET assigned_agent_id = ?, updated_at = ? WHERE id = ? AND assigned_agent_id IS NULL",
                    (agent_id, now, session_id),
                )
                self.conn.execute(
                    "INSERT INTO events (session_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (
                        session_id,
                        "agent_claimed",
                        json.dumps({"agent_id": agent_id}, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
        return [record for session_id in claimed_ids if (record := self.load_session_record(session_id))]

    def agent_tasks(self, agent_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM sessions
                WHERE assigned_agent_id = ? AND status NOT IN ('finished')
                ORDER BY updated_at DESC
                """,
                (agent_id,),
            ).fetchall()
        return [record for row in rows if (record := self._row_to_record(row))]

    def _row_to_session(self, row: sqlite3.Row | None) -> RefereeSession | None:
        if not row:
            return None
        config = json.loads(row["config_json"])
        rulepack = self.load_rulepack(config.get("rulepack_id", "default")) or default_rulepack()
        state = json.loads(row["state_json"])
        return session_from_parts(rulepack, config, state)

    def _row_to_record(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        session = self._row_to_session(row)
        if not session or not row:
            return None
        record = session_to_dict(session)
        record.update(
            {
                "status": row["status"],
                "assigned_agent_id": row["assigned_agent_id"] or "",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        return record


class RefereeHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: SQLiteRefereeStore) -> None:
        super().__init__(address, RefereeRequestHandler)
        self.store = store
        self.engine = RefereeEngine()


class RefereeRequestHandler(BaseHTTPRequestHandler):
    server: RefereeHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            parts = [part for part in path.split("/") if part]
            if path == "/api/health":
                self._send({"ok": True, "time": utcish_now()})
            elif path == "/api/rulepacks":
                self._send({"rulepacks": [asdict(pack) for pack in self.server.store.list_rulepacks()]})
            elif path == "/api/sessions":
                self._send({"sessions": self.server.store.list_session_records()})
            elif len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                self._send_record_or_404(parts[2])
            elif len(parts) == 4 and parts[:2] == ["api", "agent"] and parts[3] == "tasks":
                self._send({"sessions": self.server.store.agent_tasks(parts[2])})
            else:
                self._send({"error": "not found"}, status=404)
        except Exception as exc:
            self._send({"error": str(exc)}, status=500)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            parts = [part for part in path.split("/") if part]
            body = self._read_json()
            if path == "/api/rulepacks/draft":
                self._handle_rulepack_draft(body)
            elif len(parts) == 4 and parts[:2] == ["api", "rulepacks"] and parts[3] == "confirm":
                self._handle_rulepack_confirm(parts[2])
            elif path == "/api/sessions":
                self._handle_create_session(body)
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "control":
                self._handle_session_control(parts[2], body)
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "events":
                self._handle_session_event(parts[2], body)
            elif path == "/api/agent/heartbeat":
                self._handle_agent_heartbeat(body)
            elif path == "/api/agent/claim":
                self._handle_agent_claim(body)
            else:
                self._send({"error": "not found"}, status=404)
        except PermissionError as exc:
            self._send({"error": str(exc)}, status=409)
        except ValueError as exc:
            self._send({"error": str(exc)}, status=400)
        except Exception as exc:
            self._send({"error": str(exc)}, status=500)

    def _handle_rulepack_draft(self, body: dict[str, Any]) -> None:
        name = str(body.get("name") or "Untitled RulePack")
        rules_url = str(body.get("rules_url") or "")
        mappool_url = str(body.get("mappool_url") or "")
        draft = body.get("draft") if isinstance(body.get("draft"), dict) else None
        ai_error = ""
        if draft is None:
            ai_client = OpenAICompatibleClient.from_env()
            if ai_client:
                try:
                    draft = ai_client.extract_rulepack(name=name, rules_url=rules_url, mappool_url=mappool_url)
                except Exception as exc:
                    ai_error = str(exc)
                    draft = {}
            else:
                draft = {}
        rulepack = rulepack_from_draft(name=name, rules_url=rules_url, mappool_url=mappool_url, draft=draft)
        self.server.store.save_rulepack(rulepack)
        self._send({"rulepack": asdict(rulepack), "ai_error": ai_error}, status=201)

    def _handle_rulepack_confirm(self, rulepack_id: str) -> None:
        rulepack = self.server.store.confirm_rulepack(rulepack_id)
        if not rulepack:
            self._send({"error": "rulepack not found"}, status=404)
            return
        self._send({"rulepack": asdict(rulepack)})

    def _handle_create_session(self, body: dict[str, Any]) -> None:
        session = self.server.store.create_scheduled_session(
            name=str(body.get("name") or "Untitled Match"),
            rulepack_id=str(body.get("rulepack_id") or "default"),
            teams=list(body.get("teams") or []),
            match_time=str(body.get("match_time") or ""),
            room_lead_time_sec=int(body.get("room_lead_time_sec") or DEFAULT_ROOM_LEAD_TIME_SEC),
            mappool_override=list(body.get("mappool_override") or []),
        )
        self._send({"session": self.server.store.load_session_record(session.id)}, status=201)

    def _handle_session_control(self, session_id: str, body: dict[str, Any]) -> None:
        session = self.server.store.load_session(session_id)
        if not session:
            self._send({"error": "session not found"}, status=404)
            return
        action = str(body.get("action") or "")
        reason = str(body.get("reason") or action)
        if action == "pause":
            self.server.engine.pause(session, reason)
        elif action == "resume":
            self.server.engine.resume(session)
        elif action in {"human_control", "human"}:
            self.server.engine.enter_human_control(session)
        elif action in {"ai_control", "ai"}:
            self.server.engine.leave_human_control(session)
        elif action == "finish":
            session.state.stage = "finished"
            session.state.human_controlled = False
            session.state.paused = False
        else:
            raise ValueError("unknown control action")
        self.server.store.save_session(session)
        self.server.store.append_event(session.id, "control", {"action": action, "reason": reason})
        self._send({"session": self.server.store.load_session_record(session.id)})

    def _handle_session_event(self, session_id: str, body: dict[str, Any]) -> None:
        if not self.server.store.load_session(session_id):
            self._send({"error": "session not found"}, status=404)
            return
        event_type = str(body.get("event_type") or body.get("type") or "event")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        self.server.store.append_event(session_id, event_type, payload)
        state = body.get("state")
        if isinstance(state, dict):
            self.server.store.update_session_state(session_id, state)
        self._send({"ok": True})

    def _handle_agent_heartbeat(self, body: dict[str, Any]) -> None:
        agent_id = str(body.get("agent_id") or "")
        if not agent_id:
            raise ValueError("agent_id is required")
        self._send(
            self.server.store.heartbeat(
                agent_id,
                status=str(body.get("status") or "running"),
                current_session_id=str(body.get("current_session_id") or ""),
            )
        )

    def _handle_agent_claim(self, body: dict[str, Any]) -> None:
        agent_id = str(body.get("agent_id") or "")
        if not agent_id:
            raise ValueError("agent_id is required")
        sessions = self.server.store.claim_due_sessions(agent_id, limit=int(body.get("limit") or 1))
        self._send({"sessions": sessions})

    def _send_record_or_404(self, session_id: str) -> None:
        record = self.server.store.load_session_record(session_id)
        if not record:
            self._send({"error": "session not found"}, status=404)
            return
        self._send({"session": record})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = self.rfile.read(length).decode("utf-8")
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else {}

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def import_json_store(root: str | Path, sqlite_store: SQLiteRefereeStore) -> dict[str, int]:
    source = RefereeStore(root)
    counts = {"rulepacks": 0, "sessions": 0, "events": 0}
    for rulepack in source.list_rulepacks():
        sqlite_store.save_rulepack(rulepack)
        counts["rulepacks"] += 1
    for session in source.list_sessions():
        if not sqlite_store.load_rulepack(session.rulepack.id):
            sqlite_store.save_rulepack(session.rulepack)
        sqlite_store.save_session(session, assigned_agent_id=None)
        counts["sessions"] += 1
    logs_dir = Path(root) / "logs"
    if logs_dir.exists():
        for path in logs_dir.glob("*.jsonl"):
            session_id = path.stem
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sqlite_store.append_event(
                        session_id,
                        str(entry.get("type") or "imported"),
                        dict(entry.get("payload") or {}),
                        created_at=float(entry.get("time") or utcish_now()),
                    )
                    counts["events"] += 1
    return counts


def run_server(host: str = "127.0.0.1", port: int = 8765, db_path: str | Path = "referee.db") -> None:
    store = SQLiteRefereeStore(db_path)
    server = RefereeHTTPServer((host, port), store)
    print(f"Referee server listening on http://{host}:{port} (db: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()

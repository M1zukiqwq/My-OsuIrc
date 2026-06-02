"""HTTP client helpers for the local referee server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class RefereeApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class RefereeApiClient:
    def __init__(self, server_url: str = "http://127.0.0.1:8765", timeout: float = 10.0) -> None:
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def list_rulepacks(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/rulepacks").get("rulepacks", []))

    def draft_rulepack(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/rulepacks/draft", payload)

    def confirm_rulepack(self, rulepack_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/rulepacks/{rulepack_id}/confirm", {})

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/sessions").get("sessions", []))

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/sessions", payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/sessions/{session_id}")

    def control_session(self, session_id: str, action: str, reason: str = "") -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{session_id}/control", {"action": action, "reason": reason})

    def post_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"event_type": event_type, "payload": payload or {}}
        if state is not None:
            body["state"] = state
        return self._request("POST", f"/api/sessions/{session_id}/events", body)

    def heartbeat(self, agent_id: str, status: str = "running", current_session_id: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/agent/heartbeat",
            {"agent_id": agent_id, "status": status, "current_session_id": current_session_id},
        )

    def claim(self, agent_id: str, limit: int = 1) -> list[dict[str, Any]]:
        return list(
            self._request("POST", "/api/agent/claim", {"agent_id": agent_id, "limit": limit}).get("sessions", [])
        )

    def tasks(self, agent_id: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"/api/agent/{agent_id}/tasks").get("sessions", []))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.server_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            try:
                payload = json.loads(raw)
                message = str(payload.get("error") or raw)
            except json.JSONDecodeError:
                message = raw or exc.reason
            raise RefereeApiError(exc.code, message) from exc

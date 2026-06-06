"""Optional OpenAI-compatible helpers for rule and mappool extraction."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.json")


class OpenAICompatibleClient:
    """Small standard-library client for /v1/chat/completions APIs."""

    def __init__(self, base_url: str, api_key: str, model: str, extra_body: dict[str, Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_body = extra_body or {}

    @classmethod
    def from_env(cls) -> "OpenAICompatibleClient | None":
        config = load_ai_config()
        api_key = config.get("api_key", "").strip()
        if not api_key:
            return None
        base_url = config.get("base_url", "https://api.openai.com").strip()
        model = config.get("model", "gpt-4.1-mini").strip()
        extra_body = config.get("extra_body", {})
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            extra_body=extra_body if isinstance(extra_body, dict) else {},
        )

    def extract_rulepack(
        self,
        name: str,
        rules_url: str = "",
        mappool_url: str = "",
        rules_text: str = "",
        mappool_text: str = "",
    ) -> dict[str, Any]:
        """Parse a tournament rulebook / mappool into a structured draft.

        Text can be supplied directly (rules_text / mappool_text) — e.g. a local
        Markdown rulebook or a tab-separated mappool — or fetched from a URL.
        """
        rules_text = rules_text or (fetch_url_text(rules_url) if rules_url else "")
        mappool_text = mappool_text or (fetch_url_text(mappool_url) if mappool_url else "")
        prompt = (
            "Extract an osu! tournament referee rulepack as strict JSON from the rules "
            "and mappool text below (any input format). Return only one JSON object with "
            "optional keys: name, bp_timer, join_timer, ready_start_countdown, "
            "pick_ban_timer, prep_timer, tb_prep_timer, pause_timer, pause_per_player, "
            "abort_window, start_on_ready_settings_check, start_on_system_all_ready, "
            "start_on_join_timer_end, team_template, mappool, format, raw_rules.\n"
            "Each mappool item must be an object with keys: code (the pool label like "
            "NM1/HD2/DT3/TB), beatmap_id (integer), mods (e.g. 'NF', 'NF HR'), "
            "map_command (e.g. '!mp map 5223058 0'), mod_command (e.g. '!mp mods NF'), "
            "and title when available. Derive beatmap_id/map_command/mod_command from the "
            "mappool's map links or commands.\n"
            "Put these in `format`: team_mode ('TeamVs'/'HeadToHead'/...), win_condition "
            "('ScoreV2'/'Score'/...), and bp_order — an ordered list of the strings 'pick' "
            "and 'ban' describing the ban/pick template (e.g. ['pick','ban','pick','ban']); "
            "include one 'ban' entry per ban each side gets (0 if no bans).\n"
            "Do NOT put best_of anywhere — best-of is chosen per match when the room opens. "
            "Timers are seconds. If a field is not stated, omit it; do not invent values.\n\n"
            f"Rulepack name: {name}\n"
            f"Rules URL: {rules_url}\n"
            f"Mappool URL: {mappool_url}\n\n"
            f"Rules text:\n{rules_text[:30000]}\n\n"
            f"Mappool text:\n{mappool_text[:30000]}"
        )
        content = self.chat(prompt)
        return extract_json_object(content)

    def chat(self, prompt: str, system: str | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or "You extract structured osu! tournament referee rules."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        payload.update(self.extra_body)
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def load_ai_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load AI provider config from config.json, then apply env overrides."""
    config: dict[str, Any] = {
        "base_url": "https://api.openai.com",
        "api_key": "",
        "model": "gpt-4.1-mini",
        "extra_body": {},
    }
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            raw = {}
        ai_config = raw.get("ai", raw) if isinstance(raw, dict) else {}
        if isinstance(ai_config, dict):
            config.update(
                {
                    "base_url": str(ai_config.get("base_url") or ai_config.get("url") or config["base_url"]),
                    "api_key": str(ai_config.get("api_key") or ai_config.get("apikey") or config["api_key"]),
                    "model": str(ai_config.get("model") or config["model"]),
                    "extra_body": dict(ai_config.get("extra_body") or {}),
                }
            )
            thinking = ai_config.get("thinking")
            if isinstance(thinking, dict):
                config["extra_body"]["thinking"] = thinking

    env_overrides = {
        "base_url": os.environ.get("AI_BASE_URL"),
        "api_key": os.environ.get("AI_API_KEY"),
        "model": os.environ.get("AI_MODEL"),
    }
    for key, value in env_overrides.items():
        if value:
            config[key] = value
    return config


def fetch_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "My-OsuIrc-Referee/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("content-type", "")
            data = response.read(500_000)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return ""
    encoding = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type)
    if match:
        encoding = match.group(1)
    return data.decode(encoding, errors="replace")


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}

"""Local configuration management for the Mash CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _config_path() -> Path:
    return Path.home() / ".mash" / "cli.json"


@dataclass(frozen=True)
class CLIConfig:
    """Persisted Mash CLI connection settings."""

    api_base_url: str
    api_key: Optional[str] = None
    agent_id: Optional[str] = None


def load_config() -> Optional[CLIConfig]:
    """Load persisted CLI configuration if present."""
    path = _config_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    api_base_url = str(payload.get("api_base_url") or "").strip()
    if not api_base_url:
        return None
    api_key_value = payload.get("api_key")
    agent_id_value = payload.get("agent_id")
    return CLIConfig(
        api_base_url=api_base_url,
        api_key=str(api_key_value).strip() if isinstance(api_key_value, str) and api_key_value.strip() else None,
        agent_id=str(agent_id_value).strip() if isinstance(agent_id_value, str) and agent_id_value.strip() else None,
    )


def save_config(config: CLIConfig) -> Path:
    """Persist CLI configuration and return the written path."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_base_url": config.api_base_url,
        "api_key": config.api_key,
        "agent_id": config.agent_id,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path

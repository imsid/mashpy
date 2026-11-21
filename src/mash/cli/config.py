"""Local configuration management for the Mash CLI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _config_path() -> Path:
    return Path.home() / ".mash" / "cli.json"


@dataclass(frozen=True)
class CLIConfig:
    """Persisted Mash CLI connection settings.

    ``host_id`` is not part of the connection; ``mash compose`` pins the host
    composition it defines here. Requests and workflow runs act for ``app_id``
    under ``policy_id``.
    """

    api_base_url: str
    api_key: Optional[str] = None
    host_id: Optional[str] = None
    app_id: Optional[str] = None
    policy_id: Optional[str] = None


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
    return CLIConfig(
        api_base_url=api_base_url,
        api_key=_text(payload.get("api_key")),
        host_id=_text(payload.get("host_id")),
        app_id=_text(payload.get("app_id")),
        policy_id=_text(payload.get("policy_id")),
    )


def _text(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def save_config(config: CLIConfig) -> Path:
    """Persist CLI configuration and return the written path."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_base_url": config.api_base_url,
        "api_key": config.api_key,
        "host_id": config.host_id,
        "app_id": config.app_id,
        "policy_id": config.policy_id,
    }
    # The file holds the API key: only its owner may read it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2))
    return path

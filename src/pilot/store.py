"""The host config file — the source of truth for available hosts.

The deployment is a flat pool of agents with no built-in hosts. Compositions
live in `~/.pilot/hosts.json`: `pilot compose` writes them, `pilot hosts`
and `pilot browse` list them, and `pilot repl --host <id>` publishes them to
the deployment (idempotent PUTs) before connecting. On first use the file is
seeded with the `guide` composition so the store works out of the box.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

# Seeded into the config file on first use. Plain agent-id strings: this is
# user-editable configuration, not code.
DEFAULT_HOSTS: dict[str, dict[str, Any]] = {
    "guide": {
        "primary": "pilot",
        "subagents": [
            "cli-copilot",
            "api-copilot",
            "mcp-copilot",
            "runtime-copilot",
            "workflow-copilot",
            "admin-copilot",
        ],
        "workflows": ["pilot-quiz"],
    },
}


def hosts_file_path() -> Path:
    home = Path(os.environ.get("PILOT_HOME", "~/.pilot")).expanduser()
    return home / "hosts.json"


def _normalize(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "primary": str(entry.get("primary") or ""),
        "subagents": [str(a) for a in entry.get("subagents") or []],
        "workflows": [str(w) for w in entry.get("workflows") or []],
    }


def load_hosts() -> dict[str, dict[str, Any]]:
    """Read the host config; seed it with the defaults on first use."""
    path = hosts_file_path()
    if not path.exists():
        defaults = {
            host_id: _normalize(entry) for host_id, entry in DEFAULT_HOSTS.items()
        }
        save_hosts(defaults)
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(host_id): _normalize(entry)
        for host_id, entry in payload.items()
        if isinstance(entry, dict) and str(entry.get("primary") or "").strip()
    }


def save_hosts(hosts: dict[str, dict[str, Any]]) -> None:
    path = hosts_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(hosts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def record_host(
    host_id: str,
    *,
    primary: str,
    subagents: Iterable[str] = (),
    workflows: Iterable[str] = (),
) -> None:
    """Add or replace one composition in the config file."""
    hosts = load_hosts()
    hosts[host_id] = _normalize(
        {"primary": primary, "subagents": list(subagents), "workflows": list(workflows)}
    )
    save_hosts(hosts)


def publish_hosts(client: Any, renderer: Any | None = None) -> list[str]:
    """PUT every configured composition on the connected deployment.

    Returns the published host ids. A host that no longer validates (e.g. it
    names an agent the pool dropped) is warned about and skipped, never
    deleted: the user may be pointing at a different deployment.
    """
    published: list[str] = []
    for host_id, entry in sorted(load_hosts().items()):
        try:
            client.define_host(
                host_id,
                primary=entry["primary"],
                subagents=entry["subagents"],
                workflows=entry["workflows"],
            )
            published.append(host_id)
        except Exception as exc:
            if renderer is not None:
                renderer.warn(f"could not publish configured host '{host_id}': {exc}")
    return published

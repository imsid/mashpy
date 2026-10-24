"""Per-agent tokens derived from the host secret.

Each agent process gets a token the host derives from ``MASH_HOST_SECRET`` and
the agent id. The host checks a token by deriving it again, so it stores none,
and a restarted agent keeps working as long as the secret is unchanged. The same token authenticates host-to-agent calls and
agent-to-host calls.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Iterable

MASH_HOST_SECRET_ENV = "MASH_HOST_SECRET"


def resolve_host_secret(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or os.environ.get(MASH_HOST_SECRET_ENV) or "").strip()
    return value or None


def derive_agent_token(secret: str, agent_id: str) -> str:
    resolved_secret = str(secret or "").strip()
    if not resolved_secret:
        raise ValueError("host secret is required")
    resolved_agent_id = str(agent_id or "").strip()
    if not resolved_agent_id:
        raise ValueError("agent_id is required")
    return hmac.new(
        resolved_secret.encode(),
        f"mash-agent:{resolved_agent_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def agent_for_token(secret: str, token: str, agent_ids: Iterable[str]) -> str | None:
    """Return the agent whose derived token matches ``token``, or None."""
    supplied = str(token or "").strip()
    if not supplied:
        return None
    for agent_id in agent_ids:
        if hmac.compare_digest(derive_agent_token(secret, agent_id), supplied):
            return agent_id
    return None


__all__ = [
    "MASH_HOST_SECRET_ENV",
    "agent_for_token",
    "derive_agent_token",
    "resolve_host_secret",
]

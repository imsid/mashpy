"""Per-policy sandbox and link tokens derived from the host secret.

A policy's sandbox daemon accepts only ``HMAC(MASH_HOST_SECRET, "mash-sandbox:<policy_id>")``.
Only the host holds the secret, so only the host can run commands, and a token
for one policy opens no other policy's sandbox.

A link token names a policy in the URLs the host serves its sandbox at
(``/s/<link token>/...``). It carries the policy id and an HMAC of it, so the
host reads the policy back from the token and checks it without storing any.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def derive_sandbox_token(secret: str, policy_id: str) -> str:
    return _hmac(secret, "mash-sandbox", policy_id)


def derive_link_token(secret: str, policy_id: str) -> str:
    """``<base64url(policy_id)>.<hmac>``: opens the policy's sandbox links and nothing else."""
    signature = _hmac(secret, "mash-link", policy_id)[:32]
    encoded = base64.urlsafe_b64encode(str(policy_id).strip().encode()).decode().rstrip("=")
    return f"{encoded}.{signature}"


def policy_for_link_token(secret: str, token: str) -> str | None:
    """The policy a link token names, or None when it is not one the host made."""
    encoded, _, signature = str(token or "").partition(".")
    if not encoded or not signature:
        return None
    try:
        policy_id = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not policy_id.strip():
        return None
    if not hmac.compare_digest(derive_link_token(secret, policy_id), token):
        return None
    return policy_id


def _hmac(secret: str, purpose: str, policy_id: str) -> str:
    resolved_secret = str(secret or "").strip()
    if not resolved_secret:
        raise ValueError("host secret is required")
    resolved_policy_id = str(policy_id or "").strip()
    if not resolved_policy_id:
        raise ValueError("policy_id is required")
    return hmac.new(
        resolved_secret.encode(),
        f"{purpose}:{resolved_policy_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


__all__ = ["derive_link_token", "derive_sandbox_token", "policy_for_link_token"]

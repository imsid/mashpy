"""Browser and view tokens derived from the host secret.

A policy's browser daemon accepts only
``HMAC(MASH_HOST_SECRET, "mash-browser:<policy_id>")``, so only the host drives
it, and a token for one policy opens no other policy's browser.

A view token names a policy, a request and an expiry in the live-view link the
host hands the user during a sign-in (``/s/<view token>/browser/``). It carries
those values and an HMAC of them, so the host reads them back and checks them
without storing anything. The host also forgets a handoff's token when the
handoff ends; see ``mash.browser.manager``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time

from mash.sandbox.tokens import _hmac


def derive_browser_token(secret: str, policy_id: str) -> str:
    return _hmac(secret, "mash-browser", policy_id)


def make_view_token(secret: str, policy_id: str, request_id: str, expires_at: int) -> str:
    """``<base64url(claims)>.<hmac>``: opens the policy's live view until ``expires_at``."""
    claims = json.dumps(
        {"u": str(policy_id).strip(), "r": str(request_id), "e": int(expires_at)},
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = base64.urlsafe_b64encode(claims.encode()).decode().rstrip("=")
    return f"{encoded}.{_hmac(secret, 'mash-view', encoded)[:32]}"


def read_view_token(
    secret: str, token: str, *, now: float | None = None
) -> tuple[str, str, int] | None:
    """``(policy_id, request_id, expires_at)`` of a live view token, or None.

    None when the host did not make it or it has expired.
    """
    encoded, _, signature = str(token or "").partition(".")
    if not encoded or not signature:
        return None
    if not hmac.compare_digest(_hmac(secret, "mash-view", encoded)[:32], signature):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        policy_id, request_id, expires_at = str(claims["u"]), str(claims["r"]), int(claims["e"])
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
    if not policy_id or expires_at <= (time.time() if now is None else now):
        return None
    return policy_id, request_id, expires_at


__all__ = ["derive_browser_token", "make_view_token", "read_view_token"]

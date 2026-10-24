"""Per-user sandbox and link tokens derived from the host secret.

A user's sandbox daemon accepts only ``HMAC(MASH_HOST_SECRET, "mash-sandbox:<user_id>")``.
Only the host holds the secret, so only the host can run commands, and a token
for one user opens no other user's sandbox.

A link token names a user in the URLs the host serves their sandbox at
(``/s/<link token>/...``). It carries the user id and an HMAC of it, so the
host reads the user back from the token and checks it without storing any.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def derive_sandbox_token(secret: str, user_id: str) -> str:
    return _hmac(secret, "mash-sandbox", user_id)


def derive_link_token(secret: str, user_id: str) -> str:
    """``<base64url(user_id)>.<hmac>``: opens the user's sandbox links and nothing else."""
    signature = _hmac(secret, "mash-link", user_id)[:32]
    encoded = base64.urlsafe_b64encode(str(user_id).strip().encode()).decode().rstrip("=")
    return f"{encoded}.{signature}"


def user_for_link_token(secret: str, token: str) -> str | None:
    """The user a link token names, or None when it is not one the host made."""
    encoded, _, signature = str(token or "").partition(".")
    if not encoded or not signature:
        return None
    try:
        user_id = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not user_id.strip():
        return None
    if not hmac.compare_digest(derive_link_token(secret, user_id), token):
        return None
    return user_id


def _hmac(secret: str, purpose: str, user_id: str) -> str:
    resolved_secret = str(secret or "").strip()
    if not resolved_secret:
        raise ValueError("host secret is required")
    resolved_user_id = str(user_id or "").strip()
    if not resolved_user_id:
        raise ValueError("user_id is required")
    return hmac.new(
        resolved_secret.encode(),
        f"{purpose}:{resolved_user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


__all__ = ["derive_link_token", "derive_sandbox_token", "user_for_link_token"]

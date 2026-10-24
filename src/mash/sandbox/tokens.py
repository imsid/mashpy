"""Per-user sandbox tokens derived from the host secret.

A user's sandbox daemon accepts only ``HMAC(MASH_HOST_SECRET, "mash-sandbox:<user_id>")``.
Only the host holds the secret, so only the host can run commands, and a token
for one user opens no other user's sandbox.
"""

from __future__ import annotations

import hashlib
import hmac


def derive_sandbox_token(secret: str, user_id: str) -> str:
    resolved_secret = str(secret or "").strip()
    if not resolved_secret:
        raise ValueError("host secret is required")
    resolved_user_id = str(user_id or "").strip()
    if not resolved_user_id:
        raise ValueError("user_id is required")
    return hmac.new(
        resolved_secret.encode(),
        f"mash-sandbox:{resolved_user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


__all__ = ["derive_sandbox_token"]

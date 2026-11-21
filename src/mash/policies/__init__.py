"""Policies: named, durable resources that sessions share.

A policy owns the sandbox and the browser profile, and sets the sandbox,
browser and wait rules for every request under it.
"""

from .models import (
    DEFAULT_WAIT_MAX_SECONDS,
    MIN_WAIT_MAX_SECONDS,
    Policy,
    PolicyNotFoundError,
    PolicyRecord,
    validate_policy_id,
)
from .store import PolicyStore

__all__ = [
    "DEFAULT_WAIT_MAX_SECONDS",
    "MIN_WAIT_MAX_SECONDS",
    "Policy",
    "PolicyNotFoundError",
    "PolicyRecord",
    "PolicyStore",
    "validate_policy_id",
]

"""The companion: a built-in primary that routes user messages to pool agents and workflows."""

from .spec import COMPANION_AGENT_ID, CompanionSpec, build_companion_metadata

__all__ = ["COMPANION_AGENT_ID", "CompanionSpec", "build_companion_metadata"]

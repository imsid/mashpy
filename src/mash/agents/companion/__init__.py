"""The companion: the built-in router of every host request, to the host's agents and workflows."""

from .spec import COMPANION_AGENT_ID, CompanionSpec, build_companion_metadata

__all__ = ["COMPANION_AGENT_ID", "CompanionSpec", "build_companion_metadata"]

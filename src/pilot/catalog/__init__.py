"""The Pilot catalog: every pooled agent the store ships, with its listing.

Each entry pairs an agent factory with the `AgentMetadata` that becomes its
store listing (and, when the agent serves as a subagent, the delegation
directory the primary reads). Adding an agent to the store is adding a
package under `agents/` and one entry to `CATALOG`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mash import AgentMetadata, AgentSpec

from .agents import builder, pilot


@dataclass(frozen=True)
class CatalogEntry:
    """One store listing: an agent id, its spec factory, and its metadata."""

    agent_id: str
    create_spec: Callable[[], AgentSpec]
    build_metadata: Callable[[], AgentMetadata]


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(pilot.PILOT_AGENT_ID, pilot.PilotSpec, pilot.build_metadata),
    CatalogEntry(builder.BUILDER_AGENT_ID, builder.BuilderSpec, builder.build_metadata),
)

__all__ = ["CATALOG", "CatalogEntry"]

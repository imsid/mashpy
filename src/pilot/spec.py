"""Pilot pool assembly: register the catalog as a flat pool.

Spec classes live in their catalog sub-packages; this module wires them into a
Pool and defines the default ``guide`` host. The mashpy checkout the agents
read is mounted at ``REPO_ROOT`` by the deployment (docker-compose.pilot.yml).
"""

from __future__ import annotations

from mash import Host, HostBuilder, Pool

from .catalog import CATALOG
from .catalog.agents.builder import BUILDER_AGENT_ID
from .catalog.agents.pilot import PILOT_AGENT_ID
from .catalog.workflows.changelog import (
    CHANGELOG_WORKFLOW_ID,
    build_changelog_workflow_spec,
)

GUIDE_HOST_ID = "guide"


def build_pool() -> Pool:
    """Build the Pilot pool: the guide, the builder, and the changelog workflow."""
    builder = HostBuilder()
    for entry in CATALOG:
        builder.agent(entry.create_spec(), metadata=entry.build_metadata())
    builder.workflow(build_changelog_workflow_spec())
    builder.host(
        Host(
            host_id=GUIDE_HOST_ID,
            primary=PILOT_AGENT_ID,
            subagents=(BUILDER_AGENT_ID,),
            workflows=(CHANGELOG_WORKFLOW_ID,),
        )
    )
    return builder.build()

"""Pilot pool assembly: register the catalog as a flat pool.

Spec classes live in their catalog sub-packages; this module wires them into
an AgentPool, defines the default ``guide`` host, and re-exports the names
the test suite imports from ``pilot.spec``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from mash.api import MashHostConfig, run_host
from mash.runtime import AgentPool, HostBuilder
from mash.runtime.host.types import Host

from .catalog.workflows.quiz import build_quiz_workflow_spec

from .catalog import CATALOG
from .catalog._base import (
    APP_NAME,  # noqa: F401 — re-exported for tests
    _cached_docs_for_scope,
)
from .catalog.agents.admin import ADMIN_COPILOT_AGENT_ID
from .catalog.agents.admin.spec import AdminCopilotSpec
from .catalog.agents.api import API_COPILOT_AGENT_ID
from .catalog.agents.api.spec import ApiCopilotSpec
from .catalog.agents.cli import CLI_COPILOT_AGENT_ID
from .catalog.agents.cli.spec import CliCopilotSpec
from .catalog.agents.mcp import MCP_COPILOT_AGENT_ID
from .catalog.agents.mcp.spec import McpCopilotSpec
from .catalog.agents.pilot import PILOT_AGENT_ID
from .catalog.agents.pilot.spec import PilotSpec
from .catalog.agents.runtime import RUNTIME_COPILOT_AGENT_ID
from .catalog.agents.runtime.spec import RuntimeCopilotSpec
from .catalog.agents.workflow import WORKFLOW_COPILOT_AGENT_ID
from .catalog.agents.workflow.spec import WorkflowCopilotSpec

__all__ = [
    "ADMIN_COPILOT_AGENT_ID",
    "AdminCopilotSpec",
    "APP_NAME",
    "API_COPILOT_AGENT_ID",
    "ApiCopilotSpec",
    "CLI_COPILOT_AGENT_ID",
    "CliCopilotSpec",
    "MCP_COPILOT_AGENT_ID",
    "McpCopilotSpec",
    "PILOT_AGENT_ID",
    "PilotSpec",
    "RUNTIME_COPILOT_AGENT_ID",
    "RuntimeCopilotSpec",
    "WORKFLOW_COPILOT_AGENT_ID",
    "WorkflowCopilotSpec",
    "_cached_docs_for_scope",
]


def build_pool(workspace_root: Path | None = None) -> AgentPool:
    """Build the Pilot agent pool from the catalog.

    Registers all catalog agents and defines a default ``guide`` host that
    composes the pilot primary with its five module copilots. Dynamic hosts
    can be defined over the API at runtime.
    """
    _repo_root = Path(__file__).resolve().parents[2]
    resolved = (
        workspace_root
        or Path(os.environ.get("PILOT_WORKSPACE_ROOT", str(_repo_root)))
    ).resolve()
    ws = str(resolved)

    builder = HostBuilder()
    for entry in CATALOG:
        builder.agent(
            entry.create_spec(workspace_root=ws), metadata=entry.build_metadata()
        )
    builder.host(
        Host(
            host_id="guide",
            primary=PILOT_AGENT_ID,
            subagents=(
                API_COPILOT_AGENT_ID,
                CLI_COPILOT_AGENT_ID,
                MCP_COPILOT_AGENT_ID,
                RUNTIME_COPILOT_AGENT_ID,
                WORKFLOW_COPILOT_AGENT_ID,
                ADMIN_COPILOT_AGENT_ID,
            ),
        )
    )
    builder.enable_masher()
    pool = builder.build()
    pool.register_workflow(build_quiz_workflow_spec(resolved))
    return pool


# Back-compat alias: existing deployments may point MASH_HOST_APP at
# `pilot.spec:build_host`. Drop once confirmed on `build_pool`.
build_host = build_pool


def serve(
    *,
    workspace_root: str = ".",
    bind_host: str = "127.0.0.1",
    bind_port: int = 8000,
    api_key: str | None = None,
) -> int:
    """Run the Pilot host API over the pool. Blocks until shutdown."""
    run_host(
        build_pool(Path(workspace_root).resolve()),
        config=MashHostConfig(
            bind_host=bind_host,
            bind_port=bind_port,
            api_key=api_key,
        ),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Mash Pilot host over the Mash host API."
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace folder exposed to the Mash pilot subagents.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API bind host.")
    parser.add_argument("--port", type=int, default=8000, help="API bind port.")
    parser.add_argument("--api-key", default=None, help="Optional API key.")
    args = parser.parse_args(argv)

    return serve(
        workspace_root=args.workspace_root,
        bind_host=args.host,
        bind_port=args.port,
        api_key=args.api_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())

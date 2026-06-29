"""Workflow copilot agent spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mash.core.config import AgentConfig
from mash.runtime import AgentMetadata
from mash.tools.registry import ToolRegistry

from ..._base import CopilotAgentSpec, build_bash_tool

WORKFLOW_COPILOT_AGENT_ID = "workflow-copilot"
WORKFLOW_DOC_ROOTS = ("src/mash/workflows",)


class WorkflowCopilotSpec(CopilotAgentSpec):
    """Subagent specialized in Mash workflow orchestration."""

    def get_agent_id(self) -> str:
        return WORKFLOW_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return self._build_copilot_prompt_blocks(
            scope="src/mash/workflows",
            doc_roots=WORKFLOW_DOC_ROOTS,
            cache_label="workflow",
        )

    def build_agent_config(self) -> AgentConfig:
        return self._build_copilot_config(WORKFLOW_COPILOT_AGENT_ID)


def build_workflow_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Mash Workflow Copilot",
        description=(
            "Specialist for Mash host-level workflow specs, registry, DBOS-backed "
            "workflow orchestration, run status, deduplication, and task state handoff."
        ),
        capabilities=[
            "src/mash/workflows",
            "workflow registry",
            "workflow service",
            "DBOS workflow orchestration",
            "workflow run status",
            "task state handoff",
        ],
        usage_guidance=(
            "Use for questions centered on code-defined workflows, workflow "
            "registration, DBOS-backed run orchestration, workflow task state, "
            "or other behavior implemented under `src/mash/workflows`."
        ),
    )


def create_workflow_copilot_spec(*, workspace_root: str) -> WorkflowCopilotSpec:
    return WorkflowCopilotSpec(Path(workspace_root).resolve())

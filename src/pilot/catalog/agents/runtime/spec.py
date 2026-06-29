"""Runtime copilot agent spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mash.core.config import AgentConfig
from mash.runtime import AgentMetadata
from mash.tools.registry import ToolRegistry

from ..._base import CopilotAgentSpec, build_bash_tool

RUNTIME_COPILOT_AGENT_ID = "runtime-copilot"
RUNTIME_DOC_ROOTS = ("src/mash/runtime",)


class RuntimeCopilotSpec(CopilotAgentSpec):
    """Subagent specialized in Mash runtime hosting and durability."""

    def get_agent_id(self) -> str:
        return RUNTIME_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return self._build_copilot_prompt_blocks(
            scope="src/mash/runtime",
            doc_roots=RUNTIME_DOC_ROOTS,
            cache_label="runtime",
        )

    def build_agent_config(self) -> AgentConfig:
        return self._build_copilot_config(RUNTIME_COPILOT_AGENT_ID)


def build_runtime_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Mash Runtime Copilot",
        description=(
            "Specialist for Mash runtime hosting, request handling, event sourcing, "
            "span-based trace analysis, observability data model, "
            "durable workflow execution, and subagent/runtime integration."
        ),
        capabilities=[
            "src/mash/runtime",
            "agent runtime",
            "host composition",
            "request handling",
            "event sourcing",
            "durable workflow execution",
            "subagent runtime integration",
            "span trees",
            "SpanKind",
            "TraceSpanTree",
            "TraceAnalysis",
            "trace analysis",
            "timing breakdown",
            "observability data model",
            "tool call stats",
            "step breakdown",
            "subagent trace stitching",
        ],
        usage_guidance=(
            "Use for questions centered on AgentRuntime behavior, runtime host "
            "composition, request lifecycle, event replay, workflow durability, "
            "span-based trace analysis (spans, span trees, TraceAnalysis, timing breakdowns, "
            "tool stats, step breakdowns, subagent trace stitching), "
            "or other behavior implemented under `src/mash/runtime`."
        ),
    )


def create_runtime_copilot_spec(*, workspace_root: str) -> RuntimeCopilotSpec:
    return RuntimeCopilotSpec(Path(workspace_root).resolve())

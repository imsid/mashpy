"""Admin UI copilot agent spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mash.core.config import AgentConfig
from mash.runtime import AgentMetadata
from mash.tools.registry import ToolRegistry

from ..._base import CopilotAgentSpec, build_bash_tool

ADMIN_COPILOT_AGENT_ID = "admin-copilot"
ADMIN_DOC_ROOTS = ("src/mash/api/web-admin",)


class AdminCopilotSpec(CopilotAgentSpec):
    """Subagent specialized in the Mash admin dashboard (the web-admin SPA)."""

    def get_agent_id(self) -> str:
        return ADMIN_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return self._build_copilot_prompt_blocks(
            scope="src/mash/api/web-admin",
            doc_roots=ADMIN_DOC_ROOTS,
            cache_label="Admin UI",
            extra_rules=(
                "You own the admin dashboard front end: tabs, components, what each tab "
                "surfaces or tracks, and how the SPA wires to API routes via "
                "`src/lib/api.js`.",
                "For 'is X tracked/visible in the admin UI', 'what does X mean in the UI', "
                "or 'which tab shows X', answer from the tab map in the cached README "
                "before reaching for bash.",
                "Defer HTTP route internals (request/response shape, route handlers) to the "
                "api surface and the telemetry data model to the runtime layer; name the "
                "endpoint a tab calls, but do not re-derive its implementation.",
                "Prefer a single `rg` or one small `sed` read over repeated full-file dumps.",
            ),
        )

    def build_agent_config(self) -> AgentConfig:
        return self._build_copilot_config(ADMIN_COPILOT_AGENT_ID)


def build_admin_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Mash Admin UI Copilot",
        description=(
            "Specialist for the Mash admin dashboard: the web-admin SPA, its tabs, "
            "what each surfaces or tracks, and how the front end wires to the host API."
        ),
        capabilities=[
            "src/mash/api/web-admin",
            "admin dashboard",
            "admin UI tabs",
            "overview / agents / tools / skills / workflows tabs",
            "hosts / logs / feedback / reference tabs",
            "what is tracked in the admin UI",
            "tab to endpoint mapping",
            "web-admin components and lib/api.js",
        ],
        usage_guidance=(
            "Use for questions about the admin dashboard UI: whether something is "
            "tracked or visible in it, what a tab or field means, which tab shows a "
            "given thing, or which API endpoint feeds a tab. Route HTTP endpoint "
            "internals to api-copilot and telemetry data-model questions to "
            "runtime-copilot."
        ),
    )


def create_admin_copilot_spec(*, workspace_root: str) -> AdminCopilotSpec:
    return AdminCopilotSpec(Path(workspace_root).resolve())

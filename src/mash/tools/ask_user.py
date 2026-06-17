"""AskUser tool for agent-initiated user interaction."""

from __future__ import annotations

from typing import Any, Dict

from .base import ToolResult

ASK_USER_DEFAULT_TIMEOUT_SECONDS = 3600


class AskUserTool:
    """Built-in tool that lets the agent ask the user a question.

    When called in a hosted runtime, the workflow intercepts this tool and
    triggers a durable interaction (info or choice). The user's response
    is returned as the tool result.
    """

    name = "AskUser"
    requires_approval = False
    # Suspends the turn waiting on a user reply; never run concurrently.
    parallel_safe = False
    description = (
        "Ask the user a question and wait for their response. "
        "Use when you need clarification, a decision, or information "
        "only the user can provide."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of options for the user to choose from. "
                    "If provided, the user selects one or more. "
                    "If omitted, the user provides free-form text."
                ),
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        return ToolResult.error(
            "AskUser requires a hosted runtime with interaction support."
        )

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

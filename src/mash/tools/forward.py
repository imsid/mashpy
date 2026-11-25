"""ForwardAnswer: pass one agent's answer to the user unchanged.

The companion calls it after ``RunSubAgents`` returns and it has read the
results, when one child's answer answers the user's message as asked. The
request loop handles the call (``RequestRun._forward_answer``): it checks the
child in the request's ``RunSubAgents`` results and ends the request with the
child's response as the reply, with no further model call. The tool itself
only carries the definition the model sees.
"""

from __future__ import annotations

from typing import Any, Dict

from .base import ToolResult

FORWARD_ANSWER = "ForwardAnswer"


class ForwardAnswerTool:
    name = FORWARD_ANSWER
    requires_approval = False
    # The loop runs it alone: it must be the only call in its step.
    parallel_safe = False
    description = (
        "Send one agent's answer to the user unchanged, as your reply, and end "
        "the turn. Call it alone, after RunSubAgents has returned, with the "
        "request_id of a completed entry whose response answers the user's "
        "message as asked. Do not call it for a failed, cancelled or timed-out "
        "entry, or when several entries need combining: write your own reply "
        "then."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "The request_id of a RunSubAgents entry from this turn.",
            }
        },
        "required": ["request_id"],
    }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        del args
        return ToolResult.error(
            "ForwardAnswer is handled by the request loop and cannot run on its own"
        )

    def to_llm_format(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


__all__ = ["FORWARD_ANSWER", "ForwardAnswerTool"]

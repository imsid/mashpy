"""RunWorkflow: start one of the host's workflows and wait for its result."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional, Protocol

from ..logging import get_request_id, get_subagent_id_prefix
from ..runtime.client import WorkflowInputError
from ..runtime.errors import classify_error
from .base import ToolResult

DEFAULT_TIMEOUT_SECONDS = 360
MAX_TIMEOUT_SECONDS = 3600
POLL_SECONDS = 2.0
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class SupportsWorkflowRelay(Protocol):
    """The host relay calls RunWorkflow makes."""

    async def post_workflow_run(
        self,
        workflow_id: str,
        *,
        parent_request_id: str,
        workflow_input: Dict[str, Any],
        dedup_key: Optional[str],
    ) -> Dict[str, Any]:
        """Start a run, or return the run a duplicate ``dedup_key`` already started."""

    async def get_workflow_run(
        self, workflow_id: str, run_id: str, *, parent_request_id: str
    ) -> Dict[str, Any]:
        """The run's ``status``, ``result`` and ``error``."""


class RunWorkflowTool:
    """Start one workflow run through the host relay and poll it to the end."""

    name = "RunWorkflow"
    requires_approval = False
    # Several runs in one turn run at once.
    parallel_safe = True
    description = (
        "Run one of the host's workflows (listed under WORKFLOWS) with the "
        "given input, and wait for it. Returns the run_id and its status "
        "(completed, failed, cancelled or timed_out) with its result or error. "
        "An input the workflow refuses comes back as an error naming the "
        "fields to fix. On timed_out the run keeps going."
    )
    parameters = {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "input": {
                "type": "object",
                "description": "The workflow's input, matching its input schema.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    f"Defaults to {DEFAULT_TIMEOUT_SECONDS}, at most {MAX_TIMEOUT_SECONDS}."
                ),
                "maximum": MAX_TIMEOUT_SECONDS,
            },
        },
        "required": ["workflow_id", "input"],
    }

    def __init__(
        self,
        *,
        relay: SupportsWorkflowRelay,
        workflow_ids: list[str],
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._relay = relay
        self._workflow_ids = list(workflow_ids)
        self._poll_seconds = poll_seconds

    @staticmethod
    def _parse(args: Dict[str, Any]) -> tuple[str, Dict[str, Any], int] | str:
        workflow_id = str(args.get("workflow_id") or "").strip()
        if not workflow_id:
            return "workflow_id is required"
        workflow_input = args.get("input")
        if workflow_input is None:
            workflow_input = {}
        if not isinstance(workflow_input, dict):
            return "input must be an object"
        timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout, bool):
            return "timeout_seconds must be a positive integer"
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return "timeout_seconds must be a positive integer"
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            return f"timeout_seconds must be a positive integer, at most {MAX_TIMEOUT_SECONDS}"
        return workflow_id, workflow_input, timeout

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        parsed = self._parse(args)
        if isinstance(parsed, str):
            return ToolResult.error(parsed)
        workflow_id, workflow_input, timeout_seconds = parsed
        if workflow_id not in self._workflow_ids:
            return ToolResult.error(
                f"workflow '{workflow_id}' is not one of this host's workflows: "
                f"{', '.join(self._workflow_ids)}"
            )
        parent_request_id = get_request_id() or ""
        # The durable loop binds a key fixed by this call's position, so a
        # replayed call gets back the run it already started.
        dedup_key = get_subagent_id_prefix()
        started_at = time.monotonic()
        try:
            run = await self._relay.post_workflow_run(
                workflow_id,
                parent_request_id=parent_request_id,
                workflow_input=workflow_input,
                dedup_key=dedup_key,
            )
        except WorkflowInputError as exc:
            return ToolResult.error(
                "the workflow refused the input; fix these fields and call again: "
                + json.dumps(exc.errors, ensure_ascii=True, default=str)
            )
        except Exception as exc:
            return ToolResult.error(f"could not start workflow '{workflow_id}': {exc}")
        run_id = str(run.get("run_id") or "")
        deadline = started_at + timeout_seconds
        while str(run.get("status") or "") not in TERMINAL_STATUSES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._result(
                    workflow_id,
                    {
                        "run_id": run_id,
                        "status": "timed_out",
                        "error": "the run did not finish before timeout_seconds; it keeps going",
                    },
                    started_at,
                )
            await asyncio.sleep(min(self._poll_seconds, remaining))
            try:
                run = await self._relay.get_workflow_run(
                    workflow_id, run_id, parent_request_id=parent_request_id
                )
            except Exception as exc:
                return self._result(
                    workflow_id,
                    {"run_id": run_id, "status": "failed", **classify_error(exc)},
                    started_at,
                )
        status = str(run.get("status"))
        payload: Dict[str, Any] = {"run_id": run_id, "status": status}
        if status == "completed":
            payload["result"] = run.get("result")
        else:
            payload["error"] = run.get("error") or f"the run ended {status}"
        return self._result(workflow_id, payload, started_at)

    @staticmethod
    def _result(workflow_id: str, payload: Dict[str, Any], started_at: float) -> ToolResult:
        return ToolResult.success(
            json.dumps(payload, ensure_ascii=True, default=str),
            workflow=workflow_id,
            run_id=payload.get("run_id"),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


__all__ = ["RunWorkflowTool", "SupportsWorkflowRelay", "WorkflowInputError"]

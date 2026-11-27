"""The companion's request: route one user message, then stop.

The companion runs no agent loop. For each message its request:

1. compacts the board when its last entry reached the companion's threshold,
   and records the board position it read (``mash.context_load.end``)
2. classifies the message with one structured output call whose only field
   is an enum of the host's agent ids, workflow ids, ``about`` and
   ``unclear`` (``chat.end`` 0, with the choice)
3. acts on the choice:
   - an agent: starts that agent's request in this session, with an id
     derived from this request's and the choice in its ``parent.route``, and
     ends with ``routed`` (agent and request) in its response metadata. The
     host follows the routed request for the event.
   - a workflow: fills the input with a second structured output call
     (``chat.end`` 1), asks the user for required fields it could not fill,
     runs the workflow standalone and waits for it, writes one board entry
     with the run's input and outcome, and ends with the outcome as its reply
   - ``about``: ends with the host's agent and workflow descriptions
   - ``unclear``: asks the user to pick an agent, then routes to it

Each step records its output as events with dedupe keys and each question's
id is derived from its place in the request, so a recovered request reads its
choice, its answers and its workflow run back instead of repeating them. The
companion writes no board entry for an agent route, ``about`` or ``unclear``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

from ...core.llm.types import LLMContentBlock, LLMMessage, LLMRequest
from ...logging import bound_trace_id
from ...memory.board import render_board
from ...memory.store import WORKFLOW_RESULT
from ...otel import semconv
from ...runtime.client import WorkflowInputError
from ...tools.ask_user import ASK_USER_DEFAULT_TIMEOUT_SECONDS
from .. import context as context_helpers
from ..errors import retry_transient
from ..events import RuntimeEvent, RuntimeEventType
from ..events.types import EndStatus
from ..requests import app_id_from_request_metadata, append_runtime_event
from .steps import complete_request, start_request_trace

if TYPE_CHECKING:
    from .loop import RequestRun

# A routed request's id is derived from the companion's, so a recovered
# companion request resubmits the same id and gets back the request it started.
ROUTE_NAMESPACE = uuid.UUID("9d3e7b21-6a4f-4c58-b0e2-7f1a3c9d5e86")
WORKFLOW_POLL_SECONDS = 2.0
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


def routed_request_id(request_id: str) -> str:
    """The id of the agent request the companion's ``request_id`` routes to."""
    return str(uuid.uuid5(ROUTE_NAMESPACE, f"{request_id}:route"))


def _recorded(events: list[RuntimeEvent], dedupe_key: str) -> RuntimeEvent | None:
    return next((e for e in events if e.dedupe_key == dedupe_key), None)


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", None) or 0)
    output_tokens = int(getattr(usage, "output_tokens", None) or 0)
    context = getattr(usage, "context_tokens", None)
    return {
        "input": input_tokens,
        "output": output_tokens,
        "context": int(context) if context is not None else input_tokens + output_tokens,
    }


class Router:
    """One companion request's route, over its ``RequestRun``."""

    def __init__(self, run: "RequestRun") -> None:
        from mash.agents.companion import COMPANION_AGENT_ID

        self.run = run
        self.runtime = run.runtime
        self.metadata = run.request_metadata
        host = self.metadata.get("host") or {}
        self.host = dict(host) if isinstance(host, dict) else {}
        self.agent_ids = [str(a) for a in self.host.get("agents") or []]
        self.workflow_ids = [str(w) for w in self.host.get("workflows") or []]
        self.access = self.runtime.get_host_access()
        self.companion_id = COMPANION_AGENT_ID
        self.events: list[RuntimeEvent] = []

    # --- events ----------------------------------------------------------------

    async def _append(self, event_type: str, dedupe_key: str, payload: dict[str, Any], **kw: Any) -> RuntimeEvent:
        run = self.run
        return await append_runtime_event(
            self.runtime,
            RuntimeEvent(
                request_id=run.request_id,
                agent_id=self.runtime.agent_id,
                trace_id=run.trace_id,
                session_id=run.session_id,
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload=payload,
                **kw,
            ),
        )

    async def _complete(self, text: str, metadata: dict[str, Any] | None = None) -> None:
        run = self.run
        await complete_request(
            self.runtime,
            run.request_id,
            run.session_id,
            str(run.trace_id),
            {
                "trace_id": run.trace_id,
                "response": {"text": text, "signals": {}, "metadata": dict(metadata or {})},
            },
        )

    # --- model calls -------------------------------------------------------------

    async def _structured_call(
        self, *, loop_index: int, system: str, text: str, schema: dict[str, Any], key: str
    ) -> dict[str, Any]:
        """One structured output call, recorded on ``chat.end``; its decoded object.

        A recovered request reads the recorded output instead of calling again.
        """
        done = _recorded(self.events, f"route.{key}.completed")
        if done is not None:
            return dict((done.payload or {}).get("output") or {})
        llm = self.runtime.agent.llm
        config = self.runtime.agent.config
        attributes = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_CHAT,
            semconv.GEN_AI_PROVIDER_NAME: str(getattr(llm, "provider_name", "unknown")),
            semconv.GEN_AI_REQUEST_MODEL: str(llm.model),
        }
        await self._append(
            RuntimeEventType.CHAT_START.value,
            f"route.{key}.started",
            {"attributes": attributes},
            loop_index=loop_index,
            step_key=f"chat.start.{loop_index}",
        )
        request = LLMRequest(
            model=llm.model,
            system=system,
            messages=[LLMMessage(role="user", content=[LLMContentBlock.text(text)])],
            tools=[],
            max_tokens=config.max_tokens,
            temperature=0.0,
            provider_options={**config.provider_options, "structured_output": schema},
        )
        response = await retry_transient(lambda: llm.send(request))
        if response.stop_reason in {"max_tokens", "refusal", "error"}:
            raise RuntimeError(f"the companion's {key} call stopped: {response.stop_reason}")
        try:
            output = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"the companion's {key} call returned invalid JSON: {exc.msg}") from exc
        if not isinstance(output, dict):
            raise RuntimeError(f"the companion's {key} call must return a JSON object")
        usage = _usage(response)
        await self._append(
            RuntimeEventType.CHAT_END.value,
            f"route.{key}.completed",
            {
                "status": EndStatus.OK.value,
                "output": output,
                "context_tokens": usage["context"],
                "attributes": {**attributes, **semconv.usage_attributes(usage)},
            },
            loop_index=loop_index,
            step_key=f"chat.end.{loop_index}",
        )
        return output

    def _context_tokens(self) -> int:
        """The size of the request's last model call: a workflow entry's tokens."""
        ends = [
            e for e in self.events if e.event_type == RuntimeEventType.CHAT_END.value
        ]
        if not ends:
            return 0
        last = max(ends, key=lambda e: int(e.loop_index or 0))
        return int((last.payload or {}).get("context_tokens") or 0)

    # --- the route ---------------------------------------------------------------

    async def _load_board(self) -> list[dict[str, Any]]:
        """Compact the board if it is full, and record the position read."""
        run = self.run
        store = self.runtime.store
        loaded = _recorded(self.events, "context.loaded")
        if loaded is not None:
            up_to = int(((loaded.payload or {}).get("board") or {}).get("up_to") or 0)
            return await store.read_board(run.session_id, up_to=up_to)
        await context_helpers.compact_board_if_full(self.runtime, run.session_id, run.request_id)
        entries = await store.read_board(run.session_id)
        up_to = max((int(e["seq"]) for e in entries), default=0)
        await self._append(
            RuntimeEventType.CONTEXT_LOAD_END.value,
            "context.loaded",
            {"status": EndStatus.OK.value, "board": {"up_to": up_to}},
        )
        return entries

    def _agents(self) -> list[tuple[str, Any]]:
        return [(a, self.access.get_agent_metadata(a)) for a in self.agent_ids]

    def _workflows(self) -> list[Any]:
        get_workflow = getattr(self.access, "get_workflow", None)
        workflows = []
        for workflow_id in self.workflow_ids:
            workflow = get_workflow(workflow_id) if get_workflow is not None else None
            if workflow is None:
                raise RuntimeError(f"workflow '{workflow_id}' is not registered in this process")
            workflows.append(workflow)
        return workflows

    async def route(self) -> None:
        from mash.agents.companion.classifier import (
            ABOUT,
            CLASSIFIER_PROMPT,
            UNCLEAR,
            about_text,
            classifier_input,
            classifier_schema,
            route_choices,
            unclear_prompt,
        )

        run = self.run
        if self.access is None or not self.agent_ids:
            raise RuntimeError("the companion routes only host requests, to the host's agents")
        self.events = await self.runtime.runtime_store.list_request_events(run.request_id)
        entries = await self._load_board()
        board = render_board(entries)
        agents = self._agents()
        workflows = self._workflows()
        choices = route_choices(self.agent_ids, self.workflow_ids)
        output = await self._structured_call(
            loop_index=0,
            system=CLASSIFIER_PROMPT,
            text=classifier_input(agents=agents, workflows=workflows, board=board, message=run.message),
            schema=classifier_schema(choices),
            key="classify",
        )
        self.events = await self.runtime.runtime_store.list_request_events(run.request_id)
        choice = str(output.get("route") or "")
        if choice not in choices:
            raise RuntimeError(f"the classifier answered '{choice}', which is not a route")
        picked_by = "classifier"
        if choice == UNCLEAR:
            _, response, timed_out = await run._run_interaction(
                loop_index=0,
                interaction_type="choice",
                prompt=unclear_prompt(),
                options=list(self.agent_ids),
                timeout_seconds=ASK_USER_DEFAULT_TIMEOUT_SECONDS,
                step_prefix="route.pick",
            )
            picked = [str(item) for item in response or [] if str(item) in self.agent_ids]
            if timed_out or not picked:
                await self._complete("No agent was picked, so I did not pass the message on.")
                return
            choice, picked_by = picked[0], "user"
        if choice == ABOUT:
            await self._complete(about_text(agents, workflows))
            return
        if choice in self.workflow_ids:
            await self._run_workflow(workflows[self.workflow_ids.index(choice)], board)
            return
        await self._route_to_agent(choice, picked_by)

    async def _route_to_agent(self, agent_id: str, picked_by: str) -> None:
        run = self.run
        metadata = self.metadata
        request_id = routed_request_id(run.request_id)
        route = {"choice": agent_id, "picked_by": picked_by}
        parent: dict[str, Any] = {
            "kind": "conversation",
            "request_id": run.request_id,
            "agent_id": self.companion_id,
            "session_id": run.session_id,
            "started_by": self.companion_id,
            "parent_request_id": run.request_id,
            "route": route,
        }
        if metadata.get("event_session_id"):
            parent["event_session_id"] = metadata["event_session_id"]
        # A cancel that came while the companion classified stops it here,
        # before it starts a request no one would cancel.
        run._check_cancelled()
        client = self.access.get_client(agent_id)
        await retry_transient(
            lambda: client.post_request(
                run.message,
                session_id=run.session_id,
                structured_output=metadata.get("structured_output_request"),
                metadata=metadata.get("metadata") or None,
                request_id=request_id,
                context=metadata.get("context"),
                host=self.host,
                parent=parent,
            )
        )
        await self._complete(
            "", {"routed": {"agent_id": agent_id, "request_id": request_id, "picked_by": picked_by}}
        )

    async def _run_workflow(self, workflow: Any, board: str) -> None:
        from mash.agents.companion.classifier import (
            WORKFLOW_INPUT_PROMPT,
            coerce_answer,
            field_question,
            missing_fields,
            workflow_input_message,
            workflow_input_schema,
            workflow_reply,
        )

        run = self.run
        schema = workflow_input_schema(workflow)
        values: dict[str, Any] = {}
        if schema is not None:
            values = await self._structured_call(
                loop_index=1,
                system=WORKFLOW_INPUT_PROMPT,
                text=workflow_input_message(workflow, board, run.message),
                schema=schema,
                key="workflow_input",
            )
            self.events = await self.runtime.runtime_store.list_request_events(run.request_id)
        for index, (name, description) in enumerate(missing_fields(workflow, values)):
            _, answer, timed_out = await run._run_interaction(
                loop_index=1,
                interaction_type="info",
                prompt=field_question(workflow, name, description),
                options=None,
                timeout_seconds=ASK_USER_DEFAULT_TIMEOUT_SECONDS,
                step_prefix=f"route.field.{index}",
            )
            if timed_out or not str(answer or "").strip():
                await self._complete(
                    f"I did not run {workflow.workflow_id}: it needs {name}, which I don't have."
                )
                return
            values[name] = coerce_answer(workflow, name, str(answer))
        workflow_input = {k: v for k, v in values.items() if v is not None}
        outcome = await self._wait_for_run(workflow.workflow_id, workflow_input)
        text = workflow_reply(workflow, workflow_input, outcome)
        tokens = self._context_tokens()
        await self.runtime.store.append_entry(
            trace_id=str(run.trace_id),
            session_id=run.session_id,
            agent_id=self.companion_id,
            user_message=run.message,
            agent_response=text,
            signals={},
            context_tokens=tokens,
            tokens=tokens,
            metadata={
                "type": WORKFLOW_RESULT,
                "workflow_id": workflow.workflow_id,
                "run_id": outcome.get("run_id"),
                "input": workflow_input,
                "status": outcome.get("status"),
            },
            app_id=app_id_from_request_metadata(self.metadata),
        )
        await self._complete(
            text,
            {
                "workflow_run": {
                    "workflow_id": workflow.workflow_id,
                    "run_id": outcome.get("run_id"),
                    "status": outcome.get("status"),
                }
            },
        )

    async def _wait_for_run(self, workflow_id: str, workflow_input: dict[str, Any]) -> dict[str, Any]:
        """Start the run, or find it started, and wait for its end; its outcome.

        The run's key is derived from this request, so a recovered request
        gets back the run it started. A run the workflow refuses, or one that
        fails, is the outcome; nothing retries it.
        """
        run = self.run
        relay = self.access.get_workflow_client()
        try:
            started = await relay.post_workflow_run(
                workflow_id,
                parent_request_id=run.request_id,
                workflow_input=workflow_input,
                dedup_key=f"{run.request_id}-route",
            )
        except WorkflowInputError as exc:
            return {"status": "failed", "error": {"input": exc.errors}}
        run_id = str(started.get("run_id") or "")
        state = dict(started)
        async with run._slot_released():
            while str(state.get("status") or "") not in TERMINAL_RUN_STATUSES:
                run._check_cancelled()
                await asyncio.sleep(WORKFLOW_POLL_SECONDS)
                state = await retry_transient(
                    lambda: relay.get_workflow_run(
                        workflow_id, run_id, parent_request_id=run.request_id
                    )
                )
        outcome = {"run_id": run_id, "status": str(state.get("status"))}
        if state.get("status") == "completed":
            outcome["result"] = state.get("result")
        elif state.get("error") is not None:
            outcome["error"] = state.get("error")
        return outcome


async def run_route(run: "RequestRun") -> None:
    """Route the companion request ``run``: its whole life, new or recovered."""
    run.trace_id = await start_request_trace(
        run.runtime, run.request_id, run.session_id, run.message
    )
    with bound_trace_id(run.trace_id):
        await Router(run).route()


__all__ = ["Router", "routed_request_id", "run_route"]

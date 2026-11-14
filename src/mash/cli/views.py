"""What the REPL knows about each request it follows, and how events change it.

``RequestView`` is the state behind one row of the live area: a foreground
request, one of the companion's children, or a request brought back from the
background. ``reduce`` applies one stream event to a view and returns what
that event finished, as ``Output`` values the renderer prints once. Nothing
here prints or reads a clock: the caller passes ``now``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from mash.otel import semconv
from mash.runtime.events.types import RuntimeEventType, event_attributes

RUNNING = "running"
WAITING = "waiting"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
ENDED = frozenset({COMPLETED, FAILED, CANCELLED})

RUN_SUBAGENTS = "RunSubAgents"
RUN_WORKFLOW = "RunWorkflow"


@dataclass
class ToolCall:
    """One tool call of a request, from its start event to its end event."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    started_at: float
    duration_ms: int | None = None
    is_error: bool = False
    result: str = ""
    # What the call is doing now, shown after it in the live row: the step a
    # RunWorkflow call's run is on.
    note: str = ""


@dataclass
class RequestView:
    """One request the REPL follows."""

    agent_id: str
    request_id: str
    started_at: float
    # What was asked: the user's message, or the brief the companion wrote.
    brief: str = ""
    parent: "RequestView | None" = None
    # A child that continues an earlier session of its agent.
    continued: bool = False
    status: str = RUNNING
    # What it is doing now: starting, thinking, answering, tool or waiting.
    phase: str = "starting"
    ended_at: float | None = None
    steps: int = 0
    tool_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    running_tools: dict[str, ToolCall] = field(default_factory=dict)
    # The tool calls of the last model turn: what an approval is about.
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    children: list["RequestView"] = field(default_factory=list)
    # Seq of the last event applied; a reconnect or a watcher starts after it.
    cursor: int = 0
    session_id: str | None = None
    error: str | None = None
    # The terminal event's payload.
    final: dict[str, Any] | None = None
    # Streamed answer text not printed yet: an unfinished Markdown block.
    tail: str = ""
    # Text streamed in the current model turn, and in the turn that answered.
    step_streamed: str = ""
    response_streamed: str = ""

    @property
    def ended(self) -> bool:
        return self.status in ENDED

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def elapsed(self, now: float) -> float:
        return max(0.0, (self.ended_at if self.ended_at is not None else now) - self.started_at)

    def current_tool(self) -> ToolCall | None:
        """The running tool call that started last."""
        if not self.running_tools:
            return None
        return max(self.running_tools.values(), key=lambda tool: tool.started_at)

    def total_tokens(self) -> int:
        return self.tokens + sum(child.total_tokens() for child in self.children)

    def total_tools(self) -> int:
        return self.tool_count + sum(child.total_tools() for child in self.children)


@dataclass(frozen=True)
class Text:
    """Finished Markdown of the answer, ready to print."""

    view: RequestView
    markdown: str


@dataclass(frozen=True)
class Step:
    """One model turn ended."""

    view: RequestView
    number: int
    action: str
    tool_names: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    duration_ms: int
    thinking: str
    # The turn's text when it did not stream and tool calls follow it.
    preamble: str


@dataclass(frozen=True)
class Tool:
    """One tool call ended."""

    view: RequestView
    call: ToolCall


@dataclass(frozen=True)
class Children:
    """A ``RunSubAgents`` call started these child requests."""

    view: RequestView
    children: tuple[RequestView, ...]


@dataclass(frozen=True)
class Workflow:
    """A ``RunWorkflow`` call started; its run is found by ``dedup_key``."""

    view: RequestView
    call: ToolCall
    workflow_id: str
    dedup_key: str


@dataclass(frozen=True)
class Interaction:
    """The request waits on the user."""

    view: RequestView
    payload: dict[str, Any]


@dataclass(frozen=True)
class Ack:
    """An interaction closed."""

    view: RequestView
    payload: dict[str, Any]


@dataclass(frozen=True)
class End:
    """The request ended; ``view.status`` says how."""

    view: RequestView


Output = Text | Step | Tool | Children | Workflow | Interaction | Ack | End


def split_complete_markdown(buffer: str) -> tuple[str, str]:
    """Split streamed text into (printable, remainder).

    Markdown needs whole blocks to format (a code fence is not valid until it
    closes), so the printable part ends at the last blank line outside an open
    code fence. The remainder waits for more text.
    """
    lines = buffer.split("\n")
    fence_open = False
    boundary = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence_open = not fence_open
            continue
        if not fence_open and line.strip() == "":
            boundary = index + 1
    if boundary == 0:
        return "", buffer
    return "\n".join(lines[:boundary]), "\n".join(lines[boundary:])


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _thinking(payload: Mapping[str, Any]) -> str:
    parts = [
        str(block.get("thinking") or "").strip()
        for block in list(payload.get("assistant_blocks") or [])
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    return "\n\n".join(part for part in parts if part)


def _tool_calls(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls = payload.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [
        {
            "id": str(call.get("id") or ""),
            "name": str(call.get("name") or "unknown"),
            "arguments": dict(call.get("arguments") or {})
            if isinstance(call.get("arguments"), dict)
            else {},
        }
        for call in calls
        if isinstance(call, dict)
    ]


def _result_text(result: Any) -> str:
    """A tool result's content as text."""
    if not isinstance(result, Mapping):
        return ""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return "" if content is None else str(content)


def _flush_tail(view: RequestView) -> list[Output]:
    """Print whatever streamed text is still buffered; the block is over."""
    tail, view.tail = view.tail, ""
    return [Text(view, tail)] if tail.strip() else []


def _child_views(
    view: RequestView, payload: Mapping[str, Any], now: float
) -> tuple[RequestView, ...]:
    """The children a ``RunSubAgents`` start event lists, with their briefs."""
    listed = payload.get("subagents")
    if not isinstance(listed, list):
        return ()
    arguments = payload.get("arguments")
    items = arguments.get("requests") if isinstance(arguments, Mapping) else None
    items = items if isinstance(items, list) else []
    children: list[RequestView] = []
    for index, entry in enumerate(listed):
        if not isinstance(entry, Mapping):
            continue
        agent_id = str(entry.get("agent_id") or "").strip()
        request_id = str(entry.get("request_id") or "").strip()
        if not (agent_id and request_id):
            continue
        item = items[index] if index < len(items) and isinstance(items[index], Mapping) else {}
        children.append(
            RequestView(
                agent_id=agent_id,
                request_id=request_id,
                started_at=now,
                brief=str(item.get("prompt") or ""),
                parent=view,
                continued=bool(item.get("session_id")),
            )
        )
    return tuple(children)


def _reduce_trace(view: RequestView, data: Mapping[str, Any], now: float) -> list[Output]:
    event_type = str(data.get("event_type") or "")
    payload = data.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}

    if event_type == RuntimeEventType.CHAT_START.value:
        view.phase = "thinking"
        view.step_streamed = ""
        return []

    if event_type == RuntimeEventType.CHAT_DELTA.value:
        # The logging layer wraps the chunk under "payload"; accept both.
        text = payload.get("text")
        if text is None and isinstance(payload.get("payload"), Mapping):
            text = payload["payload"].get("text")
        if not text:
            return []
        view.phase = "answering"
        view.step_streamed += str(text)
        printable, view.tail = split_complete_markdown(view.tail + str(text))
        return [Text(view, printable)] if printable.strip() else []

    if event_type == RuntimeEventType.CHAT_END.value:
        outputs = _flush_tail(view)
        usage = semconv.token_usage(event_attributes(dict(payload)))
        view.input_tokens += usage.get("input", 0)
        view.output_tokens += usage.get("output", 0)
        view.steps += 1
        action = str(payload.get("action_type") or "unknown")
        calls = _tool_calls(payload)
        view.pending_tool_calls = calls
        streamed, view.step_streamed = view.step_streamed, ""
        if action in ("response", "finish"):
            view.response_streamed = streamed
        text = str(payload.get("assistant_text") or "").strip()
        outputs.append(
            Step(
                view=view,
                number=view.steps,
                action=action,
                tool_names=tuple(call["name"] for call in calls),
                input_tokens=usage.get("input", 0),
                output_tokens=usage.get("output", 0),
                reasoning_tokens=usage.get("reasoning"),
                duration_ms=_int(payload.get("duration_ms")),
                thinking=_thinking(payload),
                preamble=text if action == "tool_call" and not streamed.strip() else "",
            )
        )
        if calls:
            view.phase = "tool"
        elif action in ("response", "finish"):
            # The answer is written; the request is saving its turn.
            view.phase = "finishing"
        else:
            view.phase = "thinking"
        return outputs

    if event_type == RuntimeEventType.EXECUTE_TOOL_START.value:
        attributes = event_attributes(dict(payload))
        call = ToolCall(
            call_id=str(attributes.get(semconv.GEN_AI_TOOL_CALL_ID) or data.get("step_key") or ""),
            name=str(attributes.get(semconv.GEN_AI_TOOL_NAME) or "unknown"),
            arguments=dict(payload.get("arguments") or {})
            if isinstance(payload.get("arguments"), Mapping)
            else {},
            started_at=now,
        )
        view.running_tools[call.call_id] = call
        view.phase = "tool"
        if call.name == RUN_SUBAGENTS:
            children = _child_views(view, payload, now)
            if children:
                view.children.extend(children)
                return [Children(view, children)]
        if call.name == RUN_WORKFLOW:
            run = payload.get("workflow_run")
            if isinstance(run, Mapping) and run.get("workflow_id") and run.get("dedup_key"):
                return [
                    Workflow(view, call, str(run["workflow_id"]), str(run["dedup_key"]))
                ]
        return []

    if event_type == RuntimeEventType.EXECUTE_TOOL_END.value:
        attributes = event_attributes(dict(payload))
        call_id = str(attributes.get(semconv.GEN_AI_TOOL_CALL_ID) or "")
        call = view.running_tools.pop(call_id, None)
        if call is None:
            # An end without its start: the stream was joined after it began.
            call = ToolCall(
                call_id=call_id,
                name=str(attributes.get(semconv.GEN_AI_TOOL_NAME) or "unknown"),
                arguments={},
                started_at=now,
            )
        call.duration_ms = _int(payload.get("duration_ms"))
        call.is_error = payload.get("status") == "error"
        call.result = _result_text(payload.get("result"))
        view.tool_count += 1
        if not view.running_tools:
            view.phase = "thinking"
        return [Tool(view, call)]

    return []


def reduce(view: RequestView, event: Mapping[str, Any], now: float) -> list[Output]:
    """Apply one stream event to ``view``; return what it finished."""
    seq = event.get("seq")
    if isinstance(seq, int):
        view.cursor = seq
    name = str(event.get("event") or "")
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}

    if name == "agent.trace":
        return _reduce_trace(view, data, now)

    if name in ("request.accepted", "request.started", "request.resumed"):
        session_id = str(data.get("session_id") or "").strip()
        if session_id:
            view.session_id = session_id
        if view.status not in ENDED or name == "request.resumed":
            view.status = RUNNING
            view.ended_at = None
        return []

    if name == "request.interaction.create":
        outputs = _flush_tail(view)
        view.status = WAITING
        view.phase = "waiting"
        outputs.append(Interaction(view, dict(data)))
        return outputs

    if name == "request.interaction.ack":
        view.status = RUNNING
        view.phase = "tool" if view.running_tools else "thinking"
        return [Ack(view, dict(data))]

    if name in ("request.completed", "request.error", "request.cancelled"):
        outputs = _flush_tail(view)
        view.status = {
            "request.completed": COMPLETED,
            "request.error": FAILED,
            "request.cancelled": CANCELLED,
        }[name]
        view.ended_at = now
        view.final = dict(data)
        view.running_tools.clear()
        session_id = str(data.get("session_id") or "").strip()
        if session_id:
            view.session_id = session_id
        if view.status == FAILED:
            view.error = str(data.get("error") or "request failed")
        outputs.append(End(view))
        return outputs

    return []


def answer_text(view: RequestView) -> str:
    """The whole answer of a request that completed."""
    final = view.final or {}
    response = final.get("response")
    text = response.get("text") if isinstance(response, Mapping) else None
    return str(text or final.get("text") or view.response_streamed or "")


def final_text(view: RequestView) -> str:
    """The answer to print when the request ends: text that did not stream.

    Empty when the answering turn streamed its text, since that was printed
    as it arrived.
    """
    if view.response_streamed.strip():
        return ""
    final = view.final or {}
    response = final.get("response")
    if isinstance(response, Mapping):
        blocks = response.get("assistant_blocks") or []
        texts = [
            str(block.get("text") or "").strip()
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        texts = [text for text in texts if text]
        if texts:
            return "\n\n".join(texts)
        return str(response.get("text") or "").strip()
    return str(final.get("text") or "").strip()

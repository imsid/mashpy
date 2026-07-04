"""Operational metrics for one scored eval row.

Every dataset row runs through the host under test under a single ``session_id``
that the primary agent and all of its subagents share. Their runtime events are
therefore a self-contained record of what the run *cost* — tokens, steps, tool
calls, latency — independent of the qualitative rubric score.

:func:`compute_row_metrics` is a pure fold over those events (no I/O), so it is
deterministic and unit-testable against a captured event list. The scoring
workflow loads a row's session events and calls this once per row.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..runtime.events.types import RuntimeEvent, RuntimeEventType

# Only these event types carry operational signal; the loader filters to them so
# the huge per-turn payloads (subagent traces, full responses) never load.
_LLM_REQUEST_COMPLETE = "llm.request.complete"
METRIC_EVENT_TYPES: tuple[str, ...] = (
    RuntimeEventType.REQUEST_ACCEPTED.value,
    RuntimeEventType.REQUEST_COMPLETED.value,
    RuntimeEventType.REQUEST_FAILED.value,
    RuntimeEventType.STEP_COMPLETED.value,
    RuntimeEventType.TOOL_CALL_STARTED.value,
    _LLM_REQUEST_COMPLETE,
)

_TERMINAL = {
    RuntimeEventType.REQUEST_COMPLETED.value,
    RuntimeEventType.REQUEST_FAILED.value,
}


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    def add_llm_complete(self, payload: dict[str, Any]) -> None:
        self.input += _as_int(payload.get("input_tokens"))
        self.output += _as_int(payload.get("output_tokens"))
        self.cache_read += _as_int(payload.get("cache_read_input_tokens"))
        self.cache_creation += _as_int(payload.get("cache_creation_input_tokens"))


@dataclass
class SubagentMetrics:
    agent_id: str
    steps: int
    tool_calls: int
    llm_calls: int
    stop_reason: str | None
    tokens: TokenUsage


@dataclass
class RowMetrics:
    """Operational cost of running one row, primary + subagents combined."""

    latency_ms: float | None
    llm_calls: int
    steps: int
    tool_calls: int
    tokens: TokenUsage
    tool_call_breakdown: dict[str, int]
    stop_reason: str | None
    num_subagent_steps: int
    subagents: list[SubagentMetrics] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _AgentAccum:
    """Per-agent fold state within one session."""

    def __init__(self) -> None:
        self.steps = 0
        self.tool_calls = 0
        self.llm_calls = 0
        self.tokens = TokenUsage()
        self.tool_breakdown: dict[str, int] = {}
        self.last_finish_reason: str | None = None
        self.terminal_type: str | None = None
        self.terminal_payload: dict[str, Any] = {}

    def stop_reason(self) -> str | None:
        if self.terminal_type == RuntimeEventType.REQUEST_FAILED.value:
            return "error"
        metadata = self.terminal_payload.get("response_metadata")
        if isinstance(metadata, dict) and metadata.get("stop_reason"):
            # Subagents surface runtime stop reasons here (e.g. ``max_steps``),
            # which the per-call LLM finish_reason never reflects.
            return str(metadata["stop_reason"])
        return self.last_finish_reason


def compute_row_metrics(
    events: list[RuntimeEvent], *, primary_agent_id: str
) -> RowMetrics:
    """Fold a session's runtime events into one :class:`RowMetrics`.

    ``events`` should be every metric-bearing event for the row's session
    (primary and subagents), in any order. Safe on partial logs — a row whose
    host request errored still yields the tokens/steps spent before it failed.
    """
    per_agent: dict[str, _AgentAccum] = {}
    timestamps: list[float] = []

    for event in events:
        acc = per_agent.setdefault(event.agent_id, _AgentAccum())
        timestamps.append(float(event.created_at))
        payload = event.payload or {}
        etype = event.event_type
        if etype == RuntimeEventType.STEP_COMPLETED.value:
            acc.steps += 1
        elif etype == RuntimeEventType.TOOL_CALL_STARTED.value:
            acc.tool_calls += 1
            name = str(payload.get("tool_name") or "unknown")
            acc.tool_breakdown[name] = acc.tool_breakdown.get(name, 0) + 1
        elif etype == _LLM_REQUEST_COMPLETE:
            acc.llm_calls += 1
            acc.tokens.add_llm_complete(payload)
            finish = payload.get("finish_reason")
            if finish:
                acc.last_finish_reason = str(finish)
        elif etype in _TERMINAL:
            acc.terminal_type = etype
            acc.terminal_payload = payload

    primary = per_agent.get(primary_agent_id, _AgentAccum())
    subagents: list[SubagentMetrics] = []
    tokens = TokenUsage()
    tool_breakdown: dict[str, int] = {}
    steps = llm_calls = tool_calls = num_subagent_steps = 0

    for agent_id, acc in sorted(per_agent.items()):
        steps += acc.steps
        llm_calls += acc.llm_calls
        tool_calls += acc.tool_calls
        tokens.input += acc.tokens.input
        tokens.output += acc.tokens.output
        tokens.cache_read += acc.tokens.cache_read
        tokens.cache_creation += acc.tokens.cache_creation
        for name, count in acc.tool_breakdown.items():
            tool_breakdown[name] = tool_breakdown.get(name, 0) + count
        if agent_id != primary_agent_id:
            num_subagent_steps += acc.steps
            subagents.append(
                SubagentMetrics(
                    agent_id=agent_id,
                    steps=acc.steps,
                    tool_calls=acc.tool_calls,
                    llm_calls=acc.llm_calls,
                    stop_reason=acc.stop_reason(),
                    tokens=acc.tokens,
                )
            )

    latency_ms: float | None = None
    if len(timestamps) >= 2:
        latency_ms = round((max(timestamps) - min(timestamps)) * 1000.0, 1)

    return RowMetrics(
        latency_ms=latency_ms,
        llm_calls=llm_calls,
        steps=steps,
        tool_calls=tool_calls,
        tokens=tokens,
        tool_call_breakdown=tool_breakdown,
        stop_reason=primary.stop_reason(),
        num_subagent_steps=num_subagent_steps,
        subagents=subagents,
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

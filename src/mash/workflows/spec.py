"""Workflow specification types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from mash.runtime.spec import AgentSpec


@dataclass(frozen=True)
class StepContext:
    """Runtime context handed to a step body.

    Every field is stable across retries of the same logical step, so a step
    with external effects can build its own idempotency key (e.g.
    ``f"{ctx.run_id}:{ctx.step_id}"``). The framework never invents one.
    """

    run_id: str
    step_id: str
    workflow_input: dict[str, Any]
    attempt: int = 1


@dataclass(frozen=True, init=False)
class StepSpec:
    """Base marker for one workflow step.

    Concrete steps are :class:`AgentStep` (one run of the agent loop) and
    :class:`CodeStep` (deterministic Python). Step ``n``'s output threads forward
    into step ``n+1``'s input, and the final step's output is the workflow result.
    """

    step_id: str
    input: type[BaseModel] | None
    output: type[BaseModel] | dict[str, Any]
    timeout_s: float | None
    kind: str


@dataclass(frozen=True, init=False)
class AgentStep(StepSpec):
    """A step executed by one run of a registered agent's loop.

    ``input`` may be a pydantic model (coerced/validated) or ``None`` (the
    workflow input and prior output pass through unchanged, for agents that
    read ``workflow_input`` directly). ``output`` may be a pydantic model or a
    JSON-schema dict; either becomes the request's structured-output schema.
    ``skill_name``, when set, instructs the agent to load that skill before
    doing the step's work.
    """

    step_id: str
    input: type[BaseModel] | None
    output: type[BaseModel] | dict[str, Any]
    agent_id: str
    agent_spec: AgentSpec | None
    skill_name: str | None
    timeout_s: float | None
    kind: str

    def __init__(
        self,
        step_id: str,
        *,
        output: type[BaseModel] | dict[str, Any],
        input: type[BaseModel] | None = None,
        agent_id: str | None = None,
        agent_spec: AgentSpec | None = None,
        skill_name: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        resolved_step_id = _require_step_id(step_id)
        if input is not None:
            _require_model(input, "AgentStep input")
        _require_output(output, "AgentStep output")

        resolved_agent_id = str(agent_id or "").strip()
        if agent_spec is not None:
            spec_agent_id = str(agent_spec.get_agent_id() or "").strip()
            if not spec_agent_id:
                raise ValueError("workflow step agent id is required")
            if resolved_agent_id and resolved_agent_id != spec_agent_id:
                raise ValueError(
                    "agent_id must match agent_spec.get_agent_id() when both are provided"
                )
            resolved_agent_id = spec_agent_id
        if not resolved_agent_id:
            raise ValueError("workflow step agent id is required")

        object.__setattr__(self, "step_id", resolved_step_id)
        object.__setattr__(self, "input", input)
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "agent_id", resolved_agent_id)
        object.__setattr__(self, "agent_spec", agent_spec)
        object.__setattr__(
            self, "skill_name", str(skill_name).strip() if skill_name else None
        )
        object.__setattr__(self, "timeout_s", _normalize_timeout(timeout_s))
        object.__setattr__(self, "kind", "agent")


@dataclass(frozen=True, init=False)
class CodeStep(StepSpec):
    """A step executed by deterministic Python.

    ``run`` is ``run(inp: input, ctx: StepContext) -> output`` and may be sync or
    async. By default DBOS memoizes the body as an opaque step. Set
    ``orchestration=True`` only when the body must start durable child workflows;
    that mode runs in the parent workflow context and may replay, so its external
    work must be guarded by a durable, idempotent ledger. Code steps are
    pydantic-typed on both edges — they are authored in Python, never over the
    wire.
    """

    step_id: str
    input: type[BaseModel]
    output: type[BaseModel]
    run: Callable[..., Any]
    agent_ids: tuple[str, ...]
    orchestration: bool
    timeout_s: float | None
    kind: str

    def __init__(
        self,
        step_id: str,
        *,
        run: Callable[..., Any],
        input: type[BaseModel],
        output: type[BaseModel],
        agent_ids: tuple[str, ...] | list[str] = (),
        orchestration: bool = False,
        timeout_s: float | None = None,
    ) -> None:
        resolved_step_id = _require_step_id(step_id)
        _require_model(input, "CodeStep input")
        _require_model(output, "CodeStep output")
        if not callable(run):
            raise ValueError("CodeStep run must be callable")

        object.__setattr__(self, "step_id", resolved_step_id)
        object.__setattr__(self, "input", input)
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "run", run)
        object.__setattr__(self, "agent_ids", _normalize_agent_ids(agent_ids))
        object.__setattr__(self, "orchestration", bool(orchestration))
        object.__setattr__(self, "timeout_s", _normalize_timeout(timeout_s))
        object.__setattr__(self, "kind", "code")


@dataclass(frozen=True)
class WorkflowSpec:
    """One workflow, run as an ordered step pipeline.

    ``steps`` is the forward pipeline: each step's ``output`` threads into the
    next step's ``input`` (merged over the immutable ``workflow_input``), and the
    final step's output is the run result. ``input_model``, when set, types
    ``workflow_input`` and turns on strict build-time adjacency checks.
    """

    workflow_id: str
    steps: list[StepSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    input_model: type[BaseModel] | None = None

    def __post_init__(self) -> None:
        validate_step_pipeline(self.workflow_id, self.steps, self.input_model)


def _require_step_id(step_id: str) -> str:
    resolved = str(step_id or "").strip()
    if not resolved:
        raise ValueError("step_id is required")
    return resolved


def _require_model(model: Any, label: str) -> None:
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ValueError(f"{label} must be a pydantic BaseModel subclass")


def _require_output(output: Any, label: str) -> None:
    if isinstance(output, dict):
        return
    _require_model(output, f"{label} (a pydantic model or JSON-schema dict)")


def _normalize_timeout(timeout_s: float | None) -> float | None:
    if timeout_s is None:
        return None
    value = float(timeout_s)
    if value <= 0:
        raise ValueError("timeout_s must be positive")
    return value


def _normalize_agent_ids(agent_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(agent_ids, (str, bytes)):
        raise ValueError("CodeStep agent_ids must be a sequence of agent ids")
    resolved: list[str] = []
    for raw_agent_id in agent_ids:
        agent_id = str(raw_agent_id or "").strip()
        if not agent_id:
            raise ValueError("CodeStep agent_ids cannot contain an empty agent id")
        if agent_id not in resolved:
            resolved.append(agent_id)
    return tuple(resolved)


def _required_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field.is_required()}


def _output_fields(output: type[BaseModel] | dict[str, Any]) -> set[str]:
    if isinstance(output, dict):
        props = output.get("properties")
        return set(props) if isinstance(props, dict) else set()
    return set(output.model_fields)


def validate_step_pipeline(
    workflow_id: str,
    steps: list[StepSpec],
    input_model: type[BaseModel] | None,
) -> None:
    """Validate a forward step pipeline at build time.

    Checks unique step ids, step I/O typing, and agent-step agent ids. When
    ``input_model`` is provided, also checks adjacency: every required input
    field of step ``n`` must be produced by ``workflow_input`` or the previous
    step's output. Adjacency is only enforced for steps whose ``input`` is a
    pydantic model; a passthrough (``None``) input skips it. Without
    ``input_model`` the ``workflow_input`` fields are unknown, so adjacency is not
    enforced — declare it to get strict checks.
    """
    resolved_workflow_id = str(workflow_id or "").strip()
    if not resolved_workflow_id:
        raise ValueError("workflow_id is required")
    if not steps:
        raise ValueError("workflow steps are required")
    if input_model is not None:
        _require_model(input_model, "workflow input_model")

    base_fields = set(input_model.model_fields) if input_model is not None else None
    prev_output_fields: set[str] | None = None
    seen: set[str] = set()
    for step in steps:
        step_id = _require_step_id(step.step_id)
        if step_id in seen:
            raise ValueError(f"workflow step '{step_id}' is already defined")
        seen.add(step_id)
        if step.input is not None:
            _require_model(step.input, f"step '{step_id}' input")
        _require_output(step.output, f"step '{step_id}' output")
        if getattr(step, "kind", None) == "agent" and not str(
            getattr(step, "agent_id", "") or ""
        ).strip():
            raise ValueError(f"workflow step '{step_id}' agent id is required")

        if base_fields is not None and step.input is not None:
            available = set(base_fields)
            if prev_output_fields is not None:
                available |= prev_output_fields
            missing = _required_fields(step.input) - available
            if missing:
                raise ValueError(
                    f"workflow step '{step_id}' input requires fields not available "
                    f"from workflow_input or the previous step's output: "
                    f"{sorted(missing)}"
                )
        prev_output_fields = _output_fields(step.output)

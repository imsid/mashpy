"""What the companion asks its model, and the text it answers with itself.

The classifier is one structured output call whose only field is an enum: the
host's agent ids, its workflow ids, ``about`` and ``unclear``. A route to a
workflow makes a second call whose schema is the workflow's input schema.
``about`` is answered from the host's descriptions with no model call.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from mash.runtime.host.directory import AgentMetadata
from mash.runtime.structured_output import normalize_structured_output_schema

ABOUT = "about"
UNCLEAR = "unclear"

CLASSIFIER_PROMPT = """You route each user message in a conversation to the one agent or workflow
that handles it. You get the host's agents and workflows with their
descriptions, the conversation's board (every earlier entry, numbered, with
the agent that wrote it) and the new message.

Answer with one route:
- an agent id: the agent whose description fits the message. A follow-up on
  an earlier entry, such as a question about its answer or a change to it,
  goes to the agent that wrote that entry.
- a workflow id: the message asks for exactly what the workflow does.
- about: the message asks what you, the agents or the workflows can do.
- unclear: no agent or workflow fits, or more than one fits equally well.
"""

WORKFLOW_INPUT_PROMPT = """You fill in the input of a workflow from a user's message and the
conversation's board before it. Use only values the user gave or the board
states. Leave a field null when neither gives it; do not guess.
"""


def route_choices(agent_ids: Iterable[str], workflow_ids: Iterable[str]) -> list[str]:
    """The enum: agent ids, workflow ids, ``about`` and ``unclear``."""
    return list(dict.fromkeys([*agent_ids, *workflow_ids, ABOUT, UNCLEAR]))


def classifier_schema(choices: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"route": {"type": "string", "enum": list(choices)}},
        "required": ["route"],
        "additionalProperties": False,
    }


def _agent_lines(agents: list[tuple[str, Optional[AgentMetadata]]]) -> list[str]:
    lines: list[str] = []
    for agent_id, meta in agents:
        if meta is None:
            lines.append(f"- {agent_id}")
            continue
        lines.append(f"- {agent_id} | {meta.display_name}: {meta.description}")
        if meta.capabilities:
            lines.append(f"  Capabilities: {', '.join(meta.capabilities)}")
        if meta.usage_guidance:
            lines.append(f"  Guidance: {meta.usage_guidance}")
    return lines


def _workflow_name(workflow: Any) -> tuple[str, str]:
    metadata = workflow.metadata if isinstance(getattr(workflow, "metadata", None), dict) else {}
    name = str(metadata.get("display_name") or workflow.workflow_id).strip()
    return name, str(metadata.get("description") or "").strip()


def _workflow_lines(workflows: list[Any]) -> list[str]:
    lines: list[str] = []
    for workflow in workflows:
        name, description = _workflow_name(workflow)
        head = f"- {workflow.workflow_id} | {name}"
        lines.append(f"{head}: {description}" if description else head)
    return lines


def classifier_input(
    *,
    agents: list[tuple[str, Optional[AgentMetadata]]],
    workflows: list[Any],
    board: str,
    message: str,
) -> str:
    sections = ["AGENTS", *_agent_lines(agents)]
    if workflows:
        sections += ["", "WORKFLOWS", *_workflow_lines(workflows)]
    sections += ["", "BOARD", board or "(no entries yet)", "", "MESSAGE", message]
    return "\n".join(sections)


def about_text(agents: list[tuple[str, Optional[AgentMetadata]]], workflows: list[Any]) -> str:
    """What the host can do, from its agents' and workflows' descriptions."""
    lines = ["I pass each message to the agent or workflow that fits it.", "", "Agents:"]
    for agent_id, meta in agents:
        if meta is None:
            lines.append(f"- {agent_id}")
        else:
            lines.append(f"- {meta.display_name} ({agent_id}): {meta.description}")
    if workflows:
        lines += ["", "Workflows:"]
        for workflow in workflows:
            name, description = _workflow_name(workflow)
            lines.append(
                f"- {name} ({workflow.workflow_id})" + (f": {description}" if description else "")
            )
    return "\n".join(lines)


def unclear_prompt() -> str:
    return "I'm not sure which agent should handle this. Pick one:"


# --- workflow input -----------------------------------------------------------


def _input_schema(workflow: Any) -> dict[str, Any]:
    model = getattr(workflow, "input_model", None)
    return model.model_json_schema() if model is not None else {}


def workflow_input_schema(workflow: Any) -> Optional[dict[str, Any]]:
    """The input call's schema: the workflow's own, with every field nullable.

    A field the model cannot fill comes back null, so the companion can ask
    the user for it. None when the workflow takes no input.
    """
    schema = _input_schema(workflow)
    properties = schema.get("properties") or {}
    if not properties:
        return None
    nullable = {
        name: {
            "anyOf": [{k: v for k, v in prop.items() if k != "description"}, {"type": "null"}],
            **({"description": prop["description"]} if "description" in prop else {}),
        }
        for name, prop in properties.items()
    }
    wrapped = {"type": "object", "properties": nullable}
    if "$defs" in schema:
        wrapped["$defs"] = schema["$defs"]
    return normalize_structured_output_schema(wrapped)


def workflow_input_message(workflow: Any, board: str, message: str) -> str:
    name, description = _workflow_name(workflow)
    head = f"WORKFLOW\n{workflow.workflow_id} | {name}" + (f": {description}" if description else "")
    return "\n".join([head, "", "BOARD", board or "(no entries yet)", "", "MESSAGE", message])


def missing_fields(workflow: Any, values: dict[str, Any]) -> list[tuple[str, str]]:
    """The required fields with no value: each name and its description."""
    schema = _input_schema(workflow)
    properties = schema.get("properties") or {}
    return [
        (name, str((properties.get(name) or {}).get("description") or name))
        for name in schema.get("required") or []
        if values.get(name) is None
    ]


def field_question(workflow: Any, name: str, description: str) -> str:
    title, _ = _workflow_name(workflow)
    if description and description != name:
        return f"To run {title} I need {name}: {description}"
    return f"To run {title} I need {name}."


def coerce_answer(workflow: Any, name: str, answer: str) -> Any:
    """The user's answer to a field question, as the field's JSON type."""
    prop = (_input_schema(workflow).get("properties") or {}).get(name) or {}
    kind = prop.get("type")
    text = answer.strip()
    if kind in ("integer", "number", "boolean", "array", "object"):
        try:
            return json.loads(text.lower() if kind == "boolean" else text)
        except ValueError:
            return text
    return text


def workflow_reply(workflow: Any, workflow_input: dict[str, Any], run: dict[str, Any]) -> str:
    """The run as the user sees it and as its board entry records it."""
    name, _ = _workflow_name(workflow)
    status = str(run.get("status") or "failed")
    lines = [
        f"Ran {name} ({workflow.workflow_id}): {status}.",
        f"Input: {json.dumps(workflow_input, ensure_ascii=False, default=str)}",
    ]
    if status == "completed":
        lines.append(f"Result: {json.dumps(run.get('result'), ensure_ascii=False, default=str)}")
    elif run.get("error"):
        error = run.get("error")
        if not isinstance(error, str):
            error = json.dumps(error, ensure_ascii=False, default=str)
        lines.append(f"Error: {error}")
    # Blank lines between, so each line stays its own when rendered as markdown.
    return "\n\n".join(lines)


__all__ = [
    "ABOUT",
    "CLASSIFIER_PROMPT",
    "UNCLEAR",
    "WORKFLOW_INPUT_PROMPT",
    "about_text",
    "classifier_input",
    "classifier_schema",
    "coerce_answer",
    "field_question",
    "missing_fields",
    "route_choices",
    "unclear_prompt",
    "workflow_input_message",
    "workflow_input_schema",
    "workflow_reply",
]

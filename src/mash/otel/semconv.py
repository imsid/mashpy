"""OpenTelemetry attribute names Mash stores and exports.

Runtime events keep their OpenTelemetry attributes under these names in
``payload["attributes"]``, so the projection copies them without translating.
The GenAI conventions are marked Development and rename attributes between
releases; the version Mash targets is pinned here, and this module is the one
place the names are spelled.
"""

from __future__ import annotations

from typing import Any, Mapping

SEMCONV_VERSION = "1.37.0"
SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"

# GenAI operations. Each is also the prefix of the events that bound its span.
OP_INVOKE_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_INFERENCE_DETAILS_EVENT = "gen_ai.client.inference.operation.details"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"

ERROR_TYPE = "error.type"
EXCEPTION_EVENT = "exception"
EXCEPTION_TYPE = "exception.type"
EXCEPTION_MESSAGE = "exception.message"

SERVICE_NAME = "service.name"

# Mash attributes for spans the conventions have no name for.
MASH_REQUEST_ID = "mash.request_id"
MASH_HOST_ID = "mash.host_id"
MASH_REQUEST_MODE = "mash.request.mode"
MASH_REQUEST_STATUS = "mash.request.status"
MASH_LOOP_INDEX = "mash.loop_index"
MASH_INTERACTION_TYPE = "mash.interaction.type"
MASH_USAGE_REASONING_TOKENS = "mash.usage.reasoning_tokens"
MASH_WORKFLOW_ID = "mash.workflow_id"
MASH_WORKFLOW_RUN_ID = "mash.workflow_run_id"
MASH_WORKFLOW_STATUS = "mash.workflow.status"
MASH_WORKFLOW_STEP_ID = "mash.workflow.step_id"
MASH_WORKFLOW_STEP_KIND = "mash.workflow.step.kind"
MASH_WORKFLOW_STEP_ATTEMPT = "mash.workflow.step.attempt"


def usage_attributes(token_usage: Mapping[str, Any] | None) -> dict[str, int]:
    """Token counts as GenAI usage attributes, from ``input``/``output``/``cache_*``."""
    usage = dict(token_usage or {})
    pairs = (
        ("input", GEN_AI_USAGE_INPUT_TOKENS),
        ("output", GEN_AI_USAGE_OUTPUT_TOKENS),
        ("cache_read", GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS),
        ("cache_write", GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS),
        ("reasoning", MASH_USAGE_REASONING_TOKENS),
    )
    attributes: dict[str, int] = {}
    for key, name in pairs:
        value = usage.get(key)
        if value is None:
            continue
        try:
            attributes[name] = int(value)
        except (TypeError, ValueError):
            continue
    return attributes


def token_usage(attributes: Mapping[str, Any] | None) -> dict[str, int]:
    """The usage attributes back as ``input``/``output``/``cache_read``/``cache_write``.

    ``reasoning`` is present only when the provider reported it.
    """
    attrs = dict(attributes or {})

    def _int(name: str) -> int:
        try:
            return int(attrs.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    usage = {
        "input": _int(GEN_AI_USAGE_INPUT_TOKENS),
        "output": _int(GEN_AI_USAGE_OUTPUT_TOKENS),
        "cache_read": _int(GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS),
        "cache_write": _int(GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS),
    }
    if attrs.get(MASH_USAGE_REASONING_TOKENS) is not None:
        usage["reasoning"] = _int(MASH_USAGE_REASONING_TOKENS)
    return usage

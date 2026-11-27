"""Importable runtime fixtures for hosted runtime tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from mash.core.config import AgentConfig
from mash.policies import Policy
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import (
    LLMContentBlock,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
)
from mash.runtime import AgentSpec, AgentMetadata
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry


# The policy tests submit under: the defaults, as a request's snapshot holds it.
DEFAULT_POLICY: dict[str, Any] = Policy().model_dump()
# The app and policy tests act for, and the identity arguments a runtime submit takes.
TEST_APP_ID = "app-1"
TEST_POLICY_ID = "p-1"
TEST_IDENTITY: dict[str, Any] = {
    "app_id": TEST_APP_ID,
    "policy_id": TEST_POLICY_ID,
    "policy": DEFAULT_POLICY,
}


class DeterministicLLMProvider(LLMProvider):
    def __init__(
        self,
        *,
        response_text: str,
        fail_on_message: str | None = None,
        model_name: str = "test-model",
        delay_seconds: float = 0.0,
    ) -> None:
        self._response_text = response_text
        self._fail_on_message = fail_on_message
        self._model_name = model_name
        self._delay_seconds = max(0.0, float(delay_seconds))
        self.last_system: Any = None

    @property
    def model(self) -> str:
        return self._model_name

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.last_system = request.system
        user_text = ""
        for message in reversed(request.messages):
            if message.role == "user":
                user_text = "".join(
                    block.data.get("text", "")
                    for block in message.content
                    if block.type == "text"
                )
                break
        if self._fail_on_message and self._fail_on_message in user_text:
            raise RuntimeError(self._fail_on_message)
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        return LLMResponse(
            thought_summary="",
            text=self._response_text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(self._response_text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class DeterministicAgentSpec(AgentSpec):
    def __init__(
        self,
        *,
        agent_id: str,
        response_text: str = "ok",
        fail_on_message: str | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.agent_id = agent_id
        self.response_text = response_text
        self.fail_on_message = fail_on_message
        self.delay_seconds = delay_seconds
        self.provider = DeterministicLLMProvider(
            response_text=self.response_text,
            fail_on_message=self.fail_on_message,
            delay_seconds=self.delay_seconds,
        )

    def get_agent_id(self) -> str:
        return self.agent_id

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(agent_id=self.agent_id, system_prompt=f"You are {self.agent_id}.")


class RoutingLLMProvider(LLMProvider):
    """The companion's model in tests: it answers the classifier and the
    workflow input call with structured output, and makes no other call.

    ``route`` is the classifier's answer, or a function of the user's message
    that returns it. ``workflow_input`` is the input call's answer.
    """

    def __init__(
        self,
        route: str | Callable[[str], str],
        *,
        workflow_input: dict[str, Any] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._route = route
        self._workflow_input = dict(workflow_input or {})
        self._delay_seconds = delay_seconds
        self.requests: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return "test-model"

    @staticmethod
    def message_of(request: LLMRequest) -> str:
        """The user's message in a classifier or input call."""
        text = "".join(
            block.data.get("text", "") for block in request.messages[-1].content if block.type == "text"
        )
        return text.rsplit("MESSAGE\n", 1)[-1]

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        schema = (request.provider_options or {}).get("structured_output") or {}
        if "route" in (schema.get("properties") or {}):
            route = self._route(self.message_of(request)) if callable(self._route) else self._route
            text = json.dumps({"route": route})
        else:
            text = json.dumps(self._workflow_input)
        return LLMResponse(
            thought_summary="",
            text=text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=3, output_tokens=1, total_tokens=4),
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class ToolCallingLLMProvider(LLMProvider):
    """Calls one tool once, then answers with the tool's result text."""

    def __init__(self, *, tool_name: str, arguments: dict[str, Any]) -> None:
        self._tool_name = tool_name
        self._arguments = dict(arguments)
        self.last_system: Any = None

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.last_system = request.system
        result_text: str | None = None
        for message in request.messages:
            for block in message.content:
                if block.type == "tool_result":
                    content = block.data.get("content")
                    result_text = content if isinstance(content, str) else str(content)
        if result_text is None:
            return LLMResponse(
                thought_summary="",
                text="",
                tool_calls=[
                    ToolCall(id="call-1", name=self._tool_name, arguments=self._arguments)
                ],
                content_blocks=[
                    LLMContentBlock.tool_call(
                        tool_call_id="call-1",
                        name=self._tool_name,
                        arguments=self._arguments,
                    )
                ],
                stop_reason="tool_call",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            )
        return LLMResponse(
            thought_summary="",
            text=result_text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(result_text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=3, output_tokens=1, total_tokens=4),
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class ToolCallingAgentSpec(DeterministicAgentSpec):
    def __init__(self, *, agent_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__(agent_id=agent_id)
        self.provider = ToolCallingLLMProvider(tool_name=tool_name, arguments=arguments)


def metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Research",
        description="Research specialist",
        capabilities=["search", "summarize"],
        usage_guidance="Use for focused research tasks.",
    )


def build_spec(
    *,
    agent_id: str,
    response_text: str = "ok",
    fail_on_message: str | None = None,
    delay_seconds: float = 0.0,
    ) -> DeterministicAgentSpec:
    return DeterministicAgentSpec(
        agent_id=agent_id,
        response_text=response_text,
        fail_on_message=fail_on_message,
        delay_seconds=delay_seconds,
    )


def build_tool_calling_spec(
    *, agent_id: str, tool_name: str, arguments: dict[str, Any]
) -> ToolCallingAgentSpec:
    """A spec whose model calls ``tool_name`` once and answers with its result."""
    return ToolCallingAgentSpec(agent_id=agent_id, tool_name=tool_name, arguments=arguments)


def patch_companion_llm(provider: LLMProvider):
    """Run the companion, which routes every host request, on ``provider``.

    A context manager over ``unittest.mock.patch``; the companion builds its
    model when its process starts, so enter it before the pool starts.
    """
    from unittest.mock import patch

    from mash.agents.companion import CompanionSpec

    return patch.object(CompanionSpec, "build_llm", lambda self: provider)

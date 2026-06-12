"""Importable runtime fixtures for hosted runtime tests."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from mash.core.config import AgentConfig
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
        self.last_session_id: str | None = None

    @property
    def model(self) -> str:
        return self._model_name

    async def send(self, request: LLMRequest) -> LLMResponse:
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
            text=self._response_text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(self._response_text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, app_id
        self.last_session_id = session_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id

    def get_event_logger_session_id(self) -> str | None:
        return self.last_session_id


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

    def get_agent_id(self) -> str:
        return self.agent_id

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return DeterministicLLMProvider(
            response_text=self.response_text,
            fail_on_message=self.fail_on_message,
            delay_seconds=self.delay_seconds,
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id=self.agent_id, system_prompt=f"You are {self.agent_id}.")


class DelegatingLLMProvider(LLMProvider):
    def __init__(
        self,
        *,
        final_text: str,
        subagent_id: str,
        subagent_prompt: str,
    ) -> None:
        self._final_text = final_text
        self._subagent_id = subagent_id
        self._subagent_prompt = subagent_prompt
        self.last_session_id: str | None = None
        self.last_system: Any = None

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.last_system = request.system
        saw_tool_result = False
        for message in request.messages:
            if message.role != "tool":
                continue
            for block in message.content:
                if block.type == "tool_result":
                    saw_tool_result = True
                    break
            if saw_tool_result:
                break
        if not saw_tool_result:
            return LLMResponse(
                text="Delegating.",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="InvokeSubagent",
                        arguments={
                            "agent_id": self._subagent_id,
                            "prompt": self._subagent_prompt,
                            "opts": {"timeout_ms": 1500},
                        },
                    )
                ],
                content_blocks=[
                    LLMContentBlock.text("Delegating."),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-1",
                        name="InvokeSubagent",
                        arguments={
                            "agent_id": self._subagent_id,
                            "prompt": self._subagent_prompt,
                            "opts": {"timeout_ms": 1500},
                        },
                    ),
                ],
                stop_reason="tool_call",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            )
        return LLMResponse(
            text=self._final_text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(self._final_text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=3, output_tokens=1, total_tokens=4),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, app_id
        self.last_session_id = session_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id

    def get_event_logger_session_id(self) -> str | None:
        return self.last_session_id


class DelegatingAgentSpec(AgentSpec):
    def __init__(
        self,
        *,
        agent_id: str,
        final_text: str,
        subagent_id: str,
        subagent_prompt: str,
    ) -> None:
        self.agent_id = agent_id
        self.final_text = final_text
        self.subagent_id = subagent_id
        self.subagent_prompt = subagent_prompt
        self.provider = DelegatingLLMProvider(
            final_text=self.final_text,
            subagent_id=self.subagent_id,
            subagent_prompt=self.subagent_prompt,
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
        return AgentConfig(app_id=self.agent_id, system_prompt=f"You are {self.agent_id}.")


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
    ) -> AgentSpec:
    return DeterministicAgentSpec(
        agent_id=agent_id,
        response_text=response_text,
        fail_on_message=fail_on_message,
        delay_seconds=delay_seconds,
    )


def build_delegating_spec(
    *,
    agent_id: str,
    final_text: str,
    subagent_id: str,
    subagent_prompt: str,
) -> AgentSpec:
    return DelegatingAgentSpec(
        agent_id=agent_id,
        final_text=final_text,
        subagent_id=subagent_id,
        subagent_prompt=subagent_prompt,
    )

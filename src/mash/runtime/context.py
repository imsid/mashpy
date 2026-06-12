"""Context, history, and payload helpers for the runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from ..core.context import (
    Action,
    ActionType,
    Context,
    Message,
    MessageRole,
    Response,
    ToolCall,
)
from ..core.context import ToolResult as ContextToolResult
from ..memory.compaction import compact_conversation

if TYPE_CHECKING:
    from .service import AgentRuntime


async def get_session_info(
    self: "AgentRuntime",
    session_id: str | None = None,
) -> dict[str, Any]:
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")
    return {
        "app_id": self.app_id,
        "agent_id": self.app_id,
        "session_id": session_id_value,
        "model": self.get_model(),
        "max_steps": self.get_max_steps(),
        "session_total_tokens": await get_session_total_tokens(self, session_id_value),
    }


async def get_history_turns(
    self: "AgentRuntime",
    session_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return await self.store.get_turns(
        session_id=session_id,
        app_id=self.app_id,
        limit=limit,
    )


async def list_sessions(self: "AgentRuntime") -> list[dict[str, Any]]:
    if not hasattr(self.store, "list_sessions"):
        return []
    return await self.store.list_sessions(app_id=self.app_id)


async def get_session_signals(
    self: "AgentRuntime",
    session_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return await self.store.get_session_signals(
        session_id=session_id,
        app_id=self.app_id,
        limit=limit,
    )


async def get_session_total_tokens(
    self: "AgentRuntime",
    session_id: str | None = None,
) -> int:
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")

    turns = await self.store.get_turns(
        session_id=session_id_value,
        app_id=self.app_id,
        limit=1,
    )
    if not turns:
        return 0
    value = turns[-1].get("session_total_tokens", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def compact_session(
    self: "AgentRuntime",
    session_id: str | None = None,
    *,
    reason: str = "manual",
    session_total_tokens_reset: int = 0,
) -> tuple[Optional[str], Optional[str]]:
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")

    llm = self.definition.build_llm()
    if hasattr(llm, "set_event_logger"):
        llm.set_event_logger(self.event_logger, session_id_value, self.app_id)

    return await compact_conversation(
        store=self.store,
        llm=llm,
        app_id=self.app_id,
        session_id=session_id_value,
        max_tokens=self.agent.config.max_tokens,
        temperature=self.agent.config.compaction_temperature,
        turn_limit=self.agent.config.compaction_turn_limit,
        reason=reason,
        session_total_tokens_reset=session_total_tokens_reset,
    )


async def build_context_with_history(
    self: "AgentRuntime",
    session_id: str,
    message: str,
    *,
    system_prompt: Any = None,
) -> Context:
    context = Context(
        system_prompt=system_prompt if system_prompt is not None else self.system_prompt
    )

    if self.agent.config.conversation_history_turns > 0:
        turns = await self.store.get_turns(
            session_id=session_id,
            app_id=self.app_id,
            limit=None,
        )
        if turns:
            summary_index = None
            for idx in range(len(turns) - 1, -1, -1):
                meta = turns[idx].get("metadata") or {}
                if meta.get("type") == "summary_checkpoint":
                    summary_index = idx
                    break

            if summary_index is not None:
                tail_turns = turns[summary_index + 1 :]
                tail_turns = tail_turns[-self.agent.config.conversation_history_turns :]
                turns_to_include = [turns[summary_index]] + tail_turns
            else:
                turns_to_include = turns[-self.agent.config.conversation_history_turns :]

            for turn in turns_to_include:
                meta = turn.get("metadata") or {}
                user_text = turn.get("user_message")
                if user_text and meta.get("type") != "summary_checkpoint":
                    context.add_message(
                        MessageRole.USER,
                        user_text,
                        source="history",
                        turn_id=turn.get("turn_id"),
                    )

                agent_text = turn.get("agent_response")
                if agent_text:
                    context.add_message(
                        MessageRole.ASSISTANT,
                        agent_text,
                        source="history",
                        turn_id=turn.get("turn_id"),
                    )

    context.add_user_message(message)
    return context


def compute_turn_tokens(response_metadata: Dict[str, Any]) -> int:
    token_usage = response_metadata.get("token_usage")
    if not token_usage:
        return 0

    input_tokens = token_usage.get("input")
    output_tokens = token_usage.get("output")
    if input_tokens is None or output_tokens is None:
        return 0

    return int(input_tokens) + int(output_tokens)


async def build_context_payload(
    self: "AgentRuntime",
    *,
    session_id: str,
    message: str,
    system_prompt: Any = None,
) -> dict[str, Any]:
    compaction_summary_text: Optional[str] = None
    compaction_summary_turn_id: Optional[str] = None
    if self.agent.config.compaction_token_threshold > 0:
        session_total_tokens = await self.get_session_total_tokens(session_id)
        if session_total_tokens >= self.agent.config.compaction_token_threshold:
            compaction_summary_text, compaction_summary_turn_id = await self.compact_session(
                session_id,
                reason="auto",
                session_total_tokens_reset=0,
            )
    context = await build_context_with_history(
        self, session_id, message, system_prompt=system_prompt
    )
    return {
        "context": serialize_context(context),
        "compaction": {
            "compaction_summary_text": compaction_summary_text,
            "compaction_summary_turn_id": compaction_summary_turn_id,
        },
    }


def action_from_payload(payload: dict[str, Any]) -> Action:
    action_type = str(payload.get("action_type") or "")
    if action_type == ActionType.TOOL_CALL.value:
        tool_calls = [
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                arguments=dict(item.get("arguments") or {}),
            )
            for item in payload.get("tool_calls", [])
            if isinstance(item, dict)
        ]
        return Action.from_tool_calls(tool_calls, metadata=dict(payload or {}))
    if action_type == ActionType.RESPONSE.value:
        return Action.from_response(
            str(payload.get("assistant_text") or ""),
            metadata=dict(payload or {}),
        )
    return Action.finish(metadata=dict(payload or {}))


def tool_calls_from_action_payload(
    payload: dict[str, Any],
) -> list[ToolCall]:
    return action_from_payload(payload).tool_calls


def apply_action_to_context_payload(
    self: "AgentRuntime",
    context_payload: dict[str, Any],
    action_payload: dict[str, Any],
) -> dict[str, Any]:
    context = deserialize_context(self, context_payload.get("context") or {})
    assistant_blocks = list(action_payload.get("assistant_blocks") or [])
    if assistant_blocks:
        context.add_message(
            MessageRole.ASSISTANT,
            assistant_blocks,
            stop_reason=action_payload.get("stop_reason"),
        )
    return {
        "context": serialize_context(context),
        "compaction": dict(context_payload.get("compaction") or {}),
    }


def result_payloads_to_context_results(
    payloads: list[dict[str, Any]],
) -> list[ContextToolResult]:
    results: list[ContextToolResult] = []
    for payload in payloads:
        result_payload = dict(payload.get("result") or {})
        result_metadata = dict(result_payload.get("metadata") or {})
        tool_name = payload.get("tool_name")
        if tool_name is not None:
            result_metadata["tool_name"] = str(tool_name)
        results.append(
            ContextToolResult(
                tool_call_id=str(payload.get("tool_call_id") or ""),
                content=str(result_payload.get("content") or ""),
                is_error=bool(result_payload.get("is_error")),
                metadata=result_metadata,
            )
        )
    return results


def observe_context_payload(
    self: "AgentRuntime",
    context_payload: dict[str, Any],
    action_payload: dict[str, Any],
    result_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    context = deserialize_context(self, context_payload.get("context") or {})
    action = action_from_payload(action_payload)
    results = result_payloads_to_context_results(result_payloads)
    observed = self.agent.observe(context, action, results)
    return {
        "context": serialize_context(observed),
        "compaction": dict(context_payload.get("compaction") or {}),
    }


def response_from_context_payload(
    self: "AgentRuntime",
    context_payload: dict[str, Any],
) -> Response:
    context = deserialize_context(self, context_payload.get("context") or {})
    context.mark_complete()
    return Response.from_context(context)


def serialize_context(context: Context) -> dict[str, Any]:
    return {
        "system_prompt": context.system_prompt,
        "messages": [
            {
                "role": message.role.value,
                "content": message.content,
                "metadata": dict(message.metadata or {}),
            }
            for message in context.messages
        ],
        "metadata": dict(context.metadata or {}),
        "signals": dict(context.signals or {}),
        "is_complete": bool(context.is_complete),
    }


def deserialize_context(self: "AgentRuntime", payload: dict[str, Any]) -> Context:
    context = Context(
        system_prompt=payload.get("system_prompt") or self.system_prompt,
        metadata=dict(payload.get("metadata") or {}),
        signals=dict(payload.get("signals") or {}),
        is_complete=bool(payload.get("is_complete")),
    )
    for item in payload.get("messages", []):
        if not isinstance(item, dict):
            continue
        role_text = str(item.get("role") or MessageRole.USER.value)
        try:
            role = MessageRole(role_text)
        except ValueError:
            role = MessageRole.USER
        context.messages.append(
            Message(
                role=role,
                content=item.get("content", ""),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return context

"""Command router that forwards input to commands or agent runtime."""

from __future__ import annotations

from typing import Optional

from .agent import AgentRuntime
from .commands import CommandBus
from .context import CLIContext
from .logging import EventLogger


class CommandRouter:
    """Routes user input to slash commands or the agent."""

    def __init__(
        self,
        command_bus: CommandBus,
        *,
        agent: Optional[AgentRuntime] = None,
        event_logger: Optional[EventLogger] = None,
    ) -> None:
        self._command_bus = command_bus
        self._agent = agent
        self._event_logger = event_logger

    def route(self, ctx: CLIContext, line: str) -> bool:
        handled = self._command_bus.try_execute(ctx, line)
        if handled:
            return True
        if self._agent is None:
            return False
        session_id = ctx.session_id
        if not ctx.agent_trace_id:
            ctx.renderer.warn("Agent trace id not initialized.")
            return True

        ctx.memory.record_conversation(
            self._agent.config.app_id,
            session_id,
            "user",
            line,
        )
        reply = self._agent.handle_message(
            session_id, ctx.agent_trace_id, line, ctx=ctx
        )
        if reply.text:
            ctx.renderer.markdown(reply.text)
            ctx.memory.record_conversation(
                self._agent.config.app_id,
                session_id,
                "assistant",
                reply.text,
            )
        return True

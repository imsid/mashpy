"""Composed CLI shell for Mash runtime engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from mash.core.context import Context, Response
from mash.runtime.client import MashAgentClient
from mash.runtime.definition import MashRuntimeDefinition
from mash.runtime.host import MashAgentHost
from mash.runtime.types import RuntimeTurnResult, SubAgentMetadata

from .chain_renderer import ChainOfThoughtRenderer
from .commands import Command, CommandRegistry
from .default_commands import register_default_commands
from .repl import REPL
from .render import RichRenderer
from .types import CLIContext


@dataclass(frozen=True)
class SubagentRegistration:
    """Subagent registration payload for host-backed shell composition."""

    definition: MashRuntimeDefinition
    metadata: SubAgentMetadata
    agent_id: str | None = None


class CLIAppShell:
    """Interactive shell backed by a host-managed primary agent client."""

    def __init__(self, host: MashAgentHost, primary_agent_id: str) -> None:
        self.host = host
        self.primary_agent_id = primary_agent_id
        self.client: MashAgentClient = host.get_client(primary_agent_id)
        self.app_id = self.client.app_id

        self.renderer = RichRenderer()
        self.chain_renderer = ChainOfThoughtRenderer(console=self.renderer.console)
        self.client.set_chain_renderer(self.chain_renderer)

        self.command_registry = CommandRegistry(
            app_id=self.client.app_id,
            event_logger=self.client.get_event_logger(),
            session_id=self.client.get_default_session_id(),
        )
        register_default_commands(self)

        self.context = CLIContext(
            app_id=self.client.app_id,
            session_id=self.client.get_default_session_id(),
            runtime=self.client,
            renderer=self.renderer,
        )

    @classmethod
    def from_definition(
        cls,
        definition: MashRuntimeDefinition,
        *,
        subagents: Sequence[SubagentRegistration] | None = None,
        bind_host: str = "127.0.0.1",
    ) -> CLIAppShell:
        """Build a host-backed CLI shell from app definition(s)."""
        host = MashAgentHost(bind_host=bind_host)
        primary_agent_id = host.register_primary(definition)
        for subagent in subagents or ():
            host.register_subagent(
                subagent.definition,
                agent_id=subagent.agent_id,
                metadata=subagent.metadata,
            )
        host.start()
        return cls(host, primary_agent_id)

    def register_command(self, command: Command) -> None:
        """Register a custom CLI command."""
        self.command_registry.register(command)

    def handle_message(
        self,
        message: str,
        session_id: str | None = None,
    ) -> RuntimeTurnResult:
        """Process one message via agent client and return structured turn output."""
        target_session_id = (session_id or self.context.session_id).strip()
        if not target_session_id:
            target_session_id = self.context.session_id
        payload = self.client.invoke(message, session_id=target_session_id)

        response_payload = payload.get("response")
        if isinstance(response_payload, dict):
            text = str(response_payload.get("text") or "")
            signals = response_payload.get("signals")
            metadata = response_payload.get("metadata")
        else:
            text = str(payload.get("text") or "")
            signals = payload.get("signals")
            metadata = payload.get("metadata")

        response = Response(
            text=text,
            context=Context(),
            signals=signals if isinstance(signals, dict) else {},
            metadata=metadata if isinstance(metadata, dict) else {},
        )
        session_value = str(payload.get("session_id") or target_session_id).strip()
        if not session_value:
            session_value = target_session_id

        session_total_tokens = payload.get("session_total_tokens", 0)
        try:
            parsed_session_total_tokens = int(session_total_tokens)
        except (TypeError, ValueError):
            parsed_session_total_tokens = 0

        compaction_summary_text = payload.get("compaction_summary_text")
        compaction_summary_turn_id = payload.get("compaction_summary_turn_id")
        return RuntimeTurnResult(
            session_id=session_value,
            response=response,
            compaction_summary_text=(
                compaction_summary_text if isinstance(compaction_summary_text, str) else None
            ),
            compaction_summary_turn_id=(
                compaction_summary_turn_id if isinstance(compaction_summary_turn_id, str) else None
            ),
            session_total_tokens=parsed_session_total_tokens,
        )

    def render_turn_result(self, ctx: CLIContext, result: RuntimeTurnResult) -> None:
        """Render runtime output to the terminal."""
        if result.compaction_summary_text:
            ctx.renderer.info("Compaction triggered - summary checkpoint created.")
            ctx.renderer.markdown(result.compaction_summary_text)
        if result.response.text:
            ctx.renderer.markdown(result.response.text)

    def handle_repl_message(self, ctx: CLIContext, message: str) -> None:
        """REPL callback for non-command user input."""
        result = self.handle_message(message, session_id=ctx.session_id)
        self.render_turn_result(ctx, result)

    def run(self) -> None:
        """Run the interactive application."""
        repl = REPL(
            app_id=self.app_id,
            command_registry=self.command_registry,
            message_handler=self.handle_repl_message,
        )

        try:
            repl.run(self.context)
        except KeyboardInterrupt:
            self.renderer.warn("\nBye.")
        except SystemExit:
            pass

    def shutdown(self) -> None:
        """Shutdown shell and runtime resources."""
        self.host.close()

"""Remote REPL shell for Mash host deployments."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from mash.runtime.events import (
    runtime_event_from_stream_payload,
    runtime_event_response_preview,
)

from .chain_renderer import ChainOfThoughtRenderer
from .client import MashHostClient
from .commands import Command, CommandRegistry
from .default_commands import register_default_commands
from .repl import REPL
from .render import RichRenderer
from .types import CLIContext


@dataclass(frozen=True)
class ShellTarget:
    """Resolved shell target for a remote deployment."""

    api_base_url: str
    agent_id: str
    session_id: str


class MashRemoteShell:
    """Interactive shell backed by a remote Mash host deployment."""

    def __init__(self, client: MashHostClient, target: ShellTarget) -> None:
        self.client = client
        self.target = target
        self.renderer = RichRenderer()
        self.chain_renderer = ChainOfThoughtRenderer(self.renderer.console)
        self.command_registry = CommandRegistry(
            app_id=self.target.agent_id,
            event_logger=None,
            session_id=self.target.session_id,
        )
        self.context = CLIContext(
            api_base_url=self.target.api_base_url,
            agent_id=self.target.agent_id,
            session_id=self.target.session_id,
            client=self.client,
            renderer=self.renderer,
            session_ids={self.target.agent_id: self.target.session_id},
        )
        register_default_commands(self)

    def register_command(self, command: Command) -> None:
        self.command_registry.register(command)

    @staticmethod
    def _build_llm_event(payload: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            event_type=payload.get("event_type"),
            trace_id=payload.get("trace_id"),
            provider=payload.get("provider"),
            model=payload.get("model"),
            duration_ms=payload.get("duration_ms"),
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            total_tokens=payload.get("total_tokens"),
            finish_reason=payload.get("finish_reason"),
            error=payload.get("error"),
            tools=payload.get("tools"),
            betas=payload.get("betas"),
        )

    def _render_runtime_trace_payload(
        self,
        payload: dict[str, Any],
        *,
        trace_label: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        event = runtime_event_from_stream_payload(
            payload,
            app_id=agent_id or self.target.agent_id,
            agent_id=agent_id or self.target.agent_id,
        )
        if event is None:
            return
        self.chain_renderer.on_runtime_event(event, trace_label=trace_label)

    def render_runtime_trace_payload(
        self,
        payload: dict[str, Any],
        *,
        trace_label: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._render_runtime_trace_payload(
            payload,
            trace_label=trace_label,
            agent_id=agent_id,
        )

    def _render_trace_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("event_type") or "")
        if event_type.startswith("subagent."):
            self._render_subagent_event(payload)
            return
        self._render_runtime_trace_payload(payload)
        if event_type == "llm.request.complete":
            self.chain_renderer.on_llm_request_complete(self._build_llm_event(payload))

    @staticmethod
    def _extract_streamed_response_text(
        payload: dict[str, Any],
        *,
        agent_id: str,
    ) -> str:
        event = runtime_event_from_stream_payload(
            payload,
            app_id=agent_id,
            agent_id=agent_id,
        )
        if event is None:
            return ""
        return runtime_event_response_preview(event)

    @staticmethod
    def extract_streamed_response_text(
        payload: dict[str, Any],
        *,
        agent_id: str,
    ) -> str:
        return MashRemoteShell._extract_streamed_response_text(
            payload,
            agent_id=agent_id,
        )

    def _render_subagent_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("event_type") or "")
        outer_payload = payload.get("payload")
        if not isinstance(outer_payload, dict):
            outer_payload = {}
        inner = outer_payload.get("payload")
        if isinstance(inner, dict) and "agent_id" in inner:
            outer_payload = inner
        agent_id = str(outer_payload.get("agent_id") or "subagent")
        nested = outer_payload.get("data")

        if event_type == "subagent.agent.trace" and isinstance(nested, dict):
            event = runtime_event_from_stream_payload(
                nested,
                app_id=agent_id,
                agent_id=agent_id,
            )
            if event is not None:
                self.chain_renderer.render_subagent_event(event, agent_id=agent_id)
            return

        if event_type == "subagent.request.started":
            return

        if event_type == "subagent.request.completed":
            duration_ms = 0
            if isinstance(nested, dict):
                duration_ms = int(nested.get("duration_ms") or 0)
            self.chain_renderer.finish_subagent(agent_id, duration_ms)
            return

        if event_type == "subagent.request.error":
            error_data = nested if isinstance(nested, dict) else {}
            error = error_data.get("error")
            self.renderer.error(f"    Subagent {agent_id} error: {error or 'request failed'}")

    def _handle_interaction(
        self, ctx: CLIContext, request_id: str, payload: dict[str, Any]
    ) -> None:
        self.chain_renderer.finish_trace()

        interaction_id = str(payload.get("interaction_id") or "")
        interaction_type = str(payload.get("type") or "info")
        prompt = str(payload.get("prompt") or "Input required:")
        schema = payload.get("schema")

        self.renderer.info(f"\n{prompt}")

        if interaction_type == "approval":
            options = ["approve", "deny", "skip"]
            self.renderer.info(f"  Options: {', '.join(options)}")
            user_input = input("  > ").strip().lower()
            if user_input not in options:
                user_input = "deny"
            response: Any = user_input

        elif interaction_type == "choice":
            options = []
            if isinstance(schema, dict):
                options = schema.get("options", [])
            for i, opt in enumerate(options, 1):
                self.renderer.info(f"  {i}. {opt}")
            self.renderer.info("  Enter numbers separated by commas:")
            user_input = input("  > ").strip()
            selected: list[str] = []
            for part in user_input.split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(options):
                        selected.append(options[idx])
                elif part in options:
                    selected.append(part)
            response = selected

        else:
            response = input("  > ").strip()

        self.client.post_interaction(
            ctx.agent_id,
            request_id,
            interaction_id=interaction_id,
            response=response,
        )

    def _render_interaction_ack(self, payload: dict[str, Any]) -> None:
        interaction_id = str(payload.get("interaction_id") or "")
        response = payload.get("response")
        timed_out = payload.get("timed_out", False)
        if timed_out:
            self.renderer.warn(f"  Interaction {interaction_id} timed out")
        else:
            if isinstance(response, list):
                display = ", ".join(str(item) for item in response)
            else:
                display = str(response)
            self.renderer.info(f"  Accepted: {display}")

    def handle_repl_message(self, ctx: CLIContext, message: str) -> None:
        request_id = self.client.submit_request(
            ctx.agent_id,
            message=message,
            session_id=ctx.session_id,
        )
        final_payload: dict[str, Any] | None = None
        streamed_response_text: str | None = None
        try:
            for event in self.client.stream_request(ctx.agent_id, request_id):
                event_name = str(event.get("event") or "")
                payload = event.get("data")
                if not isinstance(payload, dict):
                    continue

                if event_name == "agent.trace":
                    self._render_trace_event(payload)
                    streamed_text = self._extract_streamed_response_text(
                        payload,
                        agent_id=ctx.agent_id,
                    )
                    if streamed_text and not self.chain_renderer.response_streamed():
                        # Legacy per-step preview render, used only when the
                        # provider does not stream tokens. When tokens stream
                        # live, the answer is already shown formatted in place.
                        streamed_response_text = streamed_text
                        ctx.renderer.markdown(streamed_text)
                    continue

                if event_name == "request.interaction.create":
                    self._handle_interaction(ctx, request_id, payload)
                    continue

                if event_name == "request.interaction.ack":
                    self._render_interaction_ack(payload)
                    continue

                if event_name == "request.completed":
                    final_payload = payload
                    break

                if event_name == "request.error":
                    error = payload.get("error")
                    raise RuntimeError(str(error or "remote request failed"))
        finally:
            self.chain_renderer.finish_trace()

        if final_payload is None:
            raise RuntimeError("stream ended without a terminal event")

        final_session_id = str(final_payload.get("session_id") or "").strip()
        if final_session_id:
            ctx.session_id = final_session_id
            ctx.session_ids[ctx.agent_id] = final_session_id

        response_payload = final_payload.get("response")
        if isinstance(response_payload, dict):
            text = str(response_payload.get("text") or "")
        else:
            text = str(final_payload.get("text") or "")
        # Skip the terminal render when the answer already streamed live; only
        # render here for non-streaming providers (and dedupe against any
        # legacy preview render above).
        if (
            text
            and text != streamed_response_text
            and not self.chain_renderer.take_response_streamed()
        ):
            ctx.renderer.markdown(text)

    def run(self) -> None:
        repl = REPL(
            app_id=f"{self.context.agent_id}@remote",
            command_registry=self.command_registry,
            message_handler=self.handle_repl_message,
        )
        try:
            repl.run(self.context)
        except KeyboardInterrupt:
            self.renderer.warn("\nBye.")
        except SystemExit:
            pass

    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

"""Remote REPL shell for Mash host deployments."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

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
    def _build_trace_event(payload: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            event_type=payload.get("event_type"),
            trace_id=payload.get("trace_id"),
            step_id=payload.get("step_id"),
            duration_ms=payload.get("duration_ms"),
            action_type=payload.get("action_type"),
            tool_calls=payload.get("tool_calls"),
            token_usage=payload.get("token_usage"),
            payload=payload.get("payload") or {},
        )

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

    @staticmethod
    def _normalize_runtime_trace_payload(payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("event_type") or "")
        nested = payload.get("payload")
        if not isinstance(nested, dict):
            return payload
        if event_type == "runtime.llm.think.completed":
            tool_calls_detail = nested.get("tool_calls")
            tool_calls = []
            if isinstance(tool_calls_detail, list):
                for item in tool_calls_detail:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if name:
                        tool_calls.append(name)
            return {
                "event_type": "agent.think.complete",
                "trace_id": payload.get("trace_id"),
                "step_id": payload.get("loop_index"),
                "duration_ms": nested.get("duration_ms"),
                "action_type": nested.get("action_type"),
                "tool_calls": tool_calls,
                "token_usage": nested.get("token_usage"),
                "payload": {
                    "assistant_text": nested.get("assistant_text"),
                    "tool_calls_detail": (
                        tool_calls_detail if isinstance(tool_calls_detail, list) else None
                    ),
                },
            }
        if event_type in {
            "runtime.tool.call.completed",
            "runtime.subagent.call.completed",
        }:
            tool_name = str(nested.get("tool_name") or "").strip()
            return {
                "event_type": "agent.act.complete",
                "trace_id": payload.get("trace_id"),
                "step_id": payload.get("loop_index"),
                "duration_ms": nested.get("duration_ms"),
                "action_type": "tool_call",
                "tool_calls": [tool_name] if tool_name else [],
                "payload": {},
            }
        return payload

    def _render_trace_event(self, payload: dict[str, Any]) -> None:
        payload = self._normalize_runtime_trace_payload(payload)
        event_type = str(payload.get("event_type") or "")
        if event_type.startswith("subagent."):
            self._render_subagent_event(payload)
            return
        if event_type == "agent.think.complete":
            # Skip EventLogger-sourced duplicates: those have action_type nested in
            # payload.payload, not at top level. runtime.llm.think.completed events
            # (which normalize to agent.think.complete) always set action_type at top level.
            if not payload.get("action_type"):
                return
            self.chain_renderer.on_think_complete(self._build_trace_event(payload))
            return
        if event_type == "agent.act.complete":
            self.chain_renderer.on_act_complete(self._build_trace_event(payload))
            return
        if event_type == "agent.step.complete":
            self.chain_renderer.on_step_complete(self._build_trace_event(payload))
            return
        if event_type == "llm.request.complete":
            self.chain_renderer.on_llm_request_complete(self._build_llm_event(payload))

    @staticmethod
    def _extract_streamed_response_text(payload: dict[str, Any]) -> str:
        payload = MashRemoteShell._normalize_runtime_trace_payload(payload)
        event_type = str(payload.get("event_type") or "")
        if event_type != "agent.think.complete":
            return ""
        action_type = str(payload.get("action_type") or "")
        if action_type != "response":
            return ""
        nested = payload.get("payload")
        if not isinstance(nested, dict):
            return ""
        return str(nested.get("assistant_text") or "").strip()

    def _render_subagent_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("event_type") or "")
        outer_payload = payload.get("payload")
        if not isinstance(outer_payload, dict):
            outer_payload = {}
        agent_id = str(outer_payload.get("agent_id") or "subagent")
        nested = outer_payload.get("data")
        trace_label = f"Subagent {agent_id}"

        if event_type == "subagent.agent.trace" and isinstance(nested, dict):
            nested_payload = dict(nested)
            child_payload = dict(nested_payload.get("payload") or {})
            child_payload["trace_label"] = trace_label
            nested_payload["payload"] = child_payload
            self._render_trace_event(nested_payload)
            return

        if event_type == "subagent.request.started":
            self.renderer.info(f"{trace_label} started")
            return

        if event_type == "subagent.request.completed":
            self.renderer.info(f"{trace_label} completed")
            return

        if event_type == "subagent.request.error":
            error_payload = nested if isinstance(nested, dict) else {}
            error = error_payload.get("error")
            self.renderer.error(f"{trace_label} error: {error or 'request failed'}")

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
                    streamed_text = self._extract_streamed_response_text(payload)
                    if streamed_text:
                        streamed_response_text = streamed_text
                        ctx.renderer.markdown(streamed_text)
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
        if text and text != streamed_response_text:
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

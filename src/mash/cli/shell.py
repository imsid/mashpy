"""Remote REPL shell for Mash host deployments."""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.markup import escape

from mash.runtime.events import (
    RuntimeEventType,
    runtime_event_from_stream_payload,
    runtime_event_response_preview,
)

from .background import (
    BackgroundWatchers,
    MovedToBackground,
    Notice,
    ctrl_z_moves_to_background,
    list_rows,
    resolve_prefix,
    short_id,
)
from .chain_renderer import ChainOfThoughtRenderer
from .client import MashHostClient, MashHostClientError
from .command_log import RemoteCommandEventLogger
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
    user_id: str
    host_id: str | None = None


class MashRemoteShell:
    """Interactive shell backed by a remote Mash host deployment."""

    def __init__(
        self,
        client: MashHostClient,
        target: ShellTarget,
        *,
        show_thinking: bool = False,
    ) -> None:
        self.client = client
        self.target = target
        self.renderer = RichRenderer()
        self.chain_renderer = ChainOfThoughtRenderer(
            self.renderer.console, show_thinking=show_thinking
        )
        self.command_registry = CommandRegistry(
            app_id=self.target.agent_id,
            event_logger=RemoteCommandEventLogger(
                client,
                agent_id=self.target.agent_id,
                host_id=self.target.host_id,
            ),
            session_id=self.target.session_id,
        )
        self.context = CLIContext(
            api_base_url=self.target.api_base_url,
            agent_id=self.target.agent_id,
            session_id=self.target.session_id,
            user_id=self.target.user_id,
            client=self.client,
            renderer=self.renderer,
            session_ids={self.target.agent_id: self.target.session_id},
            host_id=self.target.host_id,
        )
        self._workflow_result_renderers: dict[str, Callable[[dict[str, Any]], None]] = {}
        # Notices from watcher threads wait while a stream is being read, so
        # they never land in the middle of the request on screen.
        self._notice_lock = threading.Lock()
        self._following = False
        self._pending_notices: list[Notice] = []
        self.watchers = BackgroundWatchers(self._watcher_client, self.notify)
        register_default_commands(self)

    def _watcher_client(self) -> Any:
        copy = getattr(self.client, "copy", None)
        return copy() if callable(copy) else self.client

    def _render_notice(self, notice: Notice) -> None:
        # Escaped: Rich would read a short id like "[a1b2c3d4]" as markup.
        for kind, text in notice:
            if kind == "markdown":
                self.renderer.markdown(text)
            elif kind == "warn":
                self.renderer.warn(escape(text))
            elif kind == "error":
                self.renderer.error(escape(text))
            else:
                self.renderer.info(escape(text))

    def notify(self, notice: Notice) -> None:
        """Print a watcher's notice now, or once the current stream read ends."""
        with self._notice_lock:
            if self._following:
                self._pending_notices.append(notice)
                return
        self._render_notice(notice)

    def _set_following(self, following: bool) -> None:
        with self._notice_lock:
            self._following = following
            pending = [] if following else self._pending_notices
            if not following:
                self._pending_notices = []
        for notice in pending:
            self._render_notice(notice)

    def refresh_watchers(self, ctx: CLIContext) -> None:
        """Watch the session's running bg requests the user started."""
        try:
            records = self.client.list_session_requests(
                ctx.agent_id,
                ctx.session_id,
                mode="bg",
                status="running",
                started_by="user",
            )
        except Exception as exc:
            self.renderer.warn(f"Could not list background requests: {exc}")
            return
        self.watchers.refresh(records)

    def register_command(self, command: Command) -> None:
        self.command_registry.register(command)

    def register_workflow_result_renderer(
        self,
        workflow_id: str,
        fn: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a renderer for a completed workflow result."""
        self._workflow_result_renderers[workflow_id] = fn

    def render_final_response(
        self,
        ctx: CLIContext,
        response_payload: Any,
        fallback_text: str,
        streamed_text: str,
    ) -> None:
        """Render the assistant's final turn from assistant_blocks or text.

        Skips any text block whose content was already shown live via delta
        streaming (streamed_text). Thinking is not rendered here: the chain
        renderer shows each step's thinking with that step. Falls back to
        fallback_text when response_payload carries no blocks.
        """
        if isinstance(response_payload, dict):
            assistant_blocks = response_payload.get("assistant_blocks") or []
            text = str(response_payload.get("text") or "")
        else:
            assistant_blocks = []
            text = fallback_text

        rendered = False
        if assistant_blocks:
            for block in assistant_blocks:
                if block.get("type") == "text":
                    block_text = str(block.get("text") or "").strip()
                    if block_text and block_text != streamed_text:
                        ctx.renderer.markdown(block_text)
                        rendered = True
        elif text and text != streamed_text:
            ctx.renderer.markdown(text)
            rendered = True

        if not rendered and not streamed_text:
            ctx.renderer.warn("(no response)")

    def render_workflow_result(
        self,
        workflow_id: str,
        data: dict[str, Any],
    ) -> None:
        """Render a workflow result using a registered renderer or JSON fallback."""
        fn = self._workflow_result_renderers.get(workflow_id)
        if fn is not None:
            fn(data)
        else:
            self.renderer.markdown(
                f"```json\n{json.dumps(data, indent=2)}\n```"
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
        self._render_runtime_trace_payload(payload)
        if event_type == RuntimeEventType.EXECUTE_TOOL_START.value:
            nested = payload.get("payload")
            subagents = nested.get("subagents") if isinstance(nested, dict) else None
            if isinstance(subagents, list):
                self._follow_subagents(subagents)

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

    def _follow_subagents(self, subagents: list[Any]) -> None:
        """Show each RunSubAgents child's steps from its own stream.

        The parent is blocked on the call while its children run, so the
        children are read one after another, each from its first event; a
        child that finished while an earlier one was shown replays in full.
        """
        for item in subagents:
            if not isinstance(item, dict):
                continue
            agent_id = str(item.get("agent_id") or "").strip()
            request_id = str(item.get("request_id") or "").strip()
            if agent_id and request_id:
                self._follow_subagent(agent_id, request_id)

    def _follow_subagent(self, agent_id: str, request_id: str) -> None:
        started_at = time.monotonic()
        try:
            for event in self.client.stream_request(agent_id, request_id):
                event_name = str(event.get("event") or "")
                data = event.get("data")
                if not isinstance(data, dict):
                    continue
                if event_name == "agent.trace":
                    child_event = runtime_event_from_stream_payload(
                        data, app_id=agent_id, agent_id=agent_id
                    )
                    if child_event is not None:
                        self.chain_renderer.render_subagent_event(
                            child_event, agent_id=agent_id
                        )
                    continue
                if event_name == "request.error":
                    error = data.get("error")
                    self.renderer.error(
                        f"    Subagent {agent_id} error: {error or 'request failed'}"
                    )
                    return
                if event_name in {"request.completed", "request.cancelled"}:
                    break
        except MashHostClientError as exc:
            self.renderer.error(f"    Subagent {agent_id}: {exc}")
            return
        self.chain_renderer.finish_subagent(
            agent_id, int((time.monotonic() - started_at) * 1000)
        )

    def handle_interaction(
        self,
        ctx: CLIContext,
        request_id: str,
        payload: dict[str, Any],
        agent_id: str | None = None,
    ) -> None:
        self.chain_renderer.finish_trace()

        interaction_id = str(payload.get("interaction_id") or "")
        interaction_type = str(payload.get("type") or "info")
        prompt = str(payload.get("prompt") or "Input required:")
        schema = payload.get("schema")

        self.renderer.info(f"\n{prompt}")

        if interaction_type == "browser":
            url = str(payload.get("url") or "")
            self.renderer.link(f"  Open: {url}")
            with contextlib.suppress(Exception):
                webbrowser.open(url)
            self.renderer.info('  Press Enter when you\'re done (or type "cancel")')
            user_input = input("  > ").strip().lower()
            response: Any = "cancel" if user_input == "cancel" else "done"

        elif interaction_type == "approval":
            options = ["approve", "deny", "skip"]
            self.renderer.info(f"  Options: {', '.join(options)}")
            user_input = input("  > ").strip().lower()
            if user_input not in options:
                user_input = "deny"
            response = user_input

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
            agent_id or ctx.agent_id,
            request_id,
            interaction_id=interaction_id,
            response=response,
        )

    def render_interaction_ack(self, payload: dict[str, Any]) -> None:
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

    def submit_message(self, ctx: CLIContext, message: str, *, mode: str = "fg") -> str:
        """Submit a message to the pinned host or agent; returns the request id."""
        if ctx.host_id:
            accepted = self.client.submit_host_request(
                ctx.host_id,
                message=message,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                mode=mode,
            )
            request_id = str(accepted.get("request_id") or "")
            accepted_agent_id = str(accepted.get("agent_id") or "").strip()
            if accepted_agent_id:
                ctx.agent_id = accepted_agent_id
        else:
            request_id = self.client.submit_request(
                ctx.agent_id,
                message=message,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                mode=mode,
            )
        ctx.last_request_id = request_id or None
        return request_id

    def handle_repl_message(self, ctx: CLIContext, message: str) -> None:
        request_id = self.submit_message(ctx, message)
        try:
            self.follow_request(ctx, ctx.agent_id, request_id)
        finally:
            self.refresh_watchers(ctx)

    def follow_request(
        self,
        ctx: CLIContext,
        agent_id: str,
        request_id: str,
        *,
        child: bool = False,
    ) -> str:
        """Read a request's stream from its first event until it ends.

        Answers its interactions and renders its final response. Ctrl-Z stops
        reading and sends the request to the background; a child, whose mode
        belongs to the agent that started it, is only left to run. Returns
        ``completed``, ``cancelled`` or ``backgrounded``.
        """
        final_payload: dict[str, Any] | None = None
        cancelled = False
        moved = False
        self._set_following(True)
        try:
            with ctrl_z_moves_to_background():
                for event in self.client.stream_request(agent_id, request_id):
                    event_name = str(event.get("event") or "")
                    payload = event.get("data")
                    if not isinstance(payload, dict):
                        continue

                    if event_name == "agent.trace":
                        self._render_trace_event(payload)
                        continue

                    if event_name == "request.interaction.create":
                        self.handle_interaction(ctx, request_id, payload, agent_id=agent_id)
                        continue

                    if event_name == "request.interaction.ack":
                        self.render_interaction_ack(payload)
                        continue

                    if event_name == "request.completed":
                        final_payload = payload
                        break

                    if event_name == "request.cancelled":
                        cancelled = True
                        break

                    if event_name == "request.error":
                        error = payload.get("error")
                        raise RuntimeError(str(error or "remote request failed"))
        except MovedToBackground:
            moved = True
        finally:
            self.chain_renderer.finish_trace()
            self._set_following(False)

        if moved:
            sid = short_id(request_id)
            if child:
                self.renderer.info(escape(f"[{sid}] keeps running under its parent"))
                return "backgrounded"
            try:
                self.client.set_request_mode(agent_id, request_id, "bg")
            except MashHostClientError as exc:
                self.renderer.warn(
                    escape(f"[{sid}] could not move to the background: {exc}")
                )
            self.renderer.info(escape(f"[{sid}] moved to the background"))
            self.watchers.watch(agent_id, request_id)
            return "backgrounded"

        if cancelled:
            # A cancelled request is a terminal outcome someone asked for, so
            # the shell reports it and returns to the prompt.
            self.renderer.warn("  Request cancelled.")
            return "cancelled"

        if final_payload is None:
            raise RuntimeError("stream ended without a terminal event")

        final_session_id = str(final_payload.get("session_id") or "").strip()
        if final_session_id:
            ctx.session_id = final_session_id
            ctx.session_ids[agent_id] = final_session_id

        chain_streamed = self.chain_renderer.take_streamed_text()
        response_obj = final_payload.get("response")
        fallback = str(final_payload.get("text") or "")
        self.render_final_response(ctx, response_obj, fallback, chain_streamed)
        return "completed"

    def start_background(self, ctx: CLIContext, message: str) -> None:
        """``/bg <message>``: submit in the background and return to the prompt."""
        request_id = self.submit_message(ctx, message, mode="bg")
        self.renderer.info(escape(f"[{short_id(request_id)}] running in the background"))
        self.watchers.watch(ctx.agent_id, request_id)

    def _background_records(self, ctx: CLIContext) -> list[dict[str, Any]]:
        return self.client.list_session_requests(ctx.agent_id, ctx.session_id, mode="bg")

    def _resolve(self, ctx: CLIContext, prefix: str) -> dict[str, Any] | None:
        resolved = resolve_prefix(prefix, self._background_records(ctx))
        if isinstance(resolved, str):
            self.renderer.error(escape(resolved))
            return None
        return resolved

    def list_background(self, ctx: CLIContext) -> None:
        """``/bg --list``: the session's bg requests, children under parents."""
        records = self._background_records(ctx)
        if not records:
            self.renderer.warn("No background requests in this session.")
            return
        rows = [[escape(cell) for cell in row] for row in list_rows(records)]
        self.renderer.table(["ID", "Message", "Status", "Started by"], rows)

    def switch_to(self, ctx: CLIContext, prefix: str) -> None:
        """``/bg --switch <id>``: bring a request to the foreground and follow it."""
        record = self._resolve(ctx, prefix)
        if record is None:
            return
        request_id = str(record["request_id"])
        agent_id = str(record.get("agent_id") or ctx.agent_id)
        child = bool(record.get("parent_request_id"))
        if not child and record.get("status") == "running":
            try:
                self.client.set_request_mode(agent_id, request_id, "fg")
            except MashHostClientError:
                # It finished in the meantime; replaying still shows it.
                pass
        self.watchers.silence(request_id)
        try:
            self.follow_request(ctx, agent_id, request_id, child=child)
        finally:
            self.refresh_watchers(ctx)

    def kill(self, ctx: CLIContext, prefix: str) -> None:
        """``/bg --kill <id>``: cancel a request and its children."""
        record = self._resolve(ctx, prefix)
        if record is None:
            return
        request_id = str(record["request_id"])
        self.watchers.silence(request_id)
        self.client.cancel_request(str(record.get("agent_id") or ctx.agent_id), request_id)
        self.renderer.warn(escape(f"[{short_id(request_id)}] cancelled"))


    def run(self) -> None:
        # A restarted REPL picks up the bg requests still running.
        self.refresh_watchers(self.context)
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

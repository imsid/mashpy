"""The interactive REPL for a Mash deployment.

One asyncio loop runs everything. The input stays open while requests run:
each request the REPL follows is a task that reads its event stream, updates a
``RequestView`` and prints what finished above the input. prompt_toolkit draws
the live rows, the input and the status bar; rich prints finished output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
import uuid
import webbrowser
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markup import escape

from .background import BackgroundWatchers, Notice, list_rows, resolve_prefix, short_id
from .client import MashHostClient, MashHostClientError
from .command_log import RemoteCommandEventLogger
from .commands import Command, CommandRegistry, ExitRepl
from .default_commands import register_default_commands
from .input import PROMPT, InputLine, MenuEntry, history_path
from .render import (
    Fragments,
    OutputPrinter,
    RichRenderer,
    ToolLine,
    fmt_tokens,
    live_rows,
    prompt_style_rules,
    status_bar,
)
from .types import CLIContext
from .views import (
    CANCELLED,
    COMPLETED,
    FAILED,
    Ack,
    Children,
    End,
    Interaction,
    Output,
    RequestView,
    final_text,
    reduce,
)

# How long the REPL waits for a RunSubAgents child to be submitted.
CHILD_SUBMIT_WAIT_SECONDS = 30.0
# How long a finished request waits for its children's streams to end.
CHILD_DRAIN_SECONDS = 5.0
# A second Ctrl-C within this many seconds of the first leaves the REPL.
EXIT_CONFIRM_SECONDS = 2.0
REFRESH_SECONDS = 0.1


@dataclass(frozen=True)
class Target:
    """What the REPL talks to: an agent, or a host through its companion."""

    api_base_url: str
    agent_id: str
    session_id: str
    user_id: str
    host_id: str | None = None


@dataclass(frozen=True)
class _Interrupted:
    """The input was closed so the loop can ask something; ``draft`` was typed."""

    draft: str


class Repl:
    """Interactive session with a Mash deployment."""

    def __init__(
        self,
        client: MashHostClient,
        target: Target,
        *,
        debug: bool = False,
        app_name: str = "mash",
        theme: Mapping[str, str] | None = None,
        tool_lines: Mapping[str, ToolLine] | None = None,
        console: Console | None = None,
        history: History | Path | None = None,
        input: Any = None,
        output: Any = None,
    ) -> None:
        self.client = client
        self.target = target
        self.app_name = app_name
        # The detail level: each model turn, tool results and children's tools.
        self.detail = debug
        self.tool_lines = tool_lines
        self.renderer = RichRenderer(console, theme=theme)
        self.printer = OutputPrinter(
            self.renderer, detail=lambda: self.detail, tool_lines=tool_lines
        )
        self.command_registry = CommandRegistry(
            app_id=target.agent_id,
            event_logger=RemoteCommandEventLogger(
                client, agent_id=target.agent_id, host_id=target.host_id
            ),
            session_id=target.session_id,
        )
        self.context = CLIContext(
            api_base_url=target.api_base_url,
            agent_id=target.agent_id,
            session_id=target.session_id,
            user_id=target.user_id,
            client=client,
            renderer=self.renderer,
            session_ids={target.agent_id: target.session_id},
            host_id=target.host_id,
        )
        self._workflow_result_renderers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self.watchers = BackgroundWatchers(client, self.notify)
        # The request whose rows are in the live area, and the task reading it.
        self.foreground: RequestView | None = None
        self._foreground_task: asyncio.Task[None] | None = None
        # A request brought forward with /bg --switch that another agent
        # started: Ctrl-Z leaves its mode alone.
        self._foreground_is_child = False
        self._child_tasks: set[asyncio.Task[None]] = set()
        # Messages typed while a request ran, sent in order when it ends.
        self.queue: list[str] = []
        # Interactions waiting for the user, oldest first.
        self._interactions: deque[Interaction] = deque()
        self._draft = ""
        self._exit_armed_at: float | None = None
        self._flash: tuple[str, float] | None = None
        self._session_tokens: int | None = None
        self._side_tasks: set[asyncio.Task[Any]] = set()
        self._style = Style.from_dict(prompt_style_rules(theme))
        # The input is opened when the REPL runs, so a Repl can be built (to
        # register commands, or in tests) without a terminal.
        self._history = history
        self._terminal = {"input": input, "output": output}
        self._input: InputLine | None = None
        self._ask_session_: PromptSession[str] | None = None
        register_default_commands(self)

    @property
    def input(self) -> InputLine:
        """The input line, opened on first use."""
        if self._input is None:
            self._input = InputLine(
                history=self._history or history_path(self.app_name),
                commands=self._command_menu,
                key_bindings=self._key_bindings(),
                **self._terminal,
            )
        return self._input

    @property
    def _ask_session(self) -> PromptSession[str]:
        """Where an interaction's answer is typed.

        A session of its own, so answers stay out of the message history.
        """
        if self._ask_session_ is None:
            self._ask_session_ = PromptSession(**self._terminal)
        return self._ask_session_

    # --- small helpers ---------------------------------------------------

    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def now() -> float:
        return time.monotonic()

    def register_command(self, command: Command) -> None:
        self.command_registry.register(command)

    def register_workflow_result_renderer(
        self, workflow_id: str, fn: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a renderer for a completed workflow result."""
        self._workflow_result_renderers[workflow_id] = fn

    def render_workflow_result(self, workflow_id: str, data: dict[str, Any]) -> None:
        """Render a workflow result using a registered renderer or JSON fallback."""
        fn = self._workflow_result_renderers.get(workflow_id)
        if fn is not None:
            fn(data)
        else:
            self.renderer.markdown(f"```json\n{json.dumps(data, indent=2)}\n```")

    def _command_menu(self) -> list[MenuEntry]:
        return [
            (command.name, command.help)
            for command in self.command_registry.list_commands()
        ]

    def running(self) -> bool:
        """Whether a foreground request is running."""
        return self.foreground is not None and not self.foreground.ended

    def idle(self) -> bool:
        """Nothing in the foreground, nothing queued, nothing to ask."""
        return (
            self._foreground_task is None
            and not self.queue
            and not self._interactions
        )

    def flash(self, text: str, seconds: float = EXIT_CONFIRM_SECONDS) -> None:
        """Show ``text`` in the status bar for a moment."""
        self._flash = (text, self.now() + seconds)
        self._invalidate()

    def _invalidate(self) -> None:
        if self._input is None:
            return
        app = self._input.session.app
        if app.is_running:
            app.invalidate()

    # --- notices from background watchers --------------------------------

    def notify(self, notice: Notice) -> None:
        """Print a watcher's notice above the input."""
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

    async def refresh_watchers(self) -> None:
        """Watch the session's running bg requests the user started."""
        ctx = self.context
        try:
            records = await asyncio.to_thread(
                self.client.list_session_requests,
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

    # --- the live area and status bar -------------------------------------

    def _width(self) -> int:
        try:
            return get_app().output.get_size().columns
        except Exception:
            return 80

    def _message(self) -> Fragments:
        rows = live_rows(
            self.foreground,
            self.now(),
            self._width(),
            queued=self.queue,
            tool_lines=self.tool_lines,
        )
        return rows + [("class:prompt", PROMPT)]

    def _toolbar(self) -> Fragments:
        ctx = self.context
        parts = [ctx.host_id or ctx.agent_id]
        if self._flash is not None and self._flash[1] > self.now():
            parts.append(self._flash[0])
        else:
            self._flash = None
            background = len(self.watchers.watching())
            if background:
                parts.append(f"{background} bg")
            if self._session_tokens:
                parts.append(f"{fmt_tokens(self._session_tokens)} tokens")
            if self.detail:
                parts.append("detail")
            if self.running():
                parts.append("ctrl-c to cancel")
                parts.append("ctrl-z to background")
            else:
                parts.append("/help")
        return status_bar(parts, self._width())

    # --- keys ---------------------------------------------------------------

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        running = Condition(self.running)
        empty = Condition(lambda: not get_app().current_buffer.text)

        @bindings.add("c-c")
        def _interrupt(event: KeyPressEvent) -> None:
            buffer = event.current_buffer
            if buffer.text:
                buffer.reset()
                self._exit_armed_at = None
                return
            if self.running():
                self.cancel_foreground()
                return
            armed = self._exit_armed_at
            if armed is not None and self.now() - armed <= EXIT_CONFIRM_SECONDS:
                event.app.exit(exception=EOFError())
                return
            self._exit_armed_at = self.now()
            self.flash("press ctrl-c again to exit")

        @bindings.add("escape", filter=running)
        def _cancel(event: KeyPressEvent) -> None:
            del event
            self.cancel_foreground()

        @bindings.add("c-z", filter=running)
        def _background(event: KeyPressEvent) -> None:
            del event
            self._spawn(self.move_to_background())

        @bindings.add("c-o")
        def _detail(event: KeyPressEvent) -> None:
            del event
            self.detail = not self.detail
            self.flash("detail on" if self.detail else "detail off")

        @bindings.add("c-l")
        def _clear(event: KeyPressEvent) -> None:
            event.app.renderer.clear()

        @bindings.add("up", filter=empty & Condition(lambda: bool(self.queue)))
        def _edit_queued(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text(self.queue.pop())

        return bindings

    def _spawn(self, coroutine: Any) -> None:
        """Run a coroutine in the background; an error it raises is printed."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._side_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._side_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self.renderer.error(f"Error: {done.exception()}")

        task.add_done_callback(_done)

    # --- submitting and following requests --------------------------------

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

    async def send(self, message: str) -> None:
        """Send a message now, or queue it behind the running request."""
        if self._foreground_task is not None:
            self.queue.append(message)
            self._invalidate()
            return
        await self._start(message)

    async def _start(self, message: str) -> None:
        ctx = self.context
        request_id = await asyncio.to_thread(self.submit_message, ctx, message)
        view = RequestView(
            agent_id=ctx.agent_id,
            request_id=request_id,
            started_at=self.now(),
            brief=message,
        )
        self._follow_in_foreground(view)

    def _follow_in_foreground(
        self, view: RequestView, *, after: int = 0, child: bool = False
    ) -> None:
        self.foreground = view
        self._foreground_is_child = child
        self._foreground_task = asyncio.get_running_loop().create_task(
            self._follow(view, after=after)
        )
        self._invalidate()

    async def _follow(self, view: RequestView, *, after: int = 0) -> None:
        """Read a request's stream to its end, then print how it ended."""
        try:
            await self._read_stream(view, after=after)
            await self._drain_children()
        except asyncio.CancelledError:
            # Ctrl-Z or leaving the REPL: the request itself keeps running.
            self._cancel_children()
            raise
        except Exception as exc:
            view.status = FAILED
            view.error = str(exc)
            view.ended_at = self.now()
            self._cancel_children()
        self._print_end(view)
        self._drop_interactions(view)
        final_session_id = (view.session_id or "").strip()
        if final_session_id and view.status == COMPLETED:
            self.context.session_id = final_session_id
            self.context.session_ids[view.agent_id] = final_session_id
        self._invalidate()
        await self.refresh_watchers()
        await self._refresh_session_tokens()
        # Cleared last, so the REPL is not idle while the lines above run.
        queued = self.queue.pop(0) if self.queue else None
        self._foreground_task = None
        self._invalidate()
        if queued is not None:
            try:
                await self._start(queued)
            except Exception as exc:
                self.renderer.error(f"Error: {exc}")

    async def _refresh_session_tokens(self) -> None:
        """Read the session's token total for the status bar; best effort."""
        ctx = self.context
        try:
            info = await asyncio.to_thread(
                self.client.get_session, ctx.agent_id, ctx.session_id
            )
        except Exception:
            return
        total = info.get("total_tokens")
        if total is None:
            total = info.get("session_total_tokens")
        if isinstance(total, int):
            self._session_tokens = total

    async def _read_stream(self, view: RequestView, *, after: int = 0) -> None:
        async for event in self.client.request_events(
            view.agent_id, view.request_id, after=after
        ):
            for output in reduce(view, event, self.now()):
                self._handle(output)
            self._invalidate()
        if not view.ended:
            raise MashHostClientError("the stream ended before the request did")

    def _handle(self, output: Output) -> None:
        """Print an output, and act on the ones that need more than printing."""
        if isinstance(output, Interaction):
            # A child's own interaction only changes its row: the companion
            # relays it, and that copy is the one the user answers.
            if output.view.parent is None:
                self._interactions.append(output)
                self._wake_input()
            return
        if isinstance(output, Ack):
            if output.view.parent is None:
                self._acknowledged(output)
            return
        if isinstance(output, End) and output.view.parent is None:
            return
        self.printer.print(output, self.now())
        if isinstance(output, Children):
            for child in output.children:
                task = asyncio.get_running_loop().create_task(self._follow_child(child))
                self._child_tasks.add(task)
                task.add_done_callback(self._child_tasks.discard)

    def _print_end(self, view: RequestView) -> None:
        if view.status == COMPLETED:
            text = final_text(view)
            if text:
                self.renderer.markdown(text)
            elif not view.response_streamed.strip():
                self.renderer.warn("(no response)")
            self.printer.summary(view, self.now())
        elif view.status == CANCELLED:
            self.renderer.warn("Request cancelled.")
        else:
            self.renderer.error(f"✗ {view.error or 'request failed'}")
        self.renderer.print()

    # --- children ----------------------------------------------------------

    async def _wait_for_child(self, child: RequestView) -> bool:
        """Wait until the child exists: its id is listed before it is submitted."""
        deadline = self.now() + CHILD_SUBMIT_WAIT_SECONDS
        while True:
            try:
                await asyncio.to_thread(
                    self.client.get_request_status, child.agent_id, child.request_id
                )
                return True
            except MashHostClientError:
                if self.now() >= deadline:
                    return False
                await asyncio.sleep(0.25)

    async def _follow_child(self, child: RequestView) -> None:
        """Read one child's stream; its rows and lines show its progress."""
        try:
            if not await self._wait_for_child(child):
                raise MashHostClientError(f"request {child.request_id} never started")
            await self._read_stream(child)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One child's broken stream must not end the parent's.
            if not child.ended:
                child.status = FAILED
                child.error = str(exc)
                child.ended_at = self.now()
                self.printer.child_end(child, self.now())
        self._invalidate()

    async def _drain_children(self) -> None:
        """Let the children's streams reach their end, briefly."""
        tasks = list(self._child_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=CHILD_DRAIN_SECONDS)
        for task in pending:
            task.cancel()

    def _cancel_children(self) -> None:
        for task in list(self._child_tasks):
            task.cancel()

    # --- cancel and background ---------------------------------------------

    def cancel_foreground(self) -> None:
        """Ask the host to cancel the foreground request."""
        view = self.foreground
        if view is None or view.ended:
            return
        self.flash("cancelling…")
        self._spawn(
            asyncio.to_thread(self.client.cancel_request, view.agent_id, view.request_id)
        )

    async def move_to_background(self) -> None:
        """Ctrl-Z: stop following the foreground request and send it to ``bg``."""
        view, task = self.foreground, self._foreground_task
        if view is None or task is None or view.ended:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.foreground = None
        self._foreground_task = None
        self._drop_interactions(view)
        sid = short_id(view.request_id)
        if self._foreground_is_child:
            self.renderer.info(escape(f"[{sid}] keeps running under its parent"))
        else:
            try:
                await asyncio.to_thread(
                    self.client.set_request_mode, view.agent_id, view.request_id, "bg"
                )
            except MashHostClientError as exc:
                self.renderer.warn(
                    escape(f"[{sid}] could not move to the background: {exc}")
                )
            else:
                self.renderer.info(escape(f"[{sid}] moved to the background"))
            self.watchers.watch(view.agent_id, view.request_id, after=view.cursor)
        self._invalidate()
        if self.queue:
            await self._start(self.queue.pop(0))

    async def start_background(self, ctx: CLIContext, message: str) -> None:
        """``/bg <message>``: submit in the background and return to the prompt."""
        request_id = await asyncio.to_thread(
            lambda: self.submit_message(ctx, message, mode="bg")
        )
        self.renderer.info(escape(f"[{short_id(request_id)}] running in the background"))
        self.watchers.watch(ctx.agent_id, request_id)

    async def _background_records(self, ctx: CLIContext) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            lambda: self.client.list_session_requests(
                ctx.agent_id, ctx.session_id, mode="bg"
            )
        )

    async def _resolve(self, ctx: CLIContext, prefix: str) -> dict[str, Any] | None:
        resolved = resolve_prefix(prefix, await self._background_records(ctx))
        if isinstance(resolved, str):
            self.renderer.error(escape(resolved))
            return None
        return resolved

    async def list_background(self, ctx: CLIContext) -> None:
        """``/bg --list``: the session's bg requests, children under parents."""
        records = await self._background_records(ctx)
        if not records:
            self.renderer.warn("No background requests in this session.")
            return
        rows = [[escape(cell) for cell in row] for row in list_rows(records)]
        self.renderer.table(["ID", "Message", "Status", "Started by"], rows)

    async def switch_to(self, ctx: CLIContext, prefix: str) -> None:
        """``/bg --switch <id>``: bring a request to the foreground and follow it."""
        if self._foreground_task is not None:
            self.renderer.error(
                "A request is running in the foreground. Press Ctrl-Z to send it "
                "to the background first."
            )
            return
        record = await self._resolve(ctx, prefix)
        if record is None:
            return
        request_id = str(record["request_id"])
        agent_id = str(record.get("agent_id") or ctx.agent_id)
        child = bool(record.get("parent_request_id"))
        if not child and record.get("status") == "running":
            try:
                await asyncio.to_thread(
                    self.client.set_request_mode, agent_id, request_id, "fg"
                )
            except MashHostClientError:
                # It finished in the meantime; replaying still shows it.
                pass
        self.watchers.silence(request_id)
        view = RequestView(
            agent_id=agent_id,
            request_id=request_id,
            started_at=self.now(),
            brief=str(record.get("message") or ""),
        )
        self._follow_in_foreground(view, child=child)

    async def kill(self, ctx: CLIContext, prefix: str) -> None:
        """``/bg --kill <id>``: cancel a request and its children."""
        record = await self._resolve(ctx, prefix)
        if record is None:
            return
        request_id = str(record["request_id"])
        self.watchers.silence(request_id)
        await asyncio.to_thread(
            self.client.cancel_request,
            str(record.get("agent_id") or ctx.agent_id),
            request_id,
        )
        self.renderer.warn(escape(f"[{short_id(request_id)}] cancelled"))

    # --- interactions --------------------------------------------------------

    def _wake_input(self) -> None:
        """Close the open input so the loop can ask the waiting interaction."""
        if self._input is None:
            return
        app = self._input.session.app
        if app.is_running and not app.is_done:
            with contextlib.suppress(Exception):
                app.exit(result=_Interrupted(app.current_buffer.text))  # type: ignore[arg-type]

    def _pre_run(self) -> None:
        # An interaction that arrived between the loop's check and the input
        # opening: close the input again right away.
        if self._interactions:
            self._wake_input()

    def _drop_interactions(self, view: RequestView) -> None:
        """Forget interactions of a request the REPL stopped following."""
        self._interactions = deque(
            item for item in self._interactions if item.view is not view
        )

    def _acknowledged(self, ack: Ack) -> None:
        """An interaction closed: drop it if still unasked, report a timeout."""
        interaction_id = str(ack.payload.get("interaction_id") or "")
        self._interactions = deque(
            item
            for item in self._interactions
            if str(item.payload.get("interaction_id") or "") != interaction_id
        )
        if ack.payload.get("timed_out"):
            self.renderer.warn(f"  Interaction {interaction_id} timed out")

    async def _ask_line(self, prompt: str = "  › ") -> str:
        return (await self._ask_session.prompt_async(prompt)).strip()

    async def _ask(self, interaction: Interaction) -> Any:
        """Ask the user one interaction; return the response to post."""
        payload = interaction.payload
        interaction_type = str(payload.get("type") or "info")
        prompt = str(payload.get("prompt") or "Input required:")
        schema = payload.get("schema")
        # A relayed interaction names the agent that asked.
        origin = payload.get("origin")
        if isinstance(origin, dict) and origin.get("agent_id"):
            prompt = f"[{origin['agent_id']}] {prompt}"
        self.renderer.info(escape(prompt))

        if interaction_type == "browser":
            url = str(payload.get("url") or "")
            self.renderer.link(f"  Open: {url}")
            with contextlib.suppress(Exception):
                webbrowser.open(url)
            self.renderer.info('  Press Enter when you\'re done (or type "cancel")')
            return "cancel" if (await self._ask_line()).lower() == "cancel" else "done"

        if interaction_type == "approval":
            options = ["approve", "deny", "skip"]
            while True:
                self.renderer.info(f"  Options: {', '.join(options)}")
                answer = (await self._ask_line()).lower()
                if answer in options:
                    return answer

        if interaction_type == "choice":
            options = list(schema.get("options") or []) if isinstance(schema, dict) else []
            for index, option in enumerate(options, 1):
                self.renderer.info(f"  {index}. {option}")
            while True:
                self.renderer.info("  Enter numbers separated by commas:")
                selected = _parse_choices(await self._ask_line(), options)
                if selected is not None:
                    return selected

        return await self._ask_line()

    async def _answer_next(self) -> None:
        """Ask the oldest waiting interaction and post the answer."""
        interaction = self._interactions.popleft()
        view = interaction.view
        try:
            response = await self._ask(interaction)
        except (EOFError, KeyboardInterrupt):
            # Left unanswered: the request keeps waiting until it times out.
            self.renderer.warn("  Left unanswered.")
            return
        try:
            await asyncio.to_thread(
                lambda: self.client.post_interaction(
                    view.agent_id,
                    view.request_id,
                    interaction_id=str(interaction.payload.get("interaction_id") or ""),
                    response=response,
                )
            )
        except MashHostClientError as exc:
            self.renderer.warn(f"  The answer was not accepted: {exc}")

    # --- the loop ------------------------------------------------------------

    async def _read(self) -> str | None:
        """One message from the input, or None when the loop took it back."""
        draft, self._draft = self._draft, ""
        result = await self.input.session.prompt_async(
            self._message,
            bottom_toolbar=self._toolbar,
            style=self._style,
            refresh_interval=REFRESH_SECONDS,
            default=draft,
            pre_run=self._pre_run,
        )
        if isinstance(result, _Interrupted):
            self._draft = result.draft
            return None
        return self.input.finish(result)

    @contextlib.contextmanager
    def _sigint_cancels(self, task: asyncio.Task[Any]) -> Iterator[None]:
        """While a command runs outside the input, Ctrl-C cancels that command."""
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            yield
            return
        try:
            yield
        finally:
            loop.remove_signal_handler(signal.SIGINT)

    async def run_command(self, line: str) -> None:
        """Run one ``/command`` line."""
        task = asyncio.ensure_future(self.command_registry.execute(self.context, line))
        with self._sigint_cancels(task):
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                self.renderer.warn("Interrupted.")

    def _banner(self) -> None:
        ctx = self.context
        target = f"host {ctx.host_id}" if ctx.host_id else f"agent {ctx.agent_id}"
        self.renderer.muted(
            f"{self.app_name} · {ctx.user_id} · {target} · /help for commands"
        )

    async def run_async(self) -> None:
        """Run the REPL until the user leaves."""
        with patch_stdout(raw=True):
            self._banner()
            # A restarted REPL picks up the bg requests still running.
            await self.refresh_watchers()
            try:
                while True:
                    if self._interactions:
                        await self._answer_next()
                        continue
                    try:
                        text = await self._read()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not text:
                        continue
                    self._exit_armed_at = None
                    try:
                        if text.startswith("/"):
                            await self.run_command(text)
                        else:
                            await self.send(text)
                    except ExitRepl:
                        break
                    except Exception as exc:
                        self.renderer.error(f"Error: {exc}")
            finally:
                await self._shutdown()

    async def _shutdown(self) -> None:
        view = self.foreground
        if view is not None and not view.ended and self._foreground_task is not None:
            self.renderer.muted(
                escape(f"[{short_id(view.request_id)}] is still running on the host.")
            )
        tasks = [self._foreground_task, *self._child_tasks, *self._side_tasks]
        tasks = [task for task in tasks if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.watchers.close()
        self.renderer.muted("Bye.")

    def run(self) -> None:
        """Run the REPL on a new event loop."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            pass


def _parse_choices(text: str, options: list[Any]) -> list[Any] | None:
    """The options named by comma-separated numbers or names; None if any is wrong."""
    selected: list[Any] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and 0 < int(part) <= len(options):
            selected.append(options[int(part) - 1])
        elif part in options:
            selected.append(part)
        else:
            return None
    return selected or None

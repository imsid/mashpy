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
import re
import signal
import sys
import time
import uuid
import webbrowser
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.clipboard import Clipboard
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markup import escape

from .ask import menu
from .client import MashHostClient, MashHostClientError
from .command_log import RemoteCommandEventLogger
from .commands import CLIContext, Command, CommandRegistry, ExitRepl
from .input import CONTINUATION, PROMPT, InputLine, MenuEntry, history_path
from .render import (
    Fragments,
    OutputPrinter,
    RichRenderer,
    ToolLine,
    clip,
    fmt_seconds,
    fmt_tokens,
    live_rows,
    prompt_style_rules,
    status_bar,
    tool_display,
)
from .sessions import PICKER_HINT, PICKER_LIMIT, session_options, session_title
from .slash import register_default_commands
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
    Tool,
    ToolCall,
    Workflow,
    answer_text,
    final_text,
    reduce,
)

# How long the REPL waits for a RunSubAgents child to be submitted.
CHILD_SUBMIT_WAIT_SECONDS = 30.0
# How long the REPL looks for the run a RunWorkflow call started.
WORKFLOW_RUN_WAIT_SECONDS = 30.0
# How long a finished request waits for its children's streams to end.
CHILD_DRAIN_SECONDS = 5.0
# A second Ctrl-C within this many seconds of the first leaves the REPL.
EXIT_CONFIRM_SECONDS = 2.0
REFRESH_SECONDS = 0.1
# At most this many redraws a second, however fast text streams.
MAX_REDRAWS_PER_SECOND = 30


@dataclass(frozen=True)
class Target:
    """What the REPL talks to: an agent, or a host through its companion."""

    api_base_url: str
    agent_id: str
    session_id: str
    user_id: str
    host_id: str | None = None
    # The session has earlier turns: the REPL starts by showing the last two.
    resumed: bool = False
    # The session's first message, for the header line.
    title: str = ""


@dataclass(frozen=True)
class _Interrupted:
    """The input was closed so the loop can ask something; ``draft`` was typed."""

    draft: str


# A fenced code block's body.
_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# The first argument each built-in command offers in the menu.
ARGUMENT_MENUS: dict[str, tuple[MenuEntry, ...]] = {
    "workflow": (
        ("list", "The workflows you can run"),
        ("run", "Start a run: run <workflow_id> [--input JSON]"),
        ("status", "A run's status: status <workflow_id> <run_id>"),
        ("resume", "Resume a failed run: resume <workflow_id> <run_id>"),
    ),
    "memory": (
        ("add", "Pin a fact: add <text>"),
        ("forget", "Remove an entry: forget <id>"),
    ),
    "browser": (("forget", "Sign out of a site: forget <site>"),),
    "copy": (("code", "The last code block of the last answer"),),
}

# An open question was closed because its interaction no longer waits.
_CLOSED: Any = object()
# The approval menu's entry that prints the call's full arguments.
_VIEW_CALL: Any = object()


def _excerpt(markdown: str, limit: int = 600) -> str:
    """The start of an answer, cut at a line end, for a resumed session's recap."""
    text = markdown.strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    head = text[: cut if cut > 0 else limit].rstrip()
    if head.count("```") % 2:
        head += "\n```"
    return head + "\n\n…"


def _merge(fragments: Any) -> Fragments:
    """Fragments with runs of one style joined.

    The ANSI parser gives one fragment per character, and prompt_toolkit lays
    out each one on every redraw: a long block slowed the stream down.
    """
    merged: Fragments = []
    for fragment in fragments:
        style, text = fragment[0], fragment[1]
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


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
        clipboard: Clipboard | None = None,
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
            agent_id=target.agent_id,
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
        # The request whose rows are in the live area, and the task reading it.
        self.foreground: RequestView | None = None
        self._foreground_task: asyncio.Task[None] | None = None
        self._child_tasks: set[asyncio.Task[None]] = set()
        # RunWorkflow call id -> the task following that call's run.
        self._workflow_tasks: dict[str, asyncio.Task[None]] = {}
        # The last request that started children, for /subagent.
        self.last_parent: RequestView | None = None
        # The last answer printed in the foreground, for /copy.
        self.last_answer = ""
        # Messages typed while a request ran, sent in order when it ends.
        self.queue: list[str] = []
        # Interactions waiting for the user, oldest first.
        self._interactions: deque[Interaction] = deque()
        # The interaction being asked and the application asking it.
        self._asking: tuple[Interaction, Application[Any]] | None = None
        self._draft = ""
        self._title = target.title
        self._resumed = target.resumed
        self._exit_armed_at: float | None = None
        self._flash: tuple[str, float] | None = None
        self._session_tokens: int | None = None
        self._side_tasks: set[asyncio.Task[Any]] = set()
        # Sent messages that hold pastes -> what the input showed for them.
        self._typed: dict[str, str] = {}
        # The live area's last drawing of the unfinished block, and its key.
        self._streaming_cache: tuple[tuple[Any, ...], Fragments] | None = None
        self._style = Style.from_dict(prompt_style_rules(theme))
        # The input is opened when the REPL runs, so a Repl can be built (to
        # register commands, or in tests) without a terminal.
        self._history = history
        self._clipboard = clipboard
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
                arguments=self._argument_menu,
                clipboard=self._clipboard,
                key_bindings=self._key_bindings(),
                **self._terminal,
            )
            app = self._input.session.app
            # A sent line leaves the input; the REPL prints it when it is
            # sent (a queued message, when its turn comes), so the live rows
            # above the input never land in the scrollback with it.
            app.erase_when_done = True
            app.min_redraw_interval = 1 / MAX_REDRAWS_PER_SECOND
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

    def _argument_menu(self, command: str, typed: Sequence[str]) -> list[MenuEntry]:
        """The menu for a command's next argument."""
        if not typed:
            if command == "subagent":
                children = self.last_parent.children if self.last_parent else []
                return [
                    (str(index), f"{child.agent_id}: {clip(child.brief, 50)}")
                    for index, child in enumerate(children, 1)
                ] + [("cancel", "Cancel one subagent")]
            return list(ARGUMENT_MENUS.get(command, ()))
        if command == "subagent" and list(typed) == ["cancel"]:
            children = self.last_parent.children if self.last_parent else []
            return [
                (str(index), child.agent_id)
                for index, child in enumerate(children, 1)
                if not child.ended
            ]
        return []

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

    # --- the live area and status bar -------------------------------------

    def _width(self) -> int:
        try:
            return get_app().output.get_size().columns
        except Exception:
            return 80

    def _streaming(self, width: int) -> Fragments:
        """The answer's unfinished block, drawn as it will print."""
        view = self.foreground
        if view is None or view.ended or not view.tail.strip():
            return []
        try:
            height = get_app().output.get_size().rows
        except Exception:
            height = 24
        # Leave room for the rows, the input and the status bar.
        room = max(3, height - 8 - len(view.children))
        gap = self.renderer.gap_before_markdown()
        key = (view.tail, width, room, gap)
        if self._streaming_cache is not None and self._streaming_cache[0] == key:
            return self._streaming_cache[1]
        rendered = self.renderer.render_markdown(view.tail, width).rstrip("\n")
        lines = rendered.split("\n")
        if len(lines) > room:
            lines = lines[-room:]
        text = ("\n" if gap else "") + "\n".join(lines) + "\n"
        fragments = _merge(to_formatted_text(ANSI(text)))
        self._streaming_cache = (key, fragments)
        return fragments

    def _message(self) -> Fragments:
        try:
            if get_app().is_done:
                return []
        except Exception:
            pass
        width = self._width()
        rows = self._streaming(width) + live_rows(
            self.foreground,
            self.now(),
            width,
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
            if self._session_tokens:
                parts.append(f"{fmt_tokens(self._session_tokens)} tokens")
            if self.detail:
                parts.append("detail")
            if self.running():
                parts.append("ctrl-c to cancel")
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

    def submit_message(self, ctx: CLIContext, message: str) -> str:
        """Submit a message to the pinned host or agent; returns the request id."""
        if ctx.host_id:
            accepted = self.client.submit_host_request(
                ctx.host_id,
                message=message,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
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
        self.renderer.echo(self._typed.pop(message, message), PROMPT, CONTINUATION)
        request_id = await asyncio.to_thread(self.submit_message, ctx, message)
        view = RequestView(
            agent_id=ctx.agent_id,
            request_id=request_id,
            started_at=self.now(),
            brief=message,
        )
        self._follow_in_foreground(view)

    def _follow_in_foreground(self, view: RequestView, *, after: int = 0) -> None:
        self.foreground = view
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
            # Leaving the REPL: the request itself keeps running.
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
        if isinstance(output, Workflow):
            self._workflow_tasks[output.call.call_id] = self._child_task(
                self._follow_workflow(output)
            )
            return
        self.printer.print(output, self.now())
        if isinstance(output, Tool):
            # The call ended, so its run needs no more following.
            task = self._workflow_tasks.pop(output.call.call_id, None)
            if task is not None:
                task.cancel()
        if isinstance(output, Children):
            if output.view.parent is None:
                self.last_parent = output.view
            for child in output.children:
                self._child_task(self._follow_child(child))

    def _child_task(self, coroutine: Any) -> asyncio.Task[None]:
        """A task that lives and dies with the foreground request."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._child_tasks.add(task)
        task.add_done_callback(self._child_tasks.discard)
        return task

    def _print_end(self, view: RequestView) -> None:
        if view.status == COMPLETED:
            if view is self.foreground:
                self.last_answer = answer_text(view)
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

    async def _find_workflow_run(self, workflow: Workflow) -> str | None:
        """The id of the run a RunWorkflow call started, found by its dedup key."""
        deadline = self.now() + WORKFLOW_RUN_WAIT_SECONDS
        while self.now() < deadline:
            runs = await asyncio.to_thread(
                self.client.list_workflow_runs, workflow.workflow_id
            )
            for run in runs:
                if run.get("dedup_key") == workflow.dedup_key and run.get("run_id"):
                    return str(run["run_id"])
            await asyncio.sleep(0.5)
        return None

    async def _follow_workflow(self, workflow: Workflow) -> None:
        """Show the step a RunWorkflow call's run is on; best effort.

        The call's own result says how the run ended, so a failure to find or
        read the run only leaves the row without a step.
        """
        call = workflow.call
        try:
            run_id = await self._find_workflow_run(workflow)
            if run_id is None:
                return
            async for event in self.client.workflow_run_events(
                workflow.workflow_id, run_id
            ):
                name = str(event.get("event") or "")
                data = event.get("data")
                step_id = str(data.get("step_id") or "") if isinstance(data, dict) else ""
                if name == "step.started":
                    call.note = f"step {step_id}"
                elif name in ("step.completed", "step.failed"):
                    ok = name == "step.completed"
                    call.note = f"step {step_id} {'done' if ok else 'failed'}"
                    if self.detail:
                        mark = "[ok]✓[/ok]" if ok else "[error]✗[/error]"
                        self.renderer.print(
                            f"  [muted]{escape(workflow.workflow_id)} │[/muted] {mark} "
                            f"[muted]step {escape(step_id)}[/muted]"
                        )
                elif name in ("workflow.completed", "workflow.error"):
                    return
                self._invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

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

    # --- /retry, /compact, /copy -----------------------------------------------

    def _foreground_busy(self) -> bool:
        if self._foreground_task is None:
            return False
        self.renderer.error(
            "A request is running. Wait for it, or press Ctrl-C to cancel it."
        )
        return True

    async def retry(self) -> None:
        """``/retry``: run the last request again as a new request."""
        ctx = self.context
        if self._foreground_busy():
            return
        if not ctx.last_request_id:
            self.renderer.warn("Nothing to retry yet.")
            return
        accepted = await asyncio.to_thread(
            self.client.rerun_request, ctx.agent_id, ctx.last_request_id
        )
        request_id = str(accepted.get("request_id") or "")
        if not request_id:
            raise MashHostClientError("the host returned no request id")
        ctx.last_request_id = request_id
        self._follow_in_foreground(
            RequestView(
                agent_id=ctx.agent_id,
                request_id=request_id,
                started_at=self.now(),
                brief="retry",
            )
        )

    async def compact(self) -> None:
        """``/compact``: have the agent summarize the session's history."""
        ctx = self.context
        if self._foreground_busy():
            return
        result = await asyncio.to_thread(
            self.client.compact_session, ctx.agent_id, ctx.session_id
        )
        summary = str(result.get("summary_text") or "").strip()
        if not summary:
            self.renderer.warn("Nothing to summarize yet.")
            return
        self.renderer.info("Session summarized. Later requests start from:")
        self.renderer.muted(escape(clip(summary, 600)))
        await self._refresh_session_tokens()

    def copy_last(self, *, code: bool = False) -> None:
        """``/copy``: the last answer, or its last code block, to the clipboard."""
        text = self.last_answer
        if not text.strip():
            self.renderer.warn("No answer to copy yet.")
            return
        what = "the last answer"
        if code:
            blocks = _CODE_BLOCK.findall(text)
            if not blocks:
                self.renderer.warn("The last answer has no code block.")
                return
            text, what = blocks[-1].rstrip("\n"), "the last code block"
        clipboard = self.input.session.clipboard
        clipboard.set_text(text)
        if getattr(clipboard, "system", True):
            self.renderer.info(f"Copied {what} ({len(text)} characters).")
        else:
            self.renderer.warn(
                "No system clipboard here. Ctrl-Y pastes "
                f"{what} into the input."
            )

    # --- /subagent ------------------------------------------------------------

    def _subagent(self, number: str) -> RequestView | None:
        """Child ``number`` (from 1) of the last request that started children."""
        children = self.last_parent.children if self.last_parent is not None else []
        if not children:
            self.renderer.warn("No request in this REPL has started subagents yet.")
            return None
        if not number.isdigit() or not 0 < int(number) <= len(children):
            self.renderer.error(
                f"Give a number from 1 to {len(children)}; /subagent lists them."
            )
            return None
        return children[int(number) - 1]

    def list_subagents(self) -> None:
        """``/subagent``: the children of the last request that started any."""
        parent = self.last_parent
        if parent is None or not parent.children:
            self.renderer.warn("No request in this REPL has started subagents yet.")
            return
        now = self.now()
        rows = [
            [
                str(index),
                escape(child.agent_id) + (" ↺" if child.continued else ""),
                child.status,
                str(child.tool_count),
                fmt_tokens(child.tokens),
                fmt_seconds(child.elapsed(now)),
                escape(clip(" ".join(child.brief.split()), 60)),
            ]
            for index, child in enumerate(parent.children, 1)
        ]
        self.renderer.table(
            ["#", "Agent", "Status", "Tools", "Tokens", "Time", "Asked"], rows
        )

    async def show_subagent(self, number: str) -> None:
        """``/subagent <n>``: what a child was asked, what it did and its answer.

        The child's stream is read again from its start. A child still
        running is followed to its end; Ctrl-C stops reading.
        """
        child = self._subagent(number)
        if child is None:
            return
        self.renderer.info(f"[agent]{escape(child.agent_id)}[/agent] was asked:")
        self.renderer.markdown(child.brief or "(nothing)")
        view = RequestView(
            agent_id=child.agent_id,
            request_id=child.request_id,
            started_at=self.now(),
            brief=child.brief,
        )
        printer = OutputPrinter(
            self.renderer, detail=lambda: True, tool_lines=self.tool_lines
        )
        async for event in self.client.request_events(child.agent_id, child.request_id):
            for output in reduce(view, event, self.now()):
                # Its interactions were asked through the parent, and its own
                # children are not followed here.
                if not isinstance(output, (Interaction, Ack, Children, Workflow, End)):
                    printer.print(output, self.now())
        self._print_end(view)

    async def cancel_subagent(self, number: str) -> None:
        """``/subagent cancel <n>``: cancel one child; the others go on."""
        child = self._subagent(number)
        if child is None:
            return
        if child.ended:
            self.renderer.warn(f"{child.agent_id} already ended ({child.status}).")
            return
        await asyncio.to_thread(
            self.client.cancel_request, child.agent_id, child.request_id
        )
        self.renderer.warn(f"Cancelling {escape(child.agent_id)}.")

    # --- cancel ---------------------------------------------------------------

    def cancel_foreground(self) -> None:
        """Ask the host to cancel the foreground request."""
        view = self.foreground
        if view is None or view.ended:
            return
        self.flash("cancelling…")
        self._spawn(
            asyncio.to_thread(self.client.cancel_request, view.agent_id, view.request_id)
        )

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
        if self._asking is not None and self._asking[0].view is view:
            self._close_ask()

    def _acknowledged(self, ack: Ack) -> None:
        """An interaction closed: drop it if still unasked, report a timeout."""
        interaction_id = str(ack.payload.get("interaction_id") or "")
        self._interactions = deque(
            item
            for item in self._interactions
            if str(item.payload.get("interaction_id") or "") != interaction_id
        )
        asking = self._asking
        if asking is not None and asking[0].view is ack.view and (
            str(asking[0].payload.get("interaction_id") or "") == interaction_id
        ):
            # Answered somewhere else, or timed out, while its menu was open.
            self._close_ask()
        if ack.payload.get("timed_out"):
            self.renderer.warn(f"  Interaction {interaction_id} timed out")

    def _close_ask(self) -> None:
        """Close the open question: its interaction no longer waits."""
        if self._asking is None:
            return
        app = self._asking[1]
        if app is not None and app.is_running and not app.is_done:
            with contextlib.suppress(Exception):
                app.exit(result=_CLOSED)

    async def _run_ask(self, interaction: Interaction, app: Application[Any]) -> Any:
        """Run one question's application; ``_CLOSED`` when it was closed for it."""
        self._asking = (interaction, app)
        try:
            return await app.run_async()
        finally:
            self._asking = None

    async def _choose(
        self,
        interaction: Interaction,
        options: list[tuple[Any, str]],
        *,
        multiple: bool = False,
    ) -> Any:
        return await self._run_ask(
            interaction,
            menu(options, multiple=multiple, style=self._style, **self._terminal),
        )

    async def _ask_line(self, interaction: Interaction) -> Any:
        session = self._ask_session
        self._asking = (interaction, session.app)
        try:
            answer = await session.prompt_async("  › ")
        finally:
            self._asking = None
        return answer if answer is _CLOSED else str(answer).strip()

    def _approval_calls(self, interaction: Interaction) -> list[ToolCall]:
        """The tool calls an approval is about, from the turn that asked.

        A relayed approval is about a child's calls. Only calls the prompt
        names are returned: other calls of the turn need no approval.
        """
        view = interaction.view
        origin = interaction.payload.get("origin")
        if isinstance(origin, dict):
            request_id = str(origin.get("request_id") or "")
            view = next(
                (child for child in view.children if child.request_id == request_id),
                None,
            )
            if view is None:
                return []
        prompt = str(interaction.payload.get("prompt") or "")
        return [
            ToolCall(
                call_id=call["id"],
                name=call["name"],
                arguments=call["arguments"],
                started_at=self.now(),
            )
            for call in view.pending_tool_calls
            if call["name"] in prompt
        ]

    async def _ask(self, interaction: Interaction) -> Any:
        """Ask the user one interaction; return the response to post."""
        payload = interaction.payload
        interaction_type = str(payload.get("type") or "info")
        prompt = str(payload.get("prompt") or "Input required:")
        schema = payload.get("schema")
        options = list(schema.get("options") or []) if isinstance(schema, dict) else []
        # A relayed interaction names the agent that asked.
        origin = payload.get("origin")
        if isinstance(origin, dict) and origin.get("agent_id"):
            prompt = f"[{origin['agent_id']}] {prompt}"
        if self._interactions:
            prompt = f"{prompt} · {len(self._interactions)} more waiting"
        self.renderer.section(escape(prompt))

        if interaction_type == "browser":
            url = str(payload.get("url") or "")
            self.renderer.link(f"  Open: {url}")
            with contextlib.suppress(Exception):
                webbrowser.open(url)
            return await self._choose(
                interaction,
                [("done", "Done, continue"), ("cancel", "Cancel the sign-in")],
            )

        if interaction_type == "approval":
            calls = self._approval_calls(interaction)
            for call in calls:
                self.renderer.muted(
                    "    " + escape(tool_display(call, self.tool_lines))
                )
            choices: list[tuple[Any, str]] = [
                ("approve", "Approve"),
                ("deny", "Deny"),
                ("skip", "Skip"),
            ]
            if calls:
                choices.append((_VIEW_CALL, "View the full call"))
            while True:
                answer = await self._choose(interaction, choices)
                if answer is not _VIEW_CALL:
                    return answer
                for call in calls:
                    self.renderer.markdown(
                        f"```json\n{json.dumps({call.name: call.arguments}, indent=2, default=str)}\n```"
                    )

        if interaction_type == "choice" and options:
            multiple = not isinstance(schema, dict) or schema.get("type") != "enum"
            return await self._choose(
                interaction,
                [(option, str(option)) for option in options],
                multiple=multiple,
            )

        return await self._ask_line(interaction)

    async def _answer_next(self) -> None:
        """Ask the oldest waiting interaction and post the answer.

        Ctrl-C cancels the request that asked. Ctrl-D raises ``EOFError``.
        """
        interaction = self._interactions.popleft()
        view = interaction.view
        try:
            response = await self._ask(interaction)
        except KeyboardInterrupt:
            self.renderer.warn("  Cancelling the request.")
            try:
                await asyncio.to_thread(
                    self.client.cancel_request, view.agent_id, view.request_id
                )
            except MashHostClientError as exc:
                self.renderer.warn(f"  Could not cancel: {exc}")
            return
        if response is _CLOSED:
            self.renderer.muted("  No longer waiting for an answer.")
            return
        shown = ", ".join(map(str, response)) if isinstance(response, list) else str(response)
        self.renderer.muted("  → " + escape(shown))
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
        message = self.input.finish(result)
        # The message is printed as typed: a collapsed paste stays collapsed.
        if message and result.strip() != message:
            self._typed[message] = result.strip()
        return message

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
        title = f" · {escape(clip(self._title, 40))}" if self._title else ""
        self.renderer.muted(
            f"{self.app_name} · {ctx.user_id} · {target}{title} · /help for commands"
        )

    # --- sessions -------------------------------------------------------------

    async def _recap(self) -> None:
        """Print the session's last two turns, dimmed."""
        ctx = self.context
        try:
            turns = await asyncio.to_thread(
                lambda: self.client.get_history(ctx.agent_id, ctx.session_id, limit=2)
            )
        except MashHostClientError as exc:
            self.renderer.warn(f"Could not read the session's history: {exc}")
            return
        for turn in turns:
            self.renderer.echo(
                clip(str(turn.get("user_message") or ""), 200), PROMPT, CONTINUATION
            )
            self.renderer.markdown(_excerpt(str(turn.get("agent_response") or "")), dim=True)
        if turns:
            self.renderer.print()

    async def switch_session(
        self, session_id: str, *, resumed: bool, title: str = ""
    ) -> bool:
        """Go on in another session, or a new one, without leaving the REPL."""
        if self._foreground_busy():
            return False
        ctx = self.context
        ctx.session_id = session_id
        ctx.session_ids[ctx.agent_id] = session_id
        ctx.last_request_id = None
        self._title = title
        self._session_tokens = None
        self.last_parent = None
        self.foreground = None
        if resumed:
            self._banner()
            await self._recap()
            await self._refresh_session_tokens()
        else:
            self.renderer.info("New session.")
        self._invalidate()
        return True

    async def pick_session(self) -> None:
        """``/resume``: choose one of the user's sessions and go on in it."""
        ctx = self.context
        sessions = await asyncio.to_thread(
            lambda: self.client.list_sessions(
                ctx.agent_id, user_id=ctx.user_id, limit=PICKER_LIMIT
            )
        )
        if not sessions:
            self.renderer.warn("No earlier sessions.")
            return
        app = menu(
            session_options(sessions, current=ctx.session_id),
            hint=PICKER_HINT,
            style=self._style,
            **self._terminal,
        )
        try:
            session = await app.run_async()
        except KeyboardInterrupt:
            self.renderer.muted("Kept the current session.")
            return
        await self.switch_session(
            str(session["session_id"]), resumed=True, title=session_title(session)
        )

    def _piped(self) -> bool:
        """Whether messages come from a pipe or a file, not a terminal."""
        if self._terminal["input"] is not None:
            return False
        try:
            return not sys.stdin.isatty()
        except (AttributeError, ValueError):
            return True

    async def _run_piped(self) -> None:
        """Read one message or command per line of stdin, each run to its end.

        There is no input line to draw, so nothing is live: output is printed
        as it finishes. Nobody is there to answer an interaction, so a request
        that asks one is cancelled.
        """
        loop = asyncio.get_running_loop()
        self._banner()
        if self._resumed:
            await self._recap()
        try:
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                text = line.strip()
                if not text:
                    continue
                try:
                    if text.startswith("/"):
                        self.renderer.echo(text, PROMPT, CONTINUATION)
                        await self.run_command(text)
                    else:
                        await self.send(text)
                except ExitRepl:
                    break
                except Exception as exc:
                    self.renderer.error(f"Error: {exc}")
                while self._foreground_task is not None or self.queue or self._interactions:
                    while self._interactions:
                        interaction = self._interactions.popleft()
                        view = interaction.view
                        self.renderer.info(escape(str(interaction.payload.get("prompt") or "")))
                        self.renderer.warn(
                            "  Input is not a terminal, so this cannot be answered. "
                            "Cancelling the request."
                        )
                        with contextlib.suppress(MashHostClientError):
                            await asyncio.to_thread(
                                self.client.cancel_request, view.agent_id, view.request_id
                            )
                    await asyncio.sleep(0.02)
        finally:
            await self._shutdown()

    async def run_async(self) -> None:
        """Run the REPL until the user leaves."""
        if self._piped():
            await self._run_piped()
            return
        with patch_stdout(raw=True):
            self._banner()
            if self._resumed:
                await self._recap()
                await self._refresh_session_tokens()
            try:
                while True:
                    if self._interactions:
                        try:
                            await self._answer_next()
                        except EOFError:
                            break
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
                            self.renderer.echo(text, PROMPT, CONTINUATION)
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
                escape(f"[{view.request_id[:8]}] is still running on the host.")
            )
        tasks = [self._foreground_task, *self._child_tasks, *self._side_tasks]
        tasks = [task for task in tasks if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.renderer.muted("Bye.")

    def run(self) -> None:
        """Run the REPL on a new event loop."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            pass


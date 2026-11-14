"""``mash run``: submit one request, print its answer, exit.

The answer goes to stdout as plain Markdown text and nothing else does, so the
command can be piped. Progress (one line per tool call) goes to stderr when
stderr is a terminal. ``--json`` prints every stream event as one JSON object
per line instead.

Exit status: 0 completed, 1 failed or cancelled, 2 the request asked the user
something (it is cancelled, since nobody is there to answer), 130 Ctrl-C.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
import time
from typing import Mapping, TextIO

from rich.console import Console

from .client import MashHostClient, MashHostClientError
from .render import OutputPrinter, RichRenderer, ToolLine, rich_theme
from .views import (
    COMPLETED,
    Children,
    Interaction,
    RequestView,
    Tool,
    answer_text,
    reduce,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NEEDS_INPUT = 2
EXIT_INTERRUPTED = 130


def read_message(argument: str | None, stdin: TextIO) -> str:
    """The message: the argument, or all of stdin when it is not a terminal."""
    if argument is not None and argument.strip():
        return argument.strip()
    if stdin.isatty():
        raise ValueError("give a message as an argument or on stdin")
    message = stdin.read().strip()
    if not message:
        raise ValueError("the message is empty")
    return message


def _submit(
    client: MashHostClient,
    *,
    agent_id: str,
    host_id: str | None,
    session_id: str,
    user_id: str,
    message: str,
) -> tuple[str, str]:
    """Submit to the host or the agent; returns (agent_id, request_id)."""
    if host_id:
        accepted = client.submit_host_request(
            host_id, message=message, session_id=session_id, user_id=user_id
        )
        return (
            str(accepted.get("agent_id") or agent_id),
            str(accepted.get("request_id") or ""),
        )
    request_id = client.submit_request(
        agent_id, message=message, session_id=session_id, user_id=user_id
    )
    return agent_id, request_id


async def _run(
    client: MashHostClient,
    *,
    agent_id: str,
    host_id: str | None,
    session_id: str,
    user_id: str,
    message: str,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
    theme: Mapping[str, str] | None,
    tool_lines: Mapping[str, ToolLine] | None,
) -> int:
    agent_id, request_id = await asyncio.to_thread(
        lambda: _submit(
            client,
            agent_id=agent_id,
            host_id=host_id,
            session_id=session_id,
            user_id=user_id,
            message=message,
        )
    )
    view = RequestView(
        agent_id=agent_id, request_id=request_id, started_at=time.monotonic(), brief=message
    )
    progress = None
    if not json_output and stderr.isatty():
        progress = OutputPrinter(
            RichRenderer(Console(file=stderr, theme=rich_theme(theme))), tool_lines=tool_lines
        )
    asked: Interaction | None = None
    try:
        async for event in client.request_events(agent_id, request_id):
            if json_output:
                stdout.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")
                stdout.flush()
            for output in reduce(view, event, time.monotonic()):
                if isinstance(output, Interaction):
                    asked = output
                elif progress is not None and isinstance(output, (Tool, Children)):
                    progress.print(output, time.monotonic())
            if asked is not None:
                break
    except asyncio.CancelledError:
        with contextlib.suppress(MashHostClientError):
            await asyncio.to_thread(client.cancel_request, agent_id, request_id)
        stderr.write("Interrupted. The request was cancelled.\n")
        return EXIT_INTERRUPTED

    if asked is not None:
        with contextlib.suppress(MashHostClientError):
            await asyncio.to_thread(client.cancel_request, agent_id, request_id)
        prompt = str(asked.payload.get("prompt") or "input required")
        stderr.write(
            f"The request asked for input and was cancelled: {prompt}\n"
            "Run it in `mash repl` to answer.\n"
        )
        return EXIT_NEEDS_INPUT
    if view.status != COMPLETED:
        stderr.write(f"{view.error or f'the request was {view.status}'}\n")
        return EXIT_FAILED
    if not json_output:
        stdout.write(answer_text(view).rstrip("\n") + "\n")
        stdout.flush()
    return EXIT_OK


def run_once(
    client: MashHostClient,
    *,
    agent_id: str,
    host_id: str | None,
    session_id: str,
    user_id: str,
    message: str,
    json_output: bool = False,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    theme: Mapping[str, str] | None = None,
    tool_lines: Mapping[str, ToolLine] | None = None,
) -> int:
    """Run one request to its end; returns the process exit status."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    async def main() -> int:
        task: asyncio.Task[int] = asyncio.ensure_future(
            _run(
                client,
                agent_id=agent_id,
                host_id=host_id,
                session_id=session_id,
                user_id=user_id,
                message=message,
                json_output=json_output,
                stdout=stdout,
                stderr=stderr,
                theme=theme,
                tool_lines=tool_lines,
            )
        )
        loop = asyncio.get_running_loop()
        handled = False
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            # Ctrl-C cancels the request before the command exits.
            loop.add_signal_handler(signal.SIGINT, task.cancel)
            handled = True
        try:
            return await task
        except asyncio.CancelledError:
            return EXIT_INTERRUPTED
        finally:
            if handled:
                loop.remove_signal_handler(signal.SIGINT)

    try:
        return asyncio.run(main())
    except MashHostClientError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_FAILED


__all__ = ["run_once", "read_message", "EXIT_OK", "EXIT_FAILED", "EXIT_NEEDS_INPUT", "EXIT_INTERRUPTED"]

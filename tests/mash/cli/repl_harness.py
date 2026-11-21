"""A scripted host and a driver for REPL tests.

``FakeHost`` stands in for ``MashHostClient``: each request id has a script of
stream events, and ``Gate`` items in a script hold the stream until the test
opens them. ``ReplDriver`` runs a ``Repl`` on a pipe, types into it, and waits
for conditions.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from typing import Any

from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from mash.cli.client import MashHostClientError
from mash.cli.render import rich_theme
from mash.cli.session import Repl, Target

ENTER = "\r"
CTRL_C = "\x03"
CTRL_D = "\x04"
CTRL_O = "\x0f"
CTRL_Z = "\x1a"
UP = "\x1b[A"
DOWN = "\x1b[B"


class Gate:
    """A point in a stream script that waits until the test opens it."""

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None
        self._open = False

    def open(self) -> None:
        self._open = True
        if self._event is not None:
            self._event.set()

    async def wait(self) -> None:
        if self._open:
            return
        self._event = asyncio.Event()
        await self._event.wait()


def trace(event_type: str, payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "event": "agent.trace",
        "data": {"event_type": event_type, "payload": payload or {}, **extra},
    }


def usage(input_tokens: int = 0, output_tokens: int = 0, reasoning: int | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
    }
    if reasoning is not None:
        attributes["mash.usage.reasoning_tokens"] = reasoning
    return attributes


def chat_end(
    action: str = "response",
    *,
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    duration_ms: int = 100,
    **tokens: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_type": action,
        "assistant_text": text,
        "tool_calls": tool_calls or [],
        "duration_ms": duration_ms,
        "attributes": usage(**tokens),
    }
    if blocks is not None:
        payload["assistant_blocks"] = blocks
    return trace("chat.end", payload)


def delta(text: str) -> dict[str, Any]:
    return trace("chat.delta", {"text": text})


def tool_start(call_id: str, name: str, arguments: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return trace(
        "execute_tool.start",
        {
            "arguments": arguments or {},
            "attributes": {"gen_ai.tool.name": name, "gen_ai.tool.call.id": call_id},
            **extra,
        },
    )


def tool_end(
    call_id: str,
    name: str,
    *,
    content: str = "ok",
    is_error: bool = False,
    duration_ms: int = 50,
) -> dict[str, Any]:
    return trace(
        "execute_tool.end",
        {
            "status": "error" if is_error else "ok",
            "duration_ms": duration_ms,
            "result": {"content": content, "is_error": is_error, "metadata": {}},
            "attributes": {"gen_ai.tool.name": name, "gen_ai.tool.call.id": call_id},
        },
    )


def completed(text: str = "", **data: Any) -> dict[str, Any]:
    return {
        "event": "request.completed",
        "data": {"response": {"text": text}, "text": text, **data},
    }


def failed(error: str) -> dict[str, Any]:
    return {"event": "request.error", "data": {"error": error}}


def cancelled() -> dict[str, Any]:
    return {"event": "request.cancelled", "data": {}}


def interaction(interaction_id: str, kind: str, prompt: str, **extra: Any) -> dict[str, Any]:
    return {
        "event": "request.interaction.create",
        "data": {"interaction_id": interaction_id, "type": kind, "prompt": prompt, **extra},
    }


def ack(interaction_id: str, response: Any = None, **extra: Any) -> dict[str, Any]:
    return {
        "event": "request.interaction.ack",
        "data": {"interaction_id": interaction_id, "response": response, **extra},
    }


class FakeHost:
    """A stand-in for ``MashHostClient`` with scripted request streams."""

    def __init__(self) -> None:
        # request id -> the stream's events and gates, in order.
        self.scripts: dict[str, list[Any]] = {}
        # Request ids handed out by submit, in order; "r-<n>" when exhausted.
        self.request_ids: list[str] = []
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.answers: list[dict[str, Any]] = []
        self.stream_calls: list[tuple[str, str, int]] = []
        self.records: list[dict[str, Any]] = []
        self.missing: set[str] = set()
        self.session = {"total_tokens": 0}
        # What the built-in commands read.
        self.agents: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        # None: the deployment has no Cortex, or no browsers.
        self.cortex: dict[str, Any] | None = None
        self.browser_sites: list[dict[str, Any]] | None = None
        self.reruns: list[tuple[str, str, str]] = []
        self.summary = ""
        # The user's sessions, newest first, and each session's turns.
        self.sessions: list[dict[str, Any]] = []
        self.history: dict[str, list[dict[str, Any]]] = {}
        # workflow id -> its runs; run id -> the run stream's events and gates.
        self.workflow_runs: dict[str, list[dict[str, Any]]] = {}
        self.workflow_scripts: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._count = 0

    def script(self, request_id: str, *events: Any) -> None:
        self.scripts[request_id] = list(events)

    def _next_id(self) -> str:
        self._count += 1
        if self.request_ids:
            return self.request_ids.pop(0)
        return f"r-{self._count}"

    # --- submit -----------------------------------------------------------

    def submit_request(self, agent_id, *, message, session_id, user_id, **_):
        request_id = self._next_id()
        self.submitted.append(
            {"agent_id": agent_id, "message": message, "session_id": session_id,
             "user_id": user_id, "request_id": request_id}
        )
        return request_id

    def submit_host_request(self, host_id, *, message, session_id, user_id, **_):
        request_id = self._next_id()
        self.submitted.append(
            {"host_id": host_id, "message": message, "session_id": session_id,
             "user_id": user_id, "request_id": request_id}
        )
        return {"request_id": request_id, "agent_id": "companion"}

    # --- streams ----------------------------------------------------------

    async def request_events(self, agent_id, request_id, *, after=0):
        self.stream_calls.append((agent_id, request_id, after))
        if request_id not in self.scripts:
            raise MashHostClientError(f"no such request {request_id}", status_code=404)
        seq = 0
        for item in self.scripts[request_id]:
            if isinstance(item, Gate):
                await item.wait()
                continue
            if isinstance(item, Exception):
                raise item
            seq += 1
            if seq <= after:
                continue
            await asyncio.sleep(0)
            yield {**item, "seq": seq}

    def list_workflow_runs(self, workflow_id, *, limit=50):
        return list(self.workflow_runs.get(workflow_id, []))

    async def workflow_run_events(self, workflow_id, run_id):
        self.stream_calls.append((workflow_id, run_id, 0))
        for item in self.workflow_scripts.get(run_id, []):
            if isinstance(item, Gate):
                await item.wait()
                continue
            if isinstance(item, Exception):
                raise item
            await asyncio.sleep(0)
            yield item

    # --- request control ----------------------------------------------------

    def get_request_status(self, agent_id, request_id):
        if request_id in self.missing or request_id not in self.scripts:
            raise MashHostClientError("Request not found", status_code=404)
        return {"request_id": request_id, "status": "running"}

    def cancel_request(self, agent_id, request_id):
        self.cancelled.append((agent_id, request_id))
        return {"status": "cancelled"}

    def post_interaction(self, agent_id, request_id, *, interaction_id, response):
        self.answers.append(
            {"agent_id": agent_id, "request_id": request_id,
             "interaction_id": interaction_id, "response": response}
        )
        return {}

    def list_session_requests(self, agent_id, session_id, *, status=None, started_by=None):
        return [
            record
            for record in self.records
            if (status is None or record.get("status") == status)
            and (started_by is None or record.get("started_by") == started_by)
        ]

    # --- what the built-in commands read -------------------------------------

    def get_host(self, host_id):
        return {"host_id": host_id, "agents": list(self.agents), "workflows": []}

    def get_agent(self, agent_id):
        found = next((a for a in self.agents if a["agent_id"] == agent_id), None)
        return {"agent": found or {"agent_id": agent_id, "metadata": None}}

    def list_tools(self):
        return list(self.tools)

    def list_skills(self):
        return list(self.skills)

    def _cortex(self):
        if self.cortex is None:
            raise MashHostClientError("Not Found", status_code=404)
        return self.cortex

    def get_cortex_user(self, user_id):
        cortex = self._cortex()
        if not cortex["profile"] and not cortex["entries"]:
            raise MashHostClientError(
                "user not found", status_code=404, code="CORTEX_NOT_FOUND"
            )
        return {"user_id": user_id, "profile": cortex["profile"],
                "entry_count": len(cortex["entries"])}

    def list_cortex_entries(self, user_id, *, q=None, limit=50):
        return list(self._cortex()["entries"])

    def create_cortex_entry(self, user_id, *, topic, content):
        entry = {"entry_id": f"e{len(self._cortex()['entries']) + 1}-0000-aaaa",
                 "topic": topic, "content": content, "origin": "app"}
        self._cortex()["entries"].append(entry)
        return entry

    def delete_cortex_entry(self, user_id, entry_id):
        cortex = self._cortex()
        cortex["entries"] = [e for e in cortex["entries"] if e["entry_id"] != entry_id]

    def list_browser_sites(self, user_id):
        if self.browser_sites is None:
            raise MashHostClientError(
                "this host has no browsers", status_code=503, code="BROWSER_UNAVAILABLE"
            )
        return list(self.browser_sites)

    def clear_browser_site(self, user_id, domain):
        removed = [s for s in self.browser_sites or [] if s["domain"] == domain]
        self.browser_sites = [s for s in self.browser_sites or [] if s["domain"] != domain]
        return {"domain": domain, "cookies": sum(s["cookies"] for s in removed)}

    def rerun_request(self, agent_id, request_id):
        new_id = self._next_id()
        self.reruns.append((agent_id, request_id, new_id))
        return {"request_id": new_id, "agent_id": agent_id}

    def compact_session(self, agent_id, session_id):
        self.calls.append(("compact_session", (agent_id, session_id)))
        return {"summary_text": self.summary, "trace_id": "t-compact"}

    def list_sessions(self, agent_id, *, user_id=None, limit=None):
        self.calls.append(("list_sessions", (agent_id, user_id, limit)))
        return list(self.sessions)

    def get_history(self, agent_id, session_id, *, limit=None):
        turns = self.history.get(session_id, [])
        return turns[-limit:] if limit else list(turns)

    def get_session(self, agent_id, session_id):
        return {"agent_id": agent_id, "session_id": session_id, **self.session}

    def record_command_event(self, event):
        return None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Any other client call is recorded and answers with nothing.
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            return [] if name.startswith("list_") else {}

        return _call


class ReplDriver:
    """Runs a REPL on a pipe inside the current event loop."""

    def __init__(
        self,
        host: FakeHost,
        *,
        host_id: str | None = None,
        target: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.host = host
        self._pipe_context = create_pipe_input()
        self.pipe = self._pipe_context.__enter__()
        self.buffer = io.StringIO()
        # Tests never touch the machine's clipboard.
        self.clipboard = InMemoryClipboard()
        console = Console(
            file=self.buffer, width=100, force_terminal=False, color_system=None,
            theme=rich_theme(),
        )
        self.repl = Repl(
            host,  # type: ignore[arg-type]
            Target(
                **{
                    "api_base_url": "http://host",
                    "agent_id": "companion" if host_id else "primary",
                    "session_id": "s-1",
                    "user_id": "u-1",
                    "host_id": host_id,
                    **(target or {}),
                }
            ),
            console=console,
            history=InMemoryHistory(),
            clipboard=self.clipboard,
            input=self.pipe,
            output=DummyOutput(),
            **kwargs,
        )
        self.task: asyncio.Task[None] | None = None
        # Slash commands that have finished, so ``say`` can wait for one.
        self.commands_run = 0
        run_command = self.repl.run_command

        async def counted(line: str) -> None:
            try:
                await run_command(line)
            finally:
                self.commands_run += 1

        self.repl.run_command = counted  # type: ignore[method-assign]

    @property
    def output(self) -> str:
        return self.buffer.getvalue()

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self.repl.run_async())

    def type(self, keys: str) -> None:
        self.pipe.send_text(keys)

    async def until(self, condition: Callable[[], bool], *, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if self.task is not None and self.task.done():
                self.task.result()
                raise AssertionError(f"the REPL ended early\n{self.output}")
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out waiting\n{self.output}")
            await asyncio.sleep(0.01)

    async def idle(self) -> None:
        """Wait until every submitted request has ended and nothing is queued."""
        await asyncio.sleep(0.05)
        await self.until(self.repl.idle)

    async def say(self, message: str) -> None:
        """Type a message, send it, and wait until the REPL is idle again."""
        submitted, commands = len(self.host.submitted), self.commands_run
        self.type(message + ENTER)
        if message.startswith("/"):
            await self.until(lambda: self.commands_run > commands)
        else:
            await self.until(lambda: len(self.host.submitted) > submitted)
        await self.idle()

    async def command(self, line: str) -> None:
        """Run a slash command and wait for it, whatever else is running."""
        commands = self.commands_run
        self.type(line + ENTER)
        await self.until(lambda: self.commands_run > commands)

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.type(CTRL_D)
            await asyncio.wait_for(self.task, timeout=5)
        self._pipe_context.__exit__(None, None, None)


async def run_repl(script: Callable[[ReplDriver], Any], host: FakeHost, **kwargs: Any) -> ReplDriver:
    """Start a REPL, run ``script`` against it, and close it."""
    driver = ReplDriver(host, **kwargs)
    driver.start()
    try:
        await script(driver)
    finally:
        await driver.close()
    return driver

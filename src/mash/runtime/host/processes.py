"""Agent runtimes as subprocesses of the host.

``AgentProcessProvider.ensure(agent_id)`` spawns one child per agent::

    mash agent serve --host-app <app> --agent <id> --host 127.0.0.1 --port <free port>

waits for its ``/health``, and returns how the host reaches it. The child is
the app's own trusted code, so it inherits the host's environment, plus its
agent token and the host's API URL. It does not get ``MASH_HOST_SECRET`` or
``MASH_API_KEY``: those are the host's, and the agent's model can read any file
the process can, ``/proc/self/environ`` included. A supervisor restarts a child that exits,
with backoff from 1 to 30 seconds, on the same port. Children live and die
with the host: each one exits when its parent does, and the host stops them
all on shutdown.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Mapping, Protocol

import httpx

from ..agent_process import (
    MASH_AGENT_TOKEN_ENV,
    MASH_HOST_API_URL_ENV,
    MASH_PARENT_PID_ENV,
    MASH_BROWSER_ENABLED_ENV,
    MASH_SANDBOX_ENABLED_ENV,
)
from .tokens import MASH_HOST_SECRET_ENV, derive_agent_token

# The host secret derives every agent, sandbox and link token, and the API key
# admits any caller. A child authenticates with its own token and needs neither.
_HOST_ONLY_ENV = frozenset({MASH_HOST_SECRET_ENV, "MASH_API_KEY"})

_MAX_BACKOFF_SECONDS = 30.0
# A child that ran this long before exiting restarts after the shortest backoff.
_STABLE_SECONDS = 60.0

STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AgentEndpoint:
    """How the host reaches one agent's server."""

    agent_id: str
    base_url: str
    token: str


class AgentStartError(RuntimeError):
    """An agent's server did not become healthy."""


class AgentProvider(Protocol):
    async def ensure(self, agent_id: str) -> AgentEndpoint: ...

    async def stop(self, agent_id: str) -> None: ...

    def status(self, agent_id: str) -> str | None: ...

    async def close(self) -> None: ...


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _Child:
    agent_id: str
    port: int
    token: str
    status: str = STATUS_STARTING
    process: asyncio.subprocess.Process | None = None
    started_at: float = 0.0
    pump: asyncio.Task[None] | None = None
    supervisor: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def endpoint(self) -> AgentEndpoint:
        return AgentEndpoint(
            agent_id=self.agent_id, base_url=f"http://127.0.0.1:{self.port}", token=self.token
        )


class AgentProcessProvider:
    """Each agent's ``AgentServer`` in its own child process on a localhost port."""

    def __init__(
        self,
        *,
        host_app: str,
        host_secret: str,
        host_api_url: str,
        sandboxes: bool = False,
        browsers: bool = False,
        environ: Mapping[str, str] | None = None,
        health_timeout: float = 120.0,
        stop_timeout: float = 10.0,
    ) -> None:
        if not str(host_app or "").strip():
            raise ValueError("host_app is required to start agents")
        if not str(host_secret or "").strip():
            raise ValueError(f"{MASH_HOST_SECRET_ENV} is required to start agents")
        self._host_app = host_app
        self._host_secret = host_secret
        self._host_api_url = host_api_url
        self._sandboxes = sandboxes
        self._browsers = browsers
        self._environ = {
            key: value
            for key, value in (os.environ if environ is None else environ).items()
            if key not in _HOST_ONLY_ENV
        }
        self._health_timeout = float(health_timeout)
        self._stop_timeout = float(stop_timeout)
        self._children: dict[str, _Child] = {}

    def status(self, agent_id: str) -> str | None:
        child = self._children.get(agent_id)
        return child.status if child is not None else None

    async def ensure(self, agent_id: str) -> AgentEndpoint:
        child = self._children.get(agent_id)
        if child is None:
            child = _Child(
                agent_id=agent_id,
                port=_free_local_port(),
                token=derive_agent_token(self._host_secret, agent_id),
            )
            self._children[agent_id] = child
        async with child.lock:
            if child.supervisor is None:
                try:
                    await self._spawn(child)
                except BaseException:
                    self._children.pop(agent_id, None)
                    raise
                child.supervisor = asyncio.create_task(self._supervise(child))
        return child.endpoint

    async def stop(self, agent_id: str) -> None:
        child = self._children.pop(agent_id, None)
        if child is None:
            return
        async with child.lock:
            if child.supervisor is not None:
                child.supervisor.cancel()
                try:
                    await child.supervisor
                except asyncio.CancelledError:
                    pass
            await self._terminate(child)

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(agent_id) for agent_id in list(self._children)))

    def _argv(self, child: _Child) -> list[str]:
        return [
            sys.executable, "-m", "mash.cli.main", "agent", "serve",
            "--host-app", self._host_app,
            "--agent", child.agent_id,
            "--host", "127.0.0.1",
            "--port", str(child.port),
        ]

    def _env(self, child: _Child) -> dict[str, str]:
        return {
            **self._environ,
            "MASH_HOST_APP": self._host_app,
            MASH_AGENT_TOKEN_ENV: child.token,
            MASH_HOST_API_URL_ENV: self._host_api_url,
            MASH_SANDBOX_ENABLED_ENV: "1" if self._sandboxes else "0",
            MASH_BROWSER_ENABLED_ENV: "1" if self._browsers else "0",
            MASH_PARENT_PID_ENV: str(os.getpid()),
        }

    async def _spawn(self, child: _Child) -> None:
        child.status = STATUS_STARTING
        child.process = await asyncio.create_subprocess_exec(
            *self._argv(child),
            env=self._env(child),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Terminal signals reach the host only; the host stops its children.
            start_new_session=True,
        )
        child.started_at = time.monotonic()
        child.pump = asyncio.create_task(_forward_output(child.agent_id, child.process))
        try:
            await self._wait_healthy(child)
        except BaseException:
            await self._terminate(child)
            child.status = STATUS_UNAVAILABLE
            raise
        child.status = STATUS_RUNNING

    async def _wait_healthy(self, child: _Child) -> None:
        assert child.process is not None
        deadline = time.monotonic() + self._health_timeout
        last_error = "no response"
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                if child.process.returncode is not None:
                    raise AgentStartError(
                        f"agent '{child.agent_id}' exited with code "
                        f"{child.process.returncode} during startup"
                    )
                try:
                    response = await client.get(f"{child.endpoint.base_url}/health")
                    if response.status_code == 200:
                        return
                    last_error = f"status {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc) or exc.__class__.__name__
                if time.monotonic() >= deadline:
                    raise AgentStartError(
                        f"agent '{child.agent_id}' is not healthy at "
                        f"{child.endpoint.base_url}: {last_error}"
                    )
                await asyncio.sleep(0.25)

    async def _supervise(self, child: _Child) -> None:
        backoff = 1.0
        while True:
            assert child.process is not None
            returncode = await child.process.wait()
            child.status = STATUS_UNAVAILABLE
            if time.monotonic() - child.started_at >= _STABLE_SECONDS:
                backoff = 1.0
            _write_line(
                child.agent_id,
                f"exited with code {returncode}; restarting in {backoff:.0f}s",
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
            try:
                await self._spawn(child)
            except AgentStartError as exc:
                _write_line(child.agent_id, str(exc))

    async def _terminate(self, child: _Child) -> None:
        process = child.process
        if process is None or process.returncode is not None:
            await _finish(child)
            return
        try:
            process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), self._stop_timeout)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        await _finish(child)


async def _finish(child: _Child) -> None:
    if child.pump is not None:
        await child.pump
        child.pump = None


async def _forward_output(agent_id: str, process: asyncio.subprocess.Process) -> None:
    """Copy a child's output into the host's log, one prefixed line at a time."""
    assert process.stdout is not None
    async for line in process.stdout:
        _write_line(agent_id, line.decode("utf-8", errors="replace").rstrip("\n"))


def _write_line(agent_id: str, text: str) -> None:
    sys.stderr.write(f"[{agent_id}] {text}\n")
    sys.stderr.flush()


__all__ = [
    "AgentEndpoint",
    "AgentProcessProvider",
    "AgentProvider",
    "AgentStartError",
    "STATUS_RUNNING",
    "STATUS_STARTING",
    "STATUS_UNAVAILABLE",
]

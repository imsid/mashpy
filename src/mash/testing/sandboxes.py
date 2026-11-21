"""Sandboxes on localhost ports, for tests and Docker-free runs.

``LocalSandboxProvider`` runs each policy's ``SandboxDaemon`` in this process on
its own ``127.0.0.1`` port, with a temporary directory per policy standing in for
the workspace volume. Bash runs as the current user.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from pathlib import Path

import uvicorn

from mash.sandbox.daemon import SandboxDaemon
from mash.sandbox.provider import SandboxEndpoint, SandboxInfo, sandbox_key
from mash.sandbox.tokens import derive_sandbox_token

DEFAULT_LOCAL_SECRET = "local-sandboxes"


class _Server(uvicorn.Server):
    # Many servers share this process; none of them should take over signals.
    def capture_signals(self):  # type: ignore[override]
        return contextlib.nullcontext()


class LocalSandboxProvider:
    """Each policy's sandbox daemon on a localhost port in this process."""

    def __init__(self, *, host_secret: str = DEFAULT_LOCAL_SECRET) -> None:
        self._host_secret = host_secret
        self.root = Path(tempfile.mkdtemp(prefix="mash-sandboxes-"))
        self._running: dict[str, tuple[_Server, asyncio.Task[None], SandboxEndpoint]] = {}
        self.ensure_calls: list[str] = []
        self.stop_calls: list[str] = []

    def workspaces(self, policy_id: str) -> Path:
        """The directory holding the policy's agent folders."""
        return self.root / sandbox_key(policy_id)

    async def ensure(self, policy_id: str) -> SandboxEndpoint:
        self.ensure_calls.append(policy_id)
        running = self._running.get(policy_id)
        if running is not None:
            return running[2]
        token = derive_sandbox_token(self._host_secret, policy_id)
        daemon = SandboxDaemon(token=token, workspaces_root=str(self.workspaces(policy_id)))
        server = _Server(
            uvicorn.Config(daemon.app, host="127.0.0.1", port=0, log_level="warning", ws="none")
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()  # raises the startup error
                raise RuntimeError("local sandbox server exited during startup")
            await asyncio.sleep(0.01)
        port = int(server.servers[0].sockets[0].getsockname()[1])
        endpoint = SandboxEndpoint(
            policy_id=policy_id, base_url=f"http://127.0.0.1:{port}", token=token
        )
        self._running[policy_id] = (server, task, endpoint)
        return endpoint

    async def stop(self, policy_id: str) -> None:
        self.stop_calls.append(policy_id)
        running = self._running.pop(policy_id, None)
        if running is None:
            return
        server, task, _ = running
        server.should_exit = True
        await task

    async def list(self) -> list[SandboxInfo]:
        return [
            SandboxInfo(policy_id=policy_id, name=f"local-{sandbox_key(policy_id)}", running=True, status="running")
            for policy_id in self._running
        ]

    async def close(self) -> None:
        for policy_id in list(self._running):
            await self.stop(policy_id)
        shutil.rmtree(self.root, ignore_errors=True)


__all__ = ["LocalSandboxProvider"]

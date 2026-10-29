"""The host's view of its sandboxes: which run, when each was last used.

Every call into a sandbox goes through ``SandboxManager``: commands
(``exec``), file reads and writes, and HTTP to a port in the sandbox
(``proxy``). It is the one place that sees each use. It starts a user's
sandbox on first use, stops sandboxes left idle for ``idle_seconds``, and keeps
at most ``max_running`` running, stopping the least recently used idle one to
make room (``containers.ContainerManager``).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

import httpx

from .containers import ContainerManager, bearer
from .provider import SandboxEndpoint, SandboxProvider, SandboxUnavailableError

MASH_SANDBOX_IDLE_SECONDS_ENV = "MASH_SANDBOX_IDLE_SECONDS"
MASH_SANDBOX_MAX_RUNNING_ENV = "MASH_SANDBOX_MAX_RUNNING"
DEFAULT_IDLE_SECONDS = 900.0
DEFAULT_MAX_RUNNING = 20
# Room on top of the command's own timeout: a first exec starts the sandbox.
_EXEC_OVERHEAD_SECONDS = 150.0

# Room for moving a file of up to 100 MB in or out.
_FILE_TIMEOUT_SECONDS = 300.0
_PROXY_TIMEOUT_SECONDS = 90.0


class SandboxCapacityError(RuntimeError):
    """Every running sandbox is busy and the cap allows no more."""


class SandboxManager(ContainerManager):
    """Reaches users' sandboxes through a ``SandboxProvider``."""

    kind = "sandbox"
    kinds = "sandboxes"
    unavailable_error = SandboxUnavailableError
    capacity_error = SandboxCapacityError

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_running: int = DEFAULT_MAX_RUNNING,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            provider, idle_seconds=idle_seconds, max_running=max_running, clock=clock
        )

    async def exec(
        self,
        user_id: str,
        *,
        agent_id: str,
        command: str,
        timeout: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run ``command`` for ``agent_id`` in ``user_id``'s sandbox."""
        body: dict[str, Any] = {"agent_id": agent_id, "command": command, "timeout": timeout}
        if stdin is not None:
            body["stdin"] = stdin
        if env:
            body["env"] = dict(env)

        async def forward(endpoint: SandboxEndpoint) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=timeout + _EXEC_OVERHEAD_SECONDS) as client:
                response = await client.post(
                    f"{endpoint.base_url}/exec", json=body, headers=bearer(endpoint)
                )
            if response.status_code != 200:
                raise SandboxUnavailableError(
                    f"sandbox for '{endpoint.user_id}' returned {response.status_code}: "
                    f"{response.text}"
                )
            return response.json()

        return await self._use(user_id, forward)

    async def read_file(self, user_id: str, *, agent_id: str, path: str) -> httpx.Response:
        """``GET /files`` in the user's sandbox; the daemon's response as is."""

        async def forward(endpoint: SandboxEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_FILE_TIMEOUT_SECONDS) as client:
                return await client.get(
                    f"{endpoint.base_url}/files/{agent_id}/{path}", headers=bearer(endpoint)
                )

        return await self._use(user_id, forward)

    async def write_file(
        self, user_id: str, *, agent_id: str, path: str, data: bytes
    ) -> httpx.Response:
        """``PUT /files`` in the user's sandbox; the daemon's response as is."""

        async def forward(endpoint: SandboxEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_FILE_TIMEOUT_SECONDS) as client:
                return await client.put(
                    f"{endpoint.base_url}/files/{agent_id}/{path}",
                    content=data,
                    headers=bearer(endpoint),
                )

        return await self._use(user_id, forward)

    async def proxy(
        self,
        user_id: str,
        *,
        port: int,
        method: str,
        path: str,
        query: str = "",
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> httpx.Response:
        """HTTP to ``port`` in the user's sandbox, through the daemon's ``/ports``."""
        url = f"/ports/{port}/{path}" + (f"?{query}" if query else "")

        async def forward(endpoint: SandboxEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_SECONDS) as client:
                return await client.request(
                    method,
                    f"{endpoint.base_url}{url}",
                    content=body,
                    headers={**dict(headers or {}), **bearer(endpoint)},
                )

        return await self._use(user_id, forward)


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_RUNNING",
    "MASH_SANDBOX_IDLE_SECONDS_ENV",
    "MASH_SANDBOX_MAX_RUNNING_ENV",
    "SandboxCapacityError",
    "SandboxManager",
]

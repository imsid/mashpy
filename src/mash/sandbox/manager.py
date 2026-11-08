"""The host's view of its sandboxes: which run, when each was last used.

Every call into a sandbox goes through ``SandboxManager``: commands
(``exec``), file reads and writes, and HTTP to a port in the sandbox
(``proxy``). It is the one place that sees each use. It starts a user's
sandbox on first use, stops sandboxes left idle for ``idle_seconds``, and keeps
at most ``max_running`` running, stopping the least recently used idle one to
make room (``containers.ContainerManager``).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

import httpx

from .containers import ContainerManager, bearer, daemon_path
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
        path = daemon_path("exec")

        async def forward(endpoint: SandboxEndpoint) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=timeout + _EXEC_OVERHEAD_SECONDS) as client:
                response = await client.post(
                    f"{endpoint.base_url}{path}", json=body, headers=bearer(endpoint)
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
        url = daemon_path("files", agent_id, tail=path)

        async def forward(endpoint: SandboxEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_FILE_TIMEOUT_SECONDS) as client:
                return await client.get(f"{endpoint.base_url}{url}", headers=bearer(endpoint))

        return await self._use(user_id, forward)

    async def write_file(
        self, user_id: str, *, agent_id: str, path: str, data: bytes
    ) -> httpx.Response:
        """``PUT /files`` in the user's sandbox; the daemon's response as is."""
        url = daemon_path("files", agent_id, tail=path)

        async def forward(endpoint: SandboxEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_FILE_TIMEOUT_SECONDS) as client:
                return await client.put(
                    f"{endpoint.base_url}{url}",
                    content=data,
                    headers=bearer(endpoint),
                )

        return await self._use(user_id, forward)

    async def stream_file(self, user_id: str, *, agent_id: str, path: str) -> "Streamed":
        """``GET /files`` in the user's sandbox, its body left unread for streaming."""
        url = daemon_path("files", agent_id, tail=path)
        return await self._stream(user_id, "GET", url, timeout=_FILE_TIMEOUT_SECONDS)

    async def stream_proxy(
        self,
        user_id: str,
        *,
        port: int,
        method: str,
        path: str,
        query: str = "",
        headers: Mapping[str, str] | None = None,
        body: AsyncIterator[bytes] | None = None,
    ) -> "Streamed":
        """HTTP to ``port`` in the user's sandbox, through the daemon's ``/ports``.

        ``body`` streams up and the response streams back. Raises
        ``UnsafePathError`` for a ``path`` with ``.`` or ``..`` segments.
        """
        url = daemon_path("ports", str(port), tail=path, query=query)
        return await self._stream(
            user_id, method, url, headers=headers, body=body, timeout=_PROXY_TIMEOUT_SECONDS
        )

    async def _stream(
        self,
        user_id: str,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: Mapping[str, str] | None = None,
        body: AsyncIterator[bytes] | None = None,
    ) -> "Streamed":
        # The sandbox counts as in use until the stream is closed, so the
        # reaper cannot stop it mid-transfer.
        stack = contextlib.AsyncExitStack()
        try:
            endpoint = await stack.enter_async_context(self.hold(user_id))
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=timeout))

            async def send(endpoint: SandboxEndpoint) -> httpx.Response:
                return await client.send(
                    client.build_request(
                        method,
                        f"{endpoint.base_url}{url}",
                        content=body,
                        headers={**dict(headers or {}), **bearer(endpoint)},
                    ),
                    stream=True,
                )

            try:
                response = await send(endpoint)
            except httpx.ConnectError:
                # The container was stopped or replaced behind the host's
                # back; the call never reached it, so its body is unread.
                self._endpoints.pop(user_id, None)
                response = await send(await self._ensure(user_id))
            stack.push_async_callback(response.aclose)
        except httpx.HTTPError as exc:
            await stack.aclose()
            raise self.unavailable_error(f"{self.kind} for '{user_id}' failed: {exc}") from exc
        except BaseException:
            await stack.aclose()
            raise
        return Streamed(response, stack.aclose)


class Streamed:
    """A daemon response whose body is read as it arrives; ``close`` it after."""

    def __init__(self, response: httpx.Response, close: Callable[[], Awaitable[None]]) -> None:
        self.response = response
        self.close = close

    async def chunks(self) -> AsyncIterator[bytes]:
        """The decoded body; closes the response when done or abandoned."""
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            await self.close()


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_RUNNING",
    "MASH_SANDBOX_IDLE_SECONDS_ENV",
    "MASH_SANDBOX_MAX_RUNNING_ENV",
    "SandboxCapacityError",
    "SandboxManager",
    "Streamed",
]

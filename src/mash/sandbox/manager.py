"""The host's view of its sandboxes: which run, when each was last used.

Every call into a sandbox goes through ``SandboxManager``: commands
(``exec``), file reads and writes, and HTTP to a port in the sandbox
(``proxy``). It is the one place that sees each use. It starts a user's
sandbox on first use, stops sandboxes left idle for ``idle_seconds``, and keeps
at most ``max_running`` running, stopping the least recently used idle one to
make room.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Mapping, TypeVar

import httpx

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

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SandboxCapacityError(RuntimeError):
    """Every running sandbox is busy and the cap allows no more."""


class SandboxManager:
    """Reaches users' sandboxes through a ``SandboxProvider``."""

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_running: int = DEFAULT_MAX_RUNNING,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        if max_running < 1:
            raise ValueError("max_running must be at least 1")
        self.provider = provider
        self.idle_seconds = float(idle_seconds)
        self.max_running = int(max_running)
        self._clock = clock
        self._endpoints: dict[str, SandboxEndpoint] = {}
        # Users whose sandbox runs, or is being started, with their last use.
        self._last_used: dict[str, float] = {}
        self._in_flight: dict[str, int] = defaultdict(int)
        self._user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._capacity_lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Adopt the sandboxes already running and start the idle reaper.

        After a host restart every running sandbox counts as active as of now.
        """
        now = self._clock()
        for info in await self.provider.list():
            if info.running:
                self._last_used.setdefault(info.user_id, now)
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_forever())

    async def close(self) -> None:
        """Stop the reaper and close the provider; sandboxes keep running."""
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        await self.provider.close()

    def running(self) -> list[str]:
        return list(self._last_used)

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
                    f"{endpoint.base_url}/exec", json=body, headers=_auth(endpoint)
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
                    f"{endpoint.base_url}/files/{agent_id}/{path}", headers=_auth(endpoint)
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
                    headers=_auth(endpoint),
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
                    headers={**dict(headers or {}), **_auth(endpoint)},
                )

        return await self._use(user_id, forward)

    async def _use(
        self, user_id: str, forward: Callable[[SandboxEndpoint], Awaitable[T]]
    ) -> T:
        """Call ``forward`` with the user's running sandbox, counting it as in use."""
        self._in_flight[user_id] += 1
        try:
            try:
                return await forward(await self._ensure(user_id))
            except httpx.ConnectError:
                # The container was stopped or replaced behind the host's back;
                # the call never reached it.
                self._endpoints.pop(user_id, None)
                return await forward(await self._ensure(user_id))
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"sandbox for '{user_id}' failed: {exc}") from exc
        finally:
            self._in_flight[user_id] -= 1
            if user_id in self._endpoints:
                self._last_used[user_id] = self._clock()

    async def stop(self, user_id: str) -> None:
        async with self._user_locks[user_id]:
            await self._stop_locked(user_id)

    async def reap_idle(self) -> list[str]:
        """Stop every sandbox with nothing in flight and no use for ``idle_seconds``."""
        now = self._clock()
        stopped: list[str] = []
        for user_id, last in list(self._last_used.items()):
            if self._in_flight[user_id] == 0 and now - last >= self.idle_seconds:
                async with self._user_locks[user_id]:
                    # A call may have started while this waited for the lock.
                    if self._in_flight[user_id] == 0:
                        await self._stop_locked(user_id)
                        stopped.append(user_id)
        return stopped

    async def _ensure(self, user_id: str) -> SandboxEndpoint:
        async with self._user_locks[user_id]:
            endpoint = self._endpoints.get(user_id)
            if endpoint is not None:
                return endpoint
            await self._reserve(user_id)
            try:
                endpoint = await self.provider.ensure(user_id)
            except Exception as exc:
                self._last_used.pop(user_id, None)
                if isinstance(exc, SandboxUnavailableError):
                    raise
                raise SandboxUnavailableError(
                    f"sandbox for '{user_id}' did not start: {exc}"
                ) from exc
            self._endpoints[user_id] = endpoint
            return endpoint

    async def _reserve(self, user_id: str) -> None:
        """Count the user's sandbox as running, stopping another if the cap is hit."""
        async with self._capacity_lock:
            if user_id in self._last_used:
                self._last_used[user_id] = self._clock()
                return
            others = list(self._last_used)
            if len(others) < self.max_running:
                self._last_used[user_id] = self._clock()
                return
            idle = [user for user in others if self._in_flight[user] == 0]
            if not idle:
                raise SandboxCapacityError(
                    f"all {self.max_running} sandboxes are busy; try again shortly"
                )
            victim = min(idle, key=lambda user: self._last_used[user])
            logger.info("stopping sandbox for %s to make room", victim)
            await self._stop_locked(victim)
            self._last_used[user_id] = self._clock()

    async def _stop_locked(self, user_id: str) -> None:
        await self.provider.stop(user_id)
        self._endpoints.pop(user_id, None)
        self._last_used.pop(user_id, None)

    async def _reap_forever(self) -> None:
        interval = min(60.0, self.idle_seconds / 4)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_idle()
            except Exception:  # pragma: no cover - logged, retried next pass
                logger.exception("stopping idle sandboxes failed")


def _auth(endpoint: SandboxEndpoint) -> dict[str, str]:
    return {"Authorization": f"Bearer {endpoint.token}"}


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_RUNNING",
    "MASH_SANDBOX_IDLE_SECONDS_ENV",
    "MASH_SANDBOX_MAX_RUNNING_ENV",
    "SandboxCapacityError",
    "SandboxManager",
]

"""The lifecycle the host keeps for per-user containers: sandboxes and browsers.

``ContainerManager`` starts a user's container on first use, under a per-user
lock, stops containers left idle for ``idle_seconds``, and keeps at most
``max_running`` running, stopping the least recently used idle one to make
room. Every call into a container goes through ``_use``, which counts it as in
use for the call's length. ``SandboxManager`` and ``BrowserManager`` add the
calls their containers answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ContainerEndpoint(Protocol):
    user_id: str
    base_url: str
    token: str


class ContainerProvider(Protocol):
    async def ensure(self, user_id: str) -> Any: ...

    async def stop(self, user_id: str) -> None: ...

    async def list(self) -> list[Any]: ...

    async def close(self) -> None: ...


class ContainerManager:
    """Reaches users' containers of one kind through a provider."""

    #: Used in log lines and errors: "sandbox", "browser".
    kind = "container"
    kinds = "containers"
    #: Raised when a container cannot be started or reached.
    unavailable_error: type[Exception] = RuntimeError
    #: Raised when every running container is busy and the cap allows no more.
    capacity_error: type[Exception] = RuntimeError

    def __init__(
        self,
        provider: ContainerProvider,
        *,
        idle_seconds: float,
        max_running: int,
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
        self._endpoints: dict[str, Any] = {}
        # Users whose container runs, or is being started, with their last use.
        self._last_used: dict[str, float] = {}
        self._in_flight: dict[str, int] = defaultdict(int)
        self._user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._capacity_lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Adopt the containers already running and start the idle reaper.

        After a host restart every running container counts as active as of now.
        """
        now = self._clock()
        for info in await self.provider.list():
            if info.running:
                self._last_used.setdefault(info.user_id, now)
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_forever())

    async def close(self) -> None:
        """Stop the reaper and close the provider; containers keep running."""
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

    async def endpoint(self, user_id: str) -> Any:
        """The user's running container, started if needed."""
        return await self._use(user_id, _identity)

    async def _use(self, user_id: str, forward: Callable[[Any], Awaitable[T]]) -> T:
        """Call ``forward`` with the user's running container, counting it as in use."""
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
            raise self.unavailable_error(f"{self.kind} for '{user_id}' failed: {exc}") from exc
        finally:
            self._in_flight[user_id] -= 1
            if user_id in self._endpoints:
                self._last_used[user_id] = self._clock()

    @contextlib.asynccontextmanager
    async def hold(self, user_id: str) -> AsyncIterator[Any]:
        """The user's running container, counted as in use until the block ends."""
        self._in_flight[user_id] += 1
        try:
            yield await self._ensure(user_id)
        finally:
            self._in_flight[user_id] -= 1
            self.touch(user_id)

    def touch(self, user_id: str) -> None:
        """Count the user's container as used now, e.g. while a person views it."""
        if user_id in self._last_used:
            self._last_used[user_id] = self._clock()

    async def stop(self, user_id: str) -> None:
        async with self._user_locks[user_id]:
            await self._stop_locked(user_id)

    async def reap_idle(self) -> list[str]:
        """Stop every container with nothing in flight and no use for ``idle_seconds``."""
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

    async def _ensure(self, user_id: str) -> Any:
        async with self._user_locks[user_id]:
            endpoint = self._endpoints.get(user_id)
            if endpoint is not None:
                return endpoint
            await self._reserve(user_id)
            try:
                endpoint = await self.provider.ensure(user_id)
            except Exception as exc:
                self._last_used.pop(user_id, None)
                if isinstance(exc, self.unavailable_error):
                    raise
                raise self.unavailable_error(
                    f"{self.kind} for '{user_id}' did not start: {exc}"
                ) from exc
            self._endpoints[user_id] = endpoint
            return endpoint

    async def _reserve(self, user_id: str) -> None:
        """Count the user's container as running, stopping another if the cap is hit."""
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
                raise self.capacity_error(
                    f"all {self.max_running} {self.kinds} are busy; try again shortly"
                )
            victim = min(idle, key=lambda user: self._last_used[user])
            logger.info("stopping %s for %s to make room", self.kind, victim)
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
                logger.exception("stopping idle %s failed", self.kinds)


async def _identity(endpoint: Any) -> Any:
    return endpoint


def bearer(endpoint: ContainerEndpoint) -> dict[str, str]:
    return {"Authorization": f"Bearer {endpoint.token}"}


__all__ = ["ContainerManager", "ContainerProvider", "bearer"]

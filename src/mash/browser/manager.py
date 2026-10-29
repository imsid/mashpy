"""The host's view of its browsers, and the handoffs open on them.

Every call into a browser goes through ``BrowserManager``: tool calls, file
moves, the live view, site data, and workflow code's CDP connection. It shares
the sandboxes' lifecycle (``mash.sandbox.containers``): a user's browser starts
on first use and stops after ``idle_seconds`` without one.

A handoff gives the user a live view of their browser while a request waits
for them to sign in. ``start_handoff`` mints a view token for the request
(``tokens.py``) and remembers it; ``end_handoff`` forgets it, so the link stops
working as soon as the user is done, or when it expires.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import httpx

from mash.sandbox.containers import ContainerManager, bearer

from .provider import BrowserEndpoint, BrowserUnavailableError
from .tokens import make_view_token, read_view_token

MASH_BROWSER_IDLE_SECONDS_ENV = "MASH_BROWSER_IDLE_SECONDS"
MASH_BROWSER_MAX_RUNNING_ENV = "MASH_BROWSER_MAX_RUNNING"
MASH_BROWSER_HANDOFF_SECONDS_ENV = "MASH_BROWSER_HANDOFF_SECONDS"
DEFAULT_IDLE_SECONDS = 900.0
DEFAULT_MAX_RUNNING = 10
DEFAULT_HANDOFF_SECONDS = 900
# A tool call may wait on a page load and a download.
_CALL_TIMEOUT_SECONDS = 180.0
_FILE_TIMEOUT_SECONDS = 300.0


class BrowserCapacityError(RuntimeError):
    """Every running browser is busy and the cap allows no more."""


@dataclass(frozen=True)
class Handoff:
    user_id: str
    request_id: str
    token: str
    expires_at: int


class BrowserManager(ContainerManager):
    """Reaches users' browsers through a ``DockerBrowserProvider``."""

    kind = "browser"
    kinds = "browsers"
    unavailable_error = BrowserUnavailableError
    capacity_error = BrowserCapacityError

    def __init__(
        self,
        provider: Any,
        *,
        host_secret: str,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_running: int = DEFAULT_MAX_RUNNING,
        handoff_seconds: int = DEFAULT_HANDOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            provider, idle_seconds=idle_seconds, max_running=max_running, clock=clock
        )
        self._host_secret = host_secret
        self.handoff_seconds = int(handoff_seconds)
        self._wall_clock = wall_clock
        self._handoffs: dict[str, Handoff] = {}

    async def call(
        self,
        user_id: str,
        *,
        request_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        sites: Sequence[str],
    ) -> dict[str, Any]:
        """Run one relayed tool in the request's tab; the daemon's result."""

        async def forward(endpoint: BrowserEndpoint) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=_CALL_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{endpoint.base_url}/requests/{request_id}/call",
                    json={"tool": tool, "arguments": dict(arguments), "sites": list(sites)},
                    headers=bearer(endpoint),
                )
            return _json_or_raise(response, endpoint)

        return await self._use(user_id, forward)

    async def upload(
        self,
        user_id: str,
        *,
        request_id: str,
        name: str,
        data: bytes,
        target: str,
        element: str,
        sites: Sequence[str],
    ) -> dict[str, Any]:
        """Give ``data`` to the file chooser that clicking ``target`` opens."""

        async def forward(endpoint: BrowserEndpoint) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=_FILE_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{endpoint.base_url}/requests/{request_id}/upload",
                    params={
                        "name": name,
                        "target": target,
                        "element": element,
                        "sites": json.dumps(list(sites)),
                    },
                    content=data,
                    headers=bearer(endpoint),
                )
            return _json_or_raise(response, endpoint)

        return await self._use(user_id, forward)

    async def download(self, user_id: str, download_id: str) -> httpx.Response:
        """``GET /downloads/<id>``; the daemon's response as is."""

        async def forward(endpoint: BrowserEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=_FILE_TIMEOUT_SECONDS) as client:
                return await client.get(
                    f"{endpoint.base_url}/downloads/{download_id}", headers=bearer(endpoint)
                )

        return await self._use(user_id, forward)

    async def front(self, user_id: str, request_id: str) -> None:
        async def forward(endpoint: BrowserEndpoint) -> None:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    f"{endpoint.base_url}/requests/{request_id}/front", headers=bearer(endpoint)
                )

        await self._use(user_id, forward)

    async def sites(self, user_id: str) -> list[dict[str, Any]]:
        async def forward(endpoint: BrowserEndpoint) -> list[dict[str, Any]]:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{endpoint.base_url}/sites", headers=bearer(endpoint))
            return list(_json_or_raise(response, endpoint).get("sites") or [])

        return await self._use(user_id, forward)

    async def clear_site(self, user_id: str, domain: str) -> dict[str, Any]:
        async def forward(endpoint: BrowserEndpoint) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.delete(
                    f"{endpoint.base_url}/sites/{domain}", headers=bearer(endpoint)
                )
            return _json_or_raise(response, endpoint)

        return await self._use(user_id, forward)

    async def view(
        self, user_id: str, *, path: str, query: str = ""
    ) -> httpx.Response:
        """A file of the live view's client, from the daemon's ``/view/``."""
        url = f"/view/{path}" + (f"?{query}" if query else "")

        async def forward(endpoint: BrowserEndpoint) -> httpx.Response:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await client.get(f"{endpoint.base_url}{url}", headers=bearer(endpoint))

        return await self._use(user_id, forward)

    async def delete_profile(self, user_id: str) -> None:
        """Stop the user's browser and delete its volume."""
        async with self._user_locks[user_id]:
            await self._stop_locked(user_id)
            await self.provider.delete(user_id)

    def start_handoff(self, user_id: str, request_id: str) -> Handoff:
        expires_at = int(self._wall_clock()) + self.handoff_seconds
        handoff = Handoff(
            user_id=user_id,
            request_id=request_id,
            token=make_view_token(self._host_secret, user_id, request_id, expires_at),
            expires_at=expires_at,
        )
        self._handoffs[handoff.token] = handoff
        return handoff

    def end_handoff(self, request_id: str) -> None:
        for token, handoff in list(self._handoffs.items()):
            if handoff.request_id == request_id:
                del self._handoffs[token]

    def handoff_for(self, token: str) -> Handoff | None:
        """The live view a view token opens, or None.

        A request's token works while its handoff is open. A token for no
        request is an operator's (``mash browser view``) and works until it
        expires.
        """
        claims = read_view_token(self._host_secret, token, now=self._wall_clock())
        if claims is None:
            self._handoffs.pop(token, None)
            return None
        user_id, request_id, expires_at = claims
        handoff = self._handoffs.get(token)
        if handoff is None and request_id == "":
            handoff = Handoff(user_id=user_id, request_id="", token=token, expires_at=expires_at)
        if handoff is None:
            return None
        self.touch(handoff.user_id)
        return handoff


def _json_or_raise(response: httpx.Response, endpoint: BrowserEndpoint) -> dict[str, Any]:
    if response.status_code != 200:
        raise BrowserUnavailableError(
            f"browser for '{endpoint.user_id}' returned {response.status_code}: {response.text}"
        )
    return response.json()


__all__ = [
    "BrowserCapacityError",
    "BrowserManager",
    "DEFAULT_HANDOFF_SECONDS",
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_MAX_RUNNING",
    "Handoff",
    "MASH_BROWSER_HANDOFF_SECONDS_ENV",
    "MASH_BROWSER_IDLE_SECONDS_ENV",
    "MASH_BROWSER_MAX_RUNNING_ENV",
]

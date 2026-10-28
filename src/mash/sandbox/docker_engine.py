"""Minimal async client for the Docker Engine HTTP API.

Talks to the daemon over its Unix socket (``/var/run/docker.sock`` or the
``unix://`` path in ``DOCKER_HOST``) or a ``tcp://`` address, using httpx. Only
the calls sandboxes need are here.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
_PULL_TIMEOUT = 600.0


class DockerEngineError(RuntimeError):
    """A Docker Engine API call failed."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"docker engine error ({status_code}): {message}")
        self.status_code = status_code


def _default_socket() -> str:
    # Docker Desktop on macOS may expose only the per-user socket.
    for candidate in (DEFAULT_DOCKER_SOCKET, os.path.expanduser("~/.docker/run/docker.sock")):
        if os.path.exists(candidate):
            return candidate
    return DEFAULT_DOCKER_SOCKET


def _transport_for(docker_host: str | None) -> tuple[httpx.AsyncBaseTransport, str]:
    value = str(docker_host or os.environ.get("DOCKER_HOST") or "").strip()
    if not value or value.startswith("unix://"):
        socket_path = value[len("unix://") :] if value else _default_socket()
        return httpx.AsyncHTTPTransport(uds=socket_path), "http://docker"
    if value.startswith("tcp://"):
        return httpx.AsyncHTTPTransport(), f"http://{value[len('tcp://'):]}"
    raise ValueError(f"unsupported DOCKER_HOST '{value}'")


def _split_image(image: str) -> tuple[str, str | None]:
    """``repo[:tag]`` (tag after the last ``/``), with ``latest`` as the default tag.

    A ``repo@digest`` reference is pulled as is.
    """
    if "@" in image:
        return image, None
    name, _, tag = image.rpartition(":")
    if not name or "/" in tag:
        return image, "latest"
    return name, tag


class DockerEngine:
    """The Docker Engine API calls sandboxes use."""

    def __init__(
        self,
        *,
        docker_host: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if transport is None:
            transport, base_url = _transport_for(docker_host)
        else:
            base_url = "http://docker"
        self._client = httpx.AsyncClient(transport=transport, base_url=base_url, timeout=60.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise DockerEngineError(0, f"cannot reach the Docker daemon: {exc}") from exc
        if response.status_code >= 400 and response.status_code not in allow:
            try:
                message = str(response.json().get("message") or response.text)
            except ValueError:
                message = response.text
            raise DockerEngineError(response.status_code, message)
        return response

    async def ping(self) -> None:
        """Raise ``DockerEngineError`` unless the daemon answers."""
        await self._call("GET", "/_ping")

    async def inspect_container(self, name: str) -> dict[str, Any] | None:
        response = await self._call("GET", f"/containers/{name}/json", allow=(404,))
        return None if response.status_code == 404 else response.json()

    async def list_containers(self, *, labels: list[str]) -> list[dict[str, Any]]:
        """Containers, running or not, that carry every one of ``labels``."""
        response = await self._call(
            "GET",
            "/containers/json",
            params={"all": "1", "filters": json.dumps({"label": list(labels)})},
        )
        return list(response.json())

    async def create_container(self, name: str, config: dict[str, Any]) -> str:
        response = await self._call(
            "POST", "/containers/create", params={"name": name}, body=config
        )
        return str(response.json()["Id"])

    async def start_container(self, name: str) -> None:
        # 304: already running.
        await self._call("POST", f"/containers/{name}/start", allow=(304,))

    async def stop_container(self, name: str) -> None:
        # 304: already stopped; 404: gone.
        await self._call("POST", f"/containers/{name}/stop", allow=(304, 404))

    async def remove_container(self, name: str) -> None:
        await self._call(
            "DELETE", f"/containers/{name}", params={"force": "1"}, allow=(404,)
        )

    async def ensure_image(self, image: str) -> None:
        """Pull ``image`` unless the daemon already has it."""
        response = await self._call("GET", f"/images/{image}/json", allow=(404,))
        if response.status_code != 404:
            return
        repository, tag = _split_image(image)
        try:
            # The pull streams progress until it is done; errors arrive in the stream.
            async with self._client.stream(
                "POST",
                "/images/create",
                params={"fromImage": repository, **({"tag": tag} if tag else {})},
                timeout=_PULL_TIMEOUT,
            ) as stream:
                if stream.status_code >= 400:
                    await stream.aread()
                    raise DockerEngineError(stream.status_code, stream.text)
                async for line in stream.aiter_lines():
                    if '"error"' in line:
                        raise DockerEngineError(500, json.loads(line).get("error", line))
        except httpx.HTTPError as exc:
            raise DockerEngineError(0, f"cannot pull {image}: {exc}") from exc

    async def ensure_volume(self, name: str, *, labels: dict[str, str]) -> None:
        response = await self._call("GET", f"/volumes/{name}", allow=(404,))
        if response.status_code == 404:
            await self._call(
                "POST", "/volumes/create", body={"Name": name, "Labels": labels}
            )

    async def remove_volume(self, name: str) -> None:
        # 404: already gone.
        await self._call("DELETE", f"/volumes/{name}", allow=(404,))

    async def ensure_network(self, name: str, *, labels: dict[str, str]) -> None:
        response = await self._call("GET", f"/networks/{name}", allow=(404,))
        if response.status_code == 404:
            await self._call(
                "POST",
                "/networks/create",
                body={"Name": name, "Driver": "bridge", "Labels": labels},
            )

    async def connect_network(self, network: str, container: str) -> None:
        # 403: already connected.
        await self._call(
            "POST",
            f"/networks/{network}/connect",
            body={"Container": container},
            allow=(403,),
        )


__all__ = ["DEFAULT_DOCKER_SOCKET", "DockerEngine", "DockerEngineError"]

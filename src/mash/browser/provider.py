"""Browser providers: where each user's browser runs.

``DockerBrowserProvider`` runs each browser as a container,
``<ns>-browser-<key>``, with a named volume of the same name for
``/home/browser`` (the Chromium profile, downloads, uploads and Playwright
MCP's output), on its own network, ``<ns>-browsers``. Sandboxes are not on that
network and hold no browser token. Browsers run Mash's browser image,
``ghcr.io/imsid/mash-browser:<mashpy version>``, unless ``MASH_BROWSER_IMAGE``
names another; a container made from another image is replaced and the
profile carries over in the volume.

Networking follows the sandbox provider: a host in a container joins the
network and reaches daemons by name; a host on the Docker host publishes each
daemon's port on ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping

from mash.sandbox.docker_engine import DockerEngine
from mash.sandbox.provider import (
    NAMESPACE_LABEL,
    SandboxInfo,
    _free_local_port,
    resolve_namespace,
    running_in_container,
    sandbox_key,
    wait_healthy,
)

from .daemon import BROWSER_HOME, BROWSER_PORT, MASH_BROWSER_TOKEN_ENV
from .tokens import derive_browser_token

BROWSER_USER_LABEL = "mash.browser.user"
MASH_BROWSER_IMAGE_ENV = "MASH_BROWSER_IMAGE"
BROWSER_IMAGE_REPOSITORY = "ghcr.io/imsid/mash-browser"
# Chromium needs more shared memory than Docker's 64 MB default.
_SHM_BYTES = 1024 * 1024 * 1024


class BrowserUnavailableError(RuntimeError):
    """A user's browser could not be started or reached."""


def default_browser_image() -> str:
    """Mash's browser image for the installed mashpy version."""
    try:
        tag = version("mashpy")
    except PackageNotFoundError:
        tag = "latest"
    return f"{BROWSER_IMAGE_REPOSITORY}:{tag}"


def browser_name(user_id: str, namespace: str) -> str:
    """The user's browser container and volume name."""
    return f"{namespace}-browser-{sandbox_key(user_id)}"


def browser_network(namespace: str) -> str:
    return f"{namespace}-browsers"


def browser_labels(user_id: str, namespace: str) -> dict[str, str]:
    return {BROWSER_USER_LABEL: user_id, NAMESPACE_LABEL: namespace}


@dataclass(frozen=True)
class BrowserEndpoint:
    """How the host reaches one user's browser daemon."""

    user_id: str
    base_url: str
    token: str


class DockerBrowserProvider:
    """Browsers as Docker containers, one per user, with a volume each."""

    def __init__(
        self,
        *,
        host_secret: str,
        image: str | None = None,
        namespace: str | None = None,
        in_container: bool | None = None,
        environ: Mapping[str, str] | None = None,
        engine: DockerEngine | None = None,
        health_timeout: float = 180.0,
    ) -> None:
        if not str(host_secret or "").strip():
            raise ValueError("a host secret is required to start browsers")
        self._host_secret = host_secret
        env = dict(os.environ if environ is None else environ)
        self._image = image or env.get(MASH_BROWSER_IMAGE_ENV) or default_browser_image()
        self.namespace = resolve_namespace(namespace, env)
        self._network = browser_network(self.namespace)
        self._in_container = running_in_container() if in_container is None else in_container
        self._engine = engine or DockerEngine()
        self._health_timeout = float(health_timeout)
        self._prepared = False
        self._prepare_lock = asyncio.Lock()

    @property
    def image(self) -> str:
        return self._image

    async def close(self) -> None:
        await self._engine.close()

    async def prepare(self) -> None:
        """Pull the image when missing and create the browser network, once."""
        async with self._prepare_lock:
            if self._prepared:
                return
            await self._engine.ensure_image(self._image)
            await self._engine.ensure_network(
                self._network, labels={NAMESPACE_LABEL: self.namespace}
            )
            if self._in_container:
                own = await self._engine.inspect_container(socket.gethostname())
                if own is None:
                    raise RuntimeError("cannot inspect the host's own container")
                await self._engine.connect_network(self._network, str(own["Id"]))
            self._prepared = True

    async def ensure(self, user_id: str) -> BrowserEndpoint:
        await self.prepare()
        name = browser_name(user_id, self.namespace)
        token = derive_browser_token(self._host_secret, user_id)
        await self._engine.ensure_volume(name, labels=browser_labels(user_id, self.namespace))
        container = await self._engine.inspect_container(name)
        if container is not None and (container.get("Config") or {}).get("Image") != self._image:
            await self._engine.remove_container(name)
            container = None
        if container is None:
            await self._engine.create_container(name, self._container_config(user_id, token))
            await self._engine.start_container(name)
            container = await self._engine.inspect_container(name)
        elif not container["State"].get("Running"):
            await self._engine.start_container(name)
            container = await self._engine.inspect_container(name)
        endpoint = BrowserEndpoint(
            user_id=user_id, base_url=self._base_url(name, container), token=token
        )
        try:
            await wait_healthy(
                endpoint.base_url, timeout=self._health_timeout, what=f"browser {name}"
            )
        except Exception as exc:
            raise BrowserUnavailableError(str(exc)) from exc
        return endpoint

    async def stop(self, user_id: str) -> None:
        await self._engine.stop_container(browser_name(user_id, self.namespace))

    async def delete(self, user_id: str) -> None:
        """Remove the user's browser and its volume: the profile is gone."""
        name = browser_name(user_id, self.namespace)
        await self._engine.remove_container(name)
        await self._engine.remove_volume(name)

    async def list(self) -> list[SandboxInfo]:
        containers = await self._engine.list_containers(
            labels=[BROWSER_USER_LABEL, f"{NAMESPACE_LABEL}={self.namespace}"]
        )
        return [
            SandboxInfo(
                user_id=str(summary["Labels"][BROWSER_USER_LABEL]),
                name=str((summary.get("Names") or ["/"])[0]).lstrip("/"),
                running=summary.get("State") == "running",
                status=str(summary.get("Status") or summary.get("State") or ""),
            )
            for summary in containers
        ]

    def _container_config(self, user_id: str, token: str) -> dict:
        host_config: dict = {
            "RestartPolicy": {"Name": "unless-stopped"},
            "ShmSize": _SHM_BYTES,
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": browser_name(user_id, self.namespace),
                    "Target": BROWSER_HOME,
                }
            ],
        }
        if not self._in_container:
            host_config["PortBindings"] = {
                f"{BROWSER_PORT}/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(_free_local_port())}
                ]
            }
        return {
            "Image": self._image,
            "User": "browser",
            "Cmd": ["mash", "browser", "serve", "--host", "0.0.0.0", "--port", str(BROWSER_PORT)],
            "Env": [f"{MASH_BROWSER_TOKEN_ENV}={token}"],
            "Labels": browser_labels(user_id, self.namespace),
            "ExposedPorts": {f"{BROWSER_PORT}/tcp": {}},
            "HostConfig": host_config,
            "NetworkingConfig": {"EndpointsConfig": {self._network: {}}},
        }

    def _base_url(self, name: str, container: dict | None) -> str:
        if self._in_container:
            return f"http://{name}:{BROWSER_PORT}"
        bindings = (container or {}).get("HostConfig", {}).get("PortBindings") or {}
        [binding] = bindings[f"{BROWSER_PORT}/tcp"]
        return f"http://127.0.0.1:{binding['HostPort']}"


__all__ = [
    "BROWSER_IMAGE_REPOSITORY",
    "BROWSER_USER_LABEL",
    "BrowserEndpoint",
    "BrowserUnavailableError",
    "DockerBrowserProvider",
    "MASH_BROWSER_IMAGE_ENV",
    "browser_labels",
    "browser_name",
    "browser_network",
    "default_browser_image",
]

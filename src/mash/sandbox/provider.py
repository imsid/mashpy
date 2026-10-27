"""Sandbox providers: where each user's sandbox runs.

``SandboxProvider.ensure(user_id)`` makes sure the user's sandbox exists and is
running and returns how the host reaches its daemon. ``DockerSandboxProvider``
runs each sandbox as a container with a named volume for the user's
workspaces, on its own network. Sandboxes run Mash's sandbox image,
``ghcr.io/imsid/mash-sandbox:<mashpy version>``, unless ``MASH_SANDBOX_IMAGE``
names another. A container made from another image, such as after a mashpy
upgrade, is replaced; the user's files stay in the volume.

Two networking modes, picked by whether the host itself runs in a container:

- In a container: the host joins the sandbox network and reaches each daemon
  by container name (``http://<ns>-sandbox-<key>:9000``).
- On the Docker host (local development): each daemon's port is published on
  ``127.0.0.1`` at a port fixed when the container is created.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping, Protocol, Sequence

import httpx

from .daemon import MASH_SANDBOX_TOKEN_ENV, SANDBOX_PORT, WORKSPACES_ROOT
from .docker_engine import DockerEngine
from .tokens import derive_sandbox_token

USER_LABEL = "mash.sandbox.user"
NAMESPACE_LABEL = "mash.namespace"
DEFAULT_NAMESPACE = "mash"
MASH_SANDBOX_IMAGE_ENV = "MASH_SANDBOX_IMAGE"
MASH_SANDBOX_MOUNTS_ENV = "MASH_SANDBOX_MOUNTS"
MASH_SANDBOX_NAMESPACE_ENV = "MASH_SANDBOX_NAMESPACE"
SANDBOX_IMAGE_REPOSITORY = "ghcr.io/imsid/mash-sandbox"
_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def default_sandbox_image() -> str:
    """Mash's sandbox image for the installed mashpy version."""
    try:
        tag = version("mashpy")
    except PackageNotFoundError:
        tag = "latest"
    return f"{SANDBOX_IMAGE_REPOSITORY}:{tag}"


@dataclass(frozen=True)
class SandboxEndpoint:
    """How the host reaches one user's sandbox daemon."""

    user_id: str
    base_url: str
    token: str


@dataclass(frozen=True)
class SandboxInfo:
    """One sandbox the provider knows, running or stopped."""

    user_id: str
    name: str
    running: bool
    status: str


class SandboxUnavailableError(RuntimeError):
    """A user's sandbox could not be started or reached."""


class SandboxProvider(Protocol):
    async def ensure(self, user_id: str) -> SandboxEndpoint: ...

    async def stop(self, user_id: str) -> None: ...

    async def list(self) -> list[SandboxInfo]: ...

    async def close(self) -> None: ...


def resolve_namespace(
    explicit_value: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """The deployment's sandbox namespace, ``mash`` unless one is set.

    Container, volume and network names are global on a Docker daemon, so two
    deployments on one daemon each set their own namespace.
    """
    env = os.environ if environ is None else environ
    value = str(explicit_value or env.get(MASH_SANDBOX_NAMESPACE_ENV) or "").strip()
    if not value:
        return DEFAULT_NAMESPACE
    if not _NAMESPACE.match(value):
        raise ValueError(
            f"sandbox namespace '{value}' must be lowercase letters, digits, '_', '.' "
            "or '-', starting with a letter or digit"
        )
    return value


def sandbox_key(user_id: str) -> str:
    """A Docker-safe name part for a user: Docker names allow no '@' or ':'."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def sandbox_name(user_id: str, namespace: str = DEFAULT_NAMESPACE) -> str:
    """The user's container and volume name."""
    return f"{namespace}-sandbox-{sandbox_key(user_id)}"


def network_name(namespace: str = DEFAULT_NAMESPACE) -> str:
    return f"{namespace}-sandboxes"


def sandbox_labels(user_id: str, namespace: str = DEFAULT_NAMESPACE) -> dict[str, str]:
    return {USER_LABEL: user_id, NAMESPACE_LABEL: namespace}


def parse_mounts(entries: Sequence[str]) -> list[dict]:
    """Docker bind mounts from ``source:target`` or ``source:target:ro`` entries.

    Sources are paths on the Docker host, since the daemon resolves them.
    """
    mounts: list[dict] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "ro"):
            raise ValueError(f"mount '{entry}' is not source:target or source:target:ro")
        source, target = parts[0], parts[1]
        if not source.startswith("/") or not target.startswith("/"):
            raise ValueError(f"mount '{entry}' needs absolute source and target paths")
        mounts.append(
            {"Type": "bind", "Source": source, "Target": target, "ReadOnly": len(parts) == 3}
        )
    return mounts


def running_in_container() -> bool:
    return os.path.exists("/.dockerenv")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_healthy(base_url: str, *, timeout: float, what: str) -> None:
    """Poll ``{base_url}/health`` until it answers 200."""
    deadline = time.monotonic() + timeout
    last_error = "no response"
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            try:
                response = await client.get(f"{base_url}/health")
                if response.status_code == 200:
                    return
                last_error = f"status {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc) or exc.__class__.__name__
            if time.monotonic() >= deadline:
                raise SandboxUnavailableError(f"{what} is not healthy at {base_url}: {last_error}")
            await asyncio.sleep(0.25)


class DockerSandboxProvider:
    """Sandboxes as Docker containers, one per user, with a volume each."""

    def __init__(
        self,
        *,
        host_secret: str,
        image: str | None = None,
        mounts: Sequence[str] | None = None,
        namespace: str | None = None,
        in_container: bool | None = None,
        environ: Mapping[str, str] | None = None,
        engine: DockerEngine | None = None,
        health_timeout: float = 120.0,
    ) -> None:
        if not str(host_secret or "").strip():
            raise ValueError("a host secret is required to start sandboxes")
        self._host_secret = host_secret
        env = dict(os.environ if environ is None else environ)
        self._image = image or env.get(MASH_SANDBOX_IMAGE_ENV) or default_sandbox_image()
        if mounts is None:
            mounts = env.get(MASH_SANDBOX_MOUNTS_ENV, "").split(",")
        self._mounts = parse_mounts(mounts)
        self.namespace = resolve_namespace(namespace, env)
        self._network = network_name(self.namespace)
        self._in_container = running_in_container() if in_container is None else in_container
        self._engine = engine or DockerEngine()
        self._health_timeout = float(health_timeout)
        self._prepared = False
        self._prepare_lock = asyncio.Lock()

    @property
    def engine(self) -> DockerEngine:
        return self._engine

    async def close(self) -> None:
        await self._engine.close()

    @property
    def image(self) -> str:
        return self._image

    async def prepare(self) -> None:
        """Pull the image when missing and create the sandbox network, once.

        A host in a container also joins the network, to reach the daemons.
        """
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

    async def ensure(self, user_id: str) -> SandboxEndpoint:
        await self.prepare()
        name = sandbox_name(user_id, self.namespace)
        token = derive_sandbox_token(self._host_secret, user_id)
        await self._engine.ensure_volume(name, labels=sandbox_labels(user_id, self.namespace))
        container = await self._engine.inspect_container(name)
        if container is not None and (container.get("Config") or {}).get("Image") != self._image:
            # Made from another image, such as an older mashpy's sandbox image, so
            # it would keep running that image's daemon. Replace it; the user's
            # files are in the volume and carry over.
            await self._engine.remove_container(name)
            container = None
        if container is None:
            await self._engine.create_container(name, self._container_config(user_id, token))
            await self._engine.start_container(name)
            container = await self._engine.inspect_container(name)
        elif not container["State"].get("Running"):
            await self._engine.start_container(name)
            container = await self._engine.inspect_container(name)
        endpoint = SandboxEndpoint(
            user_id=user_id, base_url=self._base_url(name, container), token=token
        )
        await wait_healthy(
            endpoint.base_url, timeout=self._health_timeout, what=f"sandbox {name}"
        )
        return endpoint

    async def stop(self, user_id: str) -> None:
        await self._engine.stop_container(sandbox_name(user_id, self.namespace))

    async def list(self) -> list[SandboxInfo]:
        containers = await self._engine.list_containers(
            labels=[USER_LABEL, f"{NAMESPACE_LABEL}={self.namespace}"]
        )
        return [
            SandboxInfo(
                user_id=str(summary["Labels"][USER_LABEL]),
                name=str((summary.get("Names") or ["/"])[0]).lstrip("/"),
                running=summary.get("State") == "running",
                status=str(summary.get("Status") or summary.get("State") or ""),
            )
            for summary in containers
        ]

    def _container_config(self, user_id: str, token: str) -> dict:
        host_config: dict = {
            # Restart a daemon that crashes; a stop by the reaper sticks.
            "RestartPolicy": {"Name": "unless-stopped"},
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": sandbox_name(user_id, self.namespace),
                    "Target": WORKSPACES_ROOT,
                },
                *self._mounts,
            ],
        }
        if not self._in_container:
            # Fixed at create time so the address survives restarts.
            host_config["PortBindings"] = {
                f"{SANDBOX_PORT}/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(_free_local_port())}
                ]
            }
        return {
            "Image": self._image,
            "User": "mash",
            "Cmd": ["mash", "sandbox", "serve", "--host", "0.0.0.0", "--port", str(SANDBOX_PORT)],
            # The sandbox's only variable: nothing from the host's environment.
            "Env": [f"{MASH_SANDBOX_TOKEN_ENV}={token}"],
            "Labels": sandbox_labels(user_id, self.namespace),
            "ExposedPorts": {f"{SANDBOX_PORT}/tcp": {}},
            "HostConfig": host_config,
            "NetworkingConfig": {"EndpointsConfig": {self._network: {}}},
        }

    def _base_url(self, name: str, container: dict | None) -> str:
        if self._in_container:
            return f"http://{name}:{SANDBOX_PORT}"
        bindings = (container or {}).get("HostConfig", {}).get("PortBindings") or {}
        [binding] = bindings[f"{SANDBOX_PORT}/tcp"]
        return f"http://127.0.0.1:{binding['HostPort']}"


__all__ = [
    "DEFAULT_NAMESPACE",
    "DockerSandboxProvider",
    "MASH_SANDBOX_IMAGE_ENV",
    "MASH_SANDBOX_MOUNTS_ENV",
    "MASH_SANDBOX_NAMESPACE_ENV",
    "NAMESPACE_LABEL",
    "SANDBOX_IMAGE_REPOSITORY",
    "SandboxEndpoint",
    "SandboxInfo",
    "SandboxProvider",
    "SandboxUnavailableError",
    "USER_LABEL",
    "default_sandbox_image",
    "network_name",
    "parse_mounts",
    "resolve_namespace",
    "running_in_container",
    "sandbox_key",
    "sandbox_labels",
    "sandbox_name",
    "wait_healthy",
]

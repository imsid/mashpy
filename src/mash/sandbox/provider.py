"""Sandbox providers: where each user's sandbox runs.

``SandboxProvider.ensure(user_id)`` makes sure the user's sandbox exists and is
running and returns how the host reaches its daemon. ``DockerSandboxProvider``
runs each sandbox as a container with a named volume for the user's
workspaces. Sandboxes run Mash's sandbox image,
``ghcr.io/imsid/mash-sandbox:<mashpy version>``, unless ``MASH_SANDBOX_IMAGE``
names another. A container made from another image, such as after a mashpy
upgrade, is replaced; the user's files stay in the volume.

Each user's sandbox has a network of its own, named like the container, so
code in one user's sandbox cannot reach another's. The network exists while
the sandbox runs: ``ensure`` creates it and attaches the container, and
``stop`` disconnects everything from it and removes it. Docker's own address
pools hold about 30 networks, so each network gets a /28 from
``MASH_SANDBOX_SUBNETS`` (default ``10.213.0.0/16``, 4096 networks).

Two networking modes, picked by whether the host itself runs in a container:

- In a container: the host joins each running user's network and reaches the
  daemon by container name (``http://<ns>-sandbox-<key>:9000``).
- On the Docker host (local development): each daemon's port is published on
  ``127.0.0.1`` at a port fixed when the container is created.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping, Protocol, Sequence

import httpx

from .daemon import MASH_SANDBOX_TOKEN_ENV, SANDBOX_PORT, WORKSPACES_ROOT
from .docker_engine import DockerEngine, DockerEngineError
from .tokens import derive_sandbox_token

logger = logging.getLogger(__name__)

USER_LABEL = "mash.sandbox.user"
NAMESPACE_LABEL = "mash.namespace"
DEFAULT_NAMESPACE = "mash"
MASH_SANDBOX_IMAGE_ENV = "MASH_SANDBOX_IMAGE"
MASH_SANDBOX_MOUNTS_ENV = "MASH_SANDBOX_MOUNTS"
MASH_SANDBOX_NAMESPACE_ENV = "MASH_SANDBOX_NAMESPACE"
MASH_SANDBOX_SUBNETS_ENV = "MASH_SANDBOX_SUBNETS"
DEFAULT_SANDBOX_SUBNETS = "10.213.0.0/16"
#: Each user's network: room for the sandbox, the host and the gateway.
NETWORK_PREFIX = 28
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


def resolve_subnets(
    explicit_value: str | None = None, environ: Mapping[str, str] | None = None
) -> ipaddress.IPv4Network:
    """The IPv4 range users' networks are carved from, in /28s."""
    env = os.environ if environ is None else environ
    value = str(
        explicit_value or env.get(MASH_SANDBOX_SUBNETS_ENV) or DEFAULT_SANDBOX_SUBNETS
    ).strip()
    try:
        subnets = ipaddress.ip_network(value)
    except ValueError as exc:
        raise ValueError(f"{MASH_SANDBOX_SUBNETS_ENV} '{value}' is not a network: {exc}") from exc
    if not isinstance(subnets, ipaddress.IPv4Network) or subnets.prefixlen > NETWORK_PREFIX:
        raise ValueError(
            f"{MASH_SANDBOX_SUBNETS_ENV} '{value}' must be an IPv4 range of /{NETWORK_PREFIX} or wider"
        )
    return subnets


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


def _attachment(container: dict, network: str, network_id: str) -> str:
    """How ``container`` stands on ``network``: attached, detached or stale.

    Stale: attached to an earlier network of that name, which Docker cannot
    start it on.
    """
    endpoint = ((container.get("NetworkSettings") or {}).get("Networks") or {}).get(network)
    if endpoint is None:
        return "detached"
    known = str(endpoint.get("NetworkID") or "")
    return "attached" if known in ("", network_id) else "stale"


async def ensure_user_network(
    engine: DockerEngine,
    user_id: str,
    namespace: str,
    subnets: ipaddress.IPv4Network,
) -> str:
    """Create the user's network unless it exists; return its id.

    The network gets the first /28 of ``subnets`` that overlaps no network on
    the daemon. One created by someone else between the listing and the
    create makes Docker refuse the subnet (403), and the next is tried.
    """
    name = sandbox_name(user_id, namespace)
    existing = await engine.inspect_network(name)
    if existing is not None:
        return str(existing["Id"])
    taken: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for network in await engine.list_networks():
        for config in (network.get("IPAM") or {}).get("Config") or []:
            if config.get("Subnet"):
                taken.append(ipaddress.ip_network(config["Subnet"], strict=False))
    for block in subnets.subnets(new_prefix=NETWORK_PREFIX):
        if any(block.version == other.version and block.overlaps(other) for other in taken):
            continue
        try:
            return await engine.create_network(
                name, labels=sandbox_labels(user_id, namespace), subnet=str(block)
            )
        except DockerEngineError as exc:
            if exc.status_code == 409:
                # Created by another caller just now.
                created = await engine.inspect_network(name)
                if created is not None:
                    return str(created["Id"])
            if exc.status_code == 403 and "overlaps" in str(exc):
                taken.append(block)
                continue
            raise
    raise SandboxUnavailableError(
        f"no free /{NETWORK_PREFIX} left in {subnets} for {name}; "
        f"widen {MASH_SANDBOX_SUBNETS_ENV}"
    )


async def remove_user_network(engine: DockerEngine, name: str) -> None:
    """Disconnect everything from the network ``name`` and remove it.

    Docker removes a network that only stopped containers name, and those
    containers then cannot start, so every container that names it is
    disconnected, running or not: the sandbox, and the host's container even
    while the host is down.
    """
    if await engine.inspect_network(name) is None:
        return
    for container in await engine.list_containers(network=name):
        await engine.disconnect_network(name, str(container["Id"]))
    await engine.remove_network(name)


async def stop_sandbox(engine: DockerEngine, user_id: str, *, namespace: str) -> None:
    """Stop the user's sandbox and remove its network; the volume is kept."""
    name = sandbox_name(user_id, namespace)
    await engine.stop_container(name)
    await remove_user_network(engine, name)


async def start_sandbox(
    engine: DockerEngine,
    user_id: str,
    *,
    namespace: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Start the user's existing sandbox on its network, as ``mash sandbox shell`` does.

    The host joins the network on its next call to the sandbox.
    """
    name = sandbox_name(user_id, namespace)
    container = await engine.inspect_container(name)
    if container is None:
        raise ValueError(f"no sandbox named {name}")
    network_id = await ensure_user_network(
        engine, user_id, namespace, resolve_subnets(environ=environ)
    )
    attachment = _attachment(container, name, network_id)
    if (container.get("HostConfig") or {}).get("NetworkMode") != name or attachment == "stale":
        raise ValueError(
            f"{name} was made for another network; the host replaces it on the user's next request"
        )
    if attachment == "detached":
        await engine.connect_network(name, name)
    await engine.start_container(name)


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
        subnets: str | None = None,
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
        self._subnets = resolve_subnets(subnets, env)
        self._in_container = running_in_container() if in_container is None else in_container
        self._engine = engine or DockerEngine()
        self._health_timeout = float(health_timeout)
        self._own_id: str | None = None
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
        """Pull the image when missing and clean up networks, once.

        Removes the networks of this namespace's sandboxes that are not
        running, which a host that died during a stop leaves behind.
        """
        async with self._prepare_lock:
            if self._prepared:
                return
            await self._engine.ensure_image(self._image)
            if self._in_container:
                own = await self._engine.inspect_container(socket.gethostname())
                if own is None:
                    raise RuntimeError("cannot inspect the host's own container")
                self._own_id = str(own["Id"])
            for network in await self._engine.list_networks(
                labels=[USER_LABEL, f"{NAMESPACE_LABEL}={self.namespace}"]
            ):
                name = str(network["Name"])
                container = await self._engine.inspect_container(name)
                if container is None or not container["State"].get("Running"):
                    logger.info("removing network %s of a stopped sandbox", name)
                    await remove_user_network(self._engine, name)
            self._prepared = True

    async def ensure(self, user_id: str) -> SandboxEndpoint:
        await self.prepare()
        name = sandbox_name(user_id, self.namespace)
        token = derive_sandbox_token(self._host_secret, user_id)
        await self._engine.ensure_volume(name, labels=sandbox_labels(user_id, self.namespace))
        network_id = await ensure_user_network(
            self._engine, user_id, self.namespace, self._subnets
        )
        container = await self._engine.inspect_container(name)
        if container is not None and self._must_replace(container, name, network_id):
            # Made from another image, such as an older mashpy's sandbox image, so
            # it would keep running that image's daemon; or made for another
            # network, such as the shared one sandboxes used before. Replace
            # it; the user's files are in the volume and carry over.
            await self._engine.remove_container(name)
            container = None
        if container is None:
            await self._engine.create_container(name, self._container_config(user_id, token))
        elif _attachment(container, name, network_id) == "detached":
            # Its network was removed by a stop, or it was started without one.
            await self._engine.connect_network(name, name)
        if container is None or not container["State"].get("Running"):
            await self._engine.start_container(name)
        if self._own_id is not None:
            await self._engine.connect_network(name, self._own_id)
        container = await self._engine.inspect_container(name)
        endpoint = SandboxEndpoint(
            user_id=user_id, base_url=self._base_url(name, container), token=token
        )
        await wait_healthy(
            endpoint.base_url, timeout=self._health_timeout, what=f"sandbox {name}"
        )
        return endpoint

    async def stop(self, user_id: str) -> None:
        await stop_sandbox(self._engine, user_id, namespace=self.namespace)

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

    def _must_replace(self, container: dict, name: str, network_id: str) -> bool:
        return (
            (container.get("Config") or {}).get("Image") != self._image
            or (container.get("HostConfig") or {}).get("NetworkMode") != name
            or _attachment(container, name, network_id) == "stale"
        )

    def _container_config(self, user_id: str, token: str) -> dict:
        name = sandbox_name(user_id, self.namespace)
        host_config: dict = {
            # Restart a daemon that crashes; a stop by the reaper sticks.
            "RestartPolicy": {"Name": "unless-stopped"},
            "NetworkMode": name,
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": name,
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
            "NetworkingConfig": {"EndpointsConfig": {name: {}}},
        }

    def _base_url(self, name: str, container: dict | None) -> str:
        if self._in_container:
            return f"http://{name}:{SANDBOX_PORT}"
        bindings = (container or {}).get("HostConfig", {}).get("PortBindings") or {}
        [binding] = bindings[f"{SANDBOX_PORT}/tcp"]
        return f"http://127.0.0.1:{binding['HostPort']}"


__all__ = [
    "DEFAULT_NAMESPACE",
    "DEFAULT_SANDBOX_SUBNETS",
    "DockerSandboxProvider",
    "MASH_SANDBOX_IMAGE_ENV",
    "MASH_SANDBOX_MOUNTS_ENV",
    "MASH_SANDBOX_NAMESPACE_ENV",
    "MASH_SANDBOX_SUBNETS_ENV",
    "NAMESPACE_LABEL",
    "SANDBOX_IMAGE_REPOSITORY",
    "SandboxEndpoint",
    "SandboxInfo",
    "SandboxProvider",
    "SandboxUnavailableError",
    "USER_LABEL",
    "default_sandbox_image",
    "ensure_user_network",
    "parse_mounts",
    "remove_user_network",
    "resolve_namespace",
    "resolve_subnets",
    "running_in_container",
    "sandbox_key",
    "sandbox_labels",
    "sandbox_name",
    "start_sandbox",
    "stop_sandbox",
    "wait_healthy",
]

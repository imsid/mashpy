"""Agent computers: one container per agent in the pool.

``ComputerProvider.ensure(agent_id)`` makes sure the agent's computer exists
and is running, and returns how the host reaches it. Docker is the one
provider. Containers outlive the host process and restart on crash through
Docker's restart policy, so the host runs no watch loop.

Two networking modes, picked by whether the host itself runs in a container:

- In a container: the host joins the private network and reaches each computer
  by container name (``http://mash-agent-<agent_id>:9000``); computers reach the
  host by its container name.
- On the Docker host (local development): each computer's port is published
  on ``127.0.0.1`` at a port fixed when the container is created, and
  computers reach the host at ``host.docker.internal``.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from typing import Mapping, Protocol

import httpx

from .docker_engine import DockerEngine
from .tokens import MASH_HOST_SECRET_ENV, derive_agent_token

AGENT_PORT = 9000
AGENT_LABEL = "mash.agent"
CONTAINER_PREFIX = "mash-agent-"
MEMORY_MOUNT = "/home/agent/memory"
WORKSPACE_MOUNT = "/home/agent/workspace"
MASH_COMPUTER_IMAGE_ENV = "MASH_COMPUTER_IMAGE"
MASH_COMPUTER_NETWORK_ENV = "MASH_COMPUTER_NETWORK"
MASH_COMPUTER_HOST_API_URL_ENV = "MASH_COMPUTER_HOST_API_URL"
DEFAULT_NETWORK = "mash"

# Process-specific variables of the host, and secrets a computer must not hold.
# Everything else in the host's environment passes through, because a spec may
# read any variable (provider keys, MCP header tokens, PARALLEL_API_KEY).
_ENV_EXCLUDED = frozenset(
    {
        "PATH",
        "HOME",
        "PWD",
        "OLDPWD",
        "SHELL",
        "SHLVL",
        "USER",
        "LOGNAME",
        "HOSTNAME",
        "TERM",
        "TMPDIR",
        "_",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "DOCKER_HOST",
        MASH_HOST_SECRET_ENV,
        "MASH_API_KEY",
        "MASH_AGENT_TOKEN",
        "MASH_HOST_API_URL",
    }
)


def container_name(agent_id: str) -> str:
    return f"{CONTAINER_PREFIX}{agent_id}"


def volume_names(agent_id: str) -> tuple[str, str]:
    return f"{agent_id}-memory", f"{agent_id}-workspace"


def computer_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The host environment a computer receives."""
    return {key: value for key, value in environ.items() if key not in _ENV_EXCLUDED}


@dataclass(frozen=True)
class ComputerEndpoint:
    """How the host reaches one agent's computer."""

    agent_id: str
    base_url: str
    token: str


class ComputerUnavailableError(RuntimeError):
    """An agent's computer did not become healthy."""


class ComputerProvider(Protocol):
    async def ensure(self, agent_id: str) -> ComputerEndpoint: ...

    async def close(self) -> None: ...


def _running_in_container() -> bool:
    return os.path.exists("/.dockerenv")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DockerComputerProvider:
    """Agent computers as Docker containers with named volumes."""

    def __init__(
        self,
        *,
        host_app: str,
        host_secret: str,
        host_api_port: int,
        image: str | None = None,
        network: str | None = None,
        host_api_url: str | None = None,
        in_container: bool | None = None,
        environ: Mapping[str, str] | None = None,
        engine: DockerEngine | None = None,
        health_timeout: float = 60.0,
    ) -> None:
        if not str(host_app or "").strip():
            raise ValueError("host_app is required to start agent computers")
        if not str(host_secret or "").strip():
            raise ValueError(f"{MASH_HOST_SECRET_ENV} is required to start agent computers")
        self._host_app = host_app
        self._host_secret = host_secret
        self._host_api_port = int(host_api_port)
        self._environ = dict(os.environ if environ is None else environ)
        self._image = image or self._environ.get(MASH_COMPUTER_IMAGE_ENV) or None
        self._network = (
            network or self._environ.get(MASH_COMPUTER_NETWORK_ENV) or DEFAULT_NETWORK
        )
        self._host_api_url = (
            host_api_url or self._environ.get(MASH_COMPUTER_HOST_API_URL_ENV) or None
        )
        self._in_container = _running_in_container() if in_container is None else in_container
        self._engine = engine or DockerEngine()
        self._health_timeout = float(health_timeout)
        self._prepared = False

    async def close(self) -> None:
        await self._engine.close()

    async def ensure(self, agent_id: str) -> ComputerEndpoint:
        await self._prepare()
        name = container_name(agent_id)
        token = derive_agent_token(self._host_secret, agent_id)
        memory_volume, workspace_volume = volume_names(agent_id)
        labels = {AGENT_LABEL: agent_id}
        await self._engine.ensure_volume(memory_volume, labels=labels)
        await self._engine.ensure_volume(workspace_volume, labels=labels)

        container = await self._engine.inspect_container(name)
        if container is None:
            await self._engine.create_container(name, self._container_config(agent_id, token))
            await self._engine.start_container(name)
            container = await self._engine.inspect_container(name)
        elif not container["State"].get("Running"):
            await self._engine.start_container(name)
            container = await self._engine.inspect_container(name)

        endpoint = ComputerEndpoint(
            agent_id=agent_id, base_url=self._base_url(name, container), token=token
        )
        await self._wait_healthy(endpoint)
        return endpoint

    async def _prepare(self) -> None:
        """Resolve the image and join the private network, once."""
        if self._prepared:
            return
        await self._engine.ensure_network(self._network, labels={"mash.network": "computers"})
        if self._in_container:
            own = await self._engine.inspect_container(socket.gethostname())
            if own is None:
                raise RuntimeError("cannot inspect the host's own container")
            if self._image is None:
                # The image id, not the tag, so computers run exactly this code.
                self._image = str(own["Image"])
            if self._host_api_url is None:
                own_name = str(own["Name"]).lstrip("/")
                self._host_api_url = f"http://{own_name}:{self._host_api_port}"
            await self._engine.connect_network(self._network, str(own["Id"]))
        else:
            if self._image is None:
                raise RuntimeError(
                    f"{MASH_COMPUTER_IMAGE_ENV} is required when the host runs outside "
                    "a container; the image must contain the host app"
                )
            if self._host_api_url is None:
                self._host_api_url = f"http://host.docker.internal:{self._host_api_port}"
        self._prepared = True

    def _container_config(self, agent_id: str, token: str) -> dict:
        memory_volume, workspace_volume = volume_names(agent_id)
        env = {
            **computer_env(self._environ),
            "MASH_HOST_APP": self._host_app,
            "MASH_AGENT_TOKEN": token,
            "MASH_HOST_API_URL": str(self._host_api_url),
        }
        host_config: dict = {
            "RestartPolicy": {"Name": "always"},
            "Mounts": [
                {"Type": "volume", "Source": memory_volume, "Target": MEMORY_MOUNT},
                {"Type": "volume", "Source": workspace_volume, "Target": WORKSPACE_MOUNT},
            ],
            "ExtraHosts": ["host.docker.internal:host-gateway"],
        }
        if not self._in_container:
            # Fixed at create time so the address survives container restarts.
            host_config["PortBindings"] = {
                f"{AGENT_PORT}/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(_free_local_port())}
                ]
            }
        return {
            "Image": self._image,
            "Cmd": [
                "mash", "agent", "serve",
                "--host-app", self._host_app,
                "--agent", agent_id,
                "--host", "0.0.0.0",
                "--port", str(AGENT_PORT),
            ],
            "Env": [f"{key}={value}" for key, value in sorted(env.items())],
            "Labels": {AGENT_LABEL: agent_id},
            "ExposedPorts": {f"{AGENT_PORT}/tcp": {}},
            "HostConfig": host_config,
            "NetworkingConfig": {"EndpointsConfig": {self._network: {}}},
        }

    def _base_url(self, name: str, container: dict | None) -> str:
        if self._in_container:
            return f"http://{name}:{AGENT_PORT}"
        bindings = (container or {}).get("HostConfig", {}).get("PortBindings") or {}
        [binding] = bindings[f"{AGENT_PORT}/tcp"]
        return f"http://127.0.0.1:{binding['HostPort']}"

    async def _wait_healthy(self, endpoint: ComputerEndpoint) -> None:
        deadline = time.monotonic() + self._health_timeout
        last_error = "no response"
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                try:
                    response = await client.get(f"{endpoint.base_url}/health")
                    if response.status_code == 200:
                        return
                    last_error = f"status {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc) or exc.__class__.__name__
                if time.monotonic() >= deadline:
                    raise ComputerUnavailableError(
                        f"computer for agent '{endpoint.agent_id}' is not healthy at "
                        f"{endpoint.base_url}: {last_error}"
                    )
                await asyncio.sleep(0.5)


async def replace_computer(engine: DockerEngine, agent_id: str) -> None:
    """Replace an agent's container with a fresh one of the same configuration.

    Volumes are kept, so memory and workspace survive; anything else on the
    container layer (apt installs, files outside the mounts) is gone.
    """
    name = container_name(agent_id)
    current = await engine.inspect_container(name)
    if current is None:
        raise ValueError(f"agent '{agent_id}' has no computer")
    config = dict(current["Config"])
    config["Image"] = current["Image"]
    config["HostConfig"] = current["HostConfig"]
    config["NetworkingConfig"] = {
        "EndpointsConfig": {
            network: {} for network in (current["NetworkSettings"].get("Networks") or {})
        }
    }
    await engine.remove_container(name)
    await engine.create_container(name, config)
    await engine.start_container(name)


__all__ = [
    "AGENT_LABEL",
    "AGENT_PORT",
    "ComputerEndpoint",
    "ComputerProvider",
    "ComputerUnavailableError",
    "DockerComputerProvider",
    "computer_env",
    "container_name",
    "replace_computer",
    "volume_names",
]

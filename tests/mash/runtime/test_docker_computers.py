"""Docker computer provider against a fake Docker Engine API.

The provider was also run against a real daemon (both networking modes, crash
restart, replacement); these tests pin the API calls and container config.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from mash.runtime.client import AgentClient, AgentUnavailableError
from mash.runtime.host.computers import (
    DockerComputerProvider,
    computer_env,
    replace_computer,
)
from mash.runtime.host.docker_engine import DockerEngine, DockerEngineError
from mash.runtime.host.tokens import derive_agent_token


class _FakeDaemon:
    """Just enough Docker Engine API state for the provider."""

    def __init__(self) -> None:
        self.volumes: set[str] = set()
        self.networks: dict[str, set[str]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def add_container(self, name: str, *, running: bool, config: dict | None = None) -> None:
        config = config or {"Image": "img", "HostConfig": {}, "Labels": {}}
        self.containers[name] = {
            "Id": f"id-{name}",
            "Name": f"/{name}",
            "Image": config["Image"],
            "Config": {k: v for k, v in config.items() if k not in ("HostConfig", "NetworkingConfig")},
            "HostConfig": config.get("HostConfig", {}),
            "State": {"Running": running},
            "NetworkSettings": {
                "Networks": {
                    net: {}
                    for net in (config.get("NetworkingConfig") or {}).get("EndpointsConfig", {})
                }
            },
            "_create": config,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else None
        parts = path.strip("/").split("/")
        if parts[0] == "volumes":
            if method == "GET":
                return httpx.Response(200 if parts[1] in self.volumes else 404, json={})
            self.volumes.add(body["Name"])
            return httpx.Response(201, json={})
        if parts[0] == "networks":
            if method == "GET":
                return httpx.Response(200 if parts[1] in self.networks else 404, json={})
            if parts[1] == "create":
                self.networks[body["Name"]] = set()
                return httpx.Response(201, json={})
            members = self.networks[parts[1]]
            if body["Container"] in members:
                return httpx.Response(403, json={"message": "already exists"})
            members.add(body["Container"])
            return httpx.Response(200, json={})
        if parts[:2] == ["containers", "create"]:
            name = request.url.params["name"]
            self.add_container(name, running=False, config=body)
            return httpx.Response(201, json={"Id": f"id-{name}"})
        if parts[0] == "containers":
            name = parts[1]
            container = self.containers.get(name)
            if method == "DELETE":
                self.containers.pop(name, None)
                return httpx.Response(204)
            if container is None:
                return httpx.Response(404, json={"message": "No such container"})
            if parts[2] == "json":
                return httpx.Response(200, json=container)
            if parts[2] == "start":
                if container["State"]["Running"]:
                    return httpx.Response(304)
                container["State"]["Running"] = True
                return httpx.Response(204)
        return httpx.Response(500, json={"message": f"unhandled {method} {path}"})


class DockerComputerProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.daemon = _FakeDaemon()
        self.engine = DockerEngine(transport=httpx.MockTransport(self.daemon.handler))
        healthy = patch(
            "mash.runtime.host.computers.DockerComputerProvider._wait_healthy",
            autospec=True,
        )
        self.wait_healthy = healthy.start()
        self.addCleanup(healthy.stop)

    def _provider(self, **overrides: Any) -> DockerComputerProvider:
        kwargs: dict[str, Any] = dict(
            host_app="app.spec:build_pool",
            host_secret="secret",
            host_api_port=8000,
            in_container=False,
            image="mash-app:dev",
            environ={
                "MASH_DATABASE_URL": "postgresql://db/mash",
                "ANTHROPIC_API_KEY": "sk-1",
                "MASH_HOST_SECRET": "secret",
                "MASH_API_KEY": "api",
                "PATH": "/opt/homebrew/bin",
                "HOME": "/Users/dev",
            },
            engine=self.engine,
        )
        kwargs.update(overrides)
        return DockerComputerProvider(**kwargs)

    async def test_creates_volume_network_and_container(self) -> None:
        endpoint = await self._provider().ensure("research")

        self.assertEqual(self.daemon.volumes, {"research-workspace"})
        self.assertIn("mash", self.daemon.networks)
        container = self.daemon.containers["mash-agent-research"]
        self.assertTrue(container["State"]["Running"])
        create = container["_create"]
        self.assertEqual(create["Image"], "mash-app:dev")
        self.assertEqual(
            create["Cmd"],
            ["mash", "agent", "serve", "--host-app", "app.spec:build_pool",
             "--agent", "research", "--host", "0.0.0.0", "--port", "9000"],
        )
        self.assertEqual(create["HostConfig"]["RestartPolicy"], {"Name": "always"})
        self.assertEqual(
            [(m["Source"], m["Target"]) for m in create["HostConfig"]["Mounts"]],
            [("research-workspace", "/home/agent/workspace")],
        )
        self.assertEqual(create["Labels"], {"mash.agent": "research"})
        self.assertEqual(endpoint.token, derive_agent_token("secret", "research"))
        self.wait_healthy.assert_called_once()

    async def test_container_env_passes_host_env_without_secrets(self) -> None:
        await self._provider().ensure("research")

        env = dict(
            item.split("=", 1)
            for item in self.daemon.containers["mash-agent-research"]["_create"]["Env"]
        )
        self.assertEqual(env["MASH_DATABASE_URL"], "postgresql://db/mash")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-1")
        self.assertEqual(env["MASH_AGENT_TOKEN"], derive_agent_token("secret", "research"))
        self.assertEqual(env["MASH_HOST_APP"], "app.spec:build_pool")
        self.assertEqual(env["MASH_HOST_API_URL"], "http://host.docker.internal:8000")
        for leaked in ("MASH_HOST_SECRET", "MASH_API_KEY", "PATH", "HOME"):
            self.assertNotIn(leaked, env)

    async def test_mounts_from_env_follow_the_workspace(self) -> None:
        environ = {
            "MASH_COMPUTER_MOUNTS": "/src/repo:/home/agent/workspace/repo:ro,/data:/data",
        }
        await self._provider(environ=environ).ensure("research")

        mounts = self.daemon.containers["mash-agent-research"]["_create"]["HostConfig"]["Mounts"]
        self.assertEqual(
            mounts[1:],
            [
                {"Type": "bind", "Source": "/src/repo", "Target": "/home/agent/workspace/repo", "ReadOnly": True},
                {"Type": "bind", "Source": "/data", "Target": "/data", "ReadOnly": False},
            ],
        )

    def test_rejects_malformed_mounts(self) -> None:
        for entry in ("/src", "src:/dst", "/src:dst", "/src:/dst:rw", "/a:/b:ro:x"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self._provider(mounts=[entry])

    async def test_dev_mode_publishes_a_fixed_local_port(self) -> None:
        endpoint = await self._provider().ensure("research")

        bindings = self.daemon.containers["mash-agent-research"]["_create"]["HostConfig"]["PortBindings"]
        [binding] = bindings["9000/tcp"]
        self.assertEqual(binding["HostIp"], "127.0.0.1")
        self.assertTrue(binding["HostPort"].isdigit())
        self.assertEqual(endpoint.base_url, f"http://127.0.0.1:{binding['HostPort']}")

    async def test_running_container_is_left_alone(self) -> None:
        provider = self._provider()
        await provider.ensure("research")
        self.daemon.calls.clear()

        await provider.ensure("research")

        self.assertNotIn(("POST", "/containers/create"), self.daemon.calls)
        self.assertNotIn(("POST", "/containers/mash-agent-research/start"), self.daemon.calls)

    async def test_stopped_container_is_started_not_recreated(self) -> None:
        provider = self._provider()
        await provider.ensure("research")
        self.daemon.containers["mash-agent-research"]["State"]["Running"] = False
        self.daemon.calls.clear()

        await provider.ensure("research")

        self.assertIn(("POST", "/containers/mash-agent-research/start"), self.daemon.calls)
        self.assertNotIn(("POST", "/containers/create"), self.daemon.calls)

    async def test_in_container_mode_uses_own_image_and_names(self) -> None:
        self.daemon.add_container("host-1", running=True, config={"Image": "sha256:abc", "HostConfig": {}})
        self.daemon.containers["host-1"]["Name"] = "/mash-host"
        with patch("mash.runtime.host.computers.socket.gethostname", return_value="host-1"):
            endpoint = await self._provider(in_container=True, image=None).ensure("research")

        create = self.daemon.containers["mash-agent-research"]["_create"]
        self.assertEqual(create["Image"], "sha256:abc")
        self.assertNotIn("PortBindings", create["HostConfig"])
        self.assertIn("MASH_HOST_API_URL=http://mash-host:8000", create["Env"])
        self.assertEqual(endpoint.base_url, "http://mash-agent-research:9000")
        self.assertIn("id-host-1", self.daemon.networks["mash"])

    async def test_dev_mode_requires_an_image(self) -> None:
        with self.assertRaises(RuntimeError):
            await self._provider(image=None).ensure("research")

    def test_requires_a_host_secret(self) -> None:
        with self.assertRaises(ValueError):
            self._provider(host_secret="")

    async def test_replace_recreates_with_the_same_config(self) -> None:
        await self._provider().ensure("research")
        before = self.daemon.containers["mash-agent-research"]

        await replace_computer(self.engine, "research")

        after = self.daemon.containers["mash-agent-research"]
        self.assertIsNot(after, before)
        self.assertTrue(after["State"]["Running"])
        self.assertEqual(after["_create"]["Cmd"], before["_create"]["Cmd"])
        self.assertEqual(after["_create"]["HostConfig"], before["HostConfig"])
        self.assertEqual(after["_create"]["NetworkingConfig"], {"EndpointsConfig": {"mash": {}}})
        self.assertEqual(self.daemon.volumes, {"research-workspace"})

    async def test_replace_unknown_agent_raises(self) -> None:
        with self.assertRaises(ValueError):
            await replace_computer(self.engine, "missing")

    async def test_engine_errors_carry_the_daemon_message(self) -> None:
        engine = DockerEngine(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(404, json={"message": "No such image: x"})
            )
        )
        with self.assertRaises(DockerEngineError) as ctx:
            await engine.create_container("c", {"Image": "x"})
        self.assertIn("No such image", str(ctx.exception))


class ComputerEnvTests(unittest.TestCase):
    def test_excludes_process_and_secret_variables(self) -> None:
        env = computer_env({"OPENAI_API_KEY": "k", "PATH": "/bin", "MASH_HOST_SECRET": "s"})

        self.assertEqual(env, {"OPENAI_API_KEY": "k"})


class AgentUnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_agent_raises_agent_unavailable(self) -> None:
        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        transport = httpx.MockTransport(_refuse)
        real_client = httpx.AsyncClient
        with patch(
            "mash.runtime.client.httpx.AsyncClient",
            lambda **kw: real_client(transport=transport, **kw),
        ):
            with self.assertRaises(AgentUnavailableError):
                await AgentClient("http://mash-agent-research:9000", "research").post_request(
                    "hi", session_id="s-1"
                )


if __name__ == "__main__":
    unittest.main()

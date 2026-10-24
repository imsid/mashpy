"""Docker sandbox provider against a fake Docker Engine API."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from mash.sandbox.docker_engine import DockerEngine
from mash.sandbox.provider import (
    DockerSandboxProvider,
    parse_mounts,
    resolve_namespace,
    sandbox_key,
    sandbox_name,
)
from mash.sandbox.tokens import derive_sandbox_token


def _has_label(labels: dict[str, str], wanted: str) -> bool:
    """Docker's label filter: ``key`` or ``key=value``."""
    key, sep, value = wanted.partition("=")
    return key in labels and (not sep or labels[key] == value)


class _FakeDaemon:
    """Just enough Docker Engine API state for the provider."""

    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}
        self.networks: dict[str, set[str]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.images: set[str] = {"mash-app:dev"}
        self.pulls: list[dict[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def add_container(self, name: str, *, running: bool, config: dict) -> None:
        self.containers[name] = {
            "Id": f"id-{name}",
            "Name": f"/{name}",
            "Image": config["Image"],
            "Labels": config.get("Labels", {}),
            "HostConfig": config.get("HostConfig", {}),
            "State": {"Running": running},
            "_create": config,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else None
        parts = path.strip("/").split("/")
        if parts[0] == "_ping":
            return httpx.Response(200, text="OK")
        if parts[0] == "images":
            if parts[1] == "create":
                params = dict(request.url.params)
                self.pulls.append(params)
                self.images.add(f"{params['fromImage']}:{params.get('tag', 'latest')}")
                return httpx.Response(200, text='{"status":"Pulling"}\n{"status":"Done"}\n')
            image = "/".join(parts[1:-1])
            return httpx.Response(200 if image in self.images else 404, json={})
        if parts[0] == "volumes":
            if method == "GET":
                return httpx.Response(200 if parts[1] in self.volumes else 404, json={})
            self.volumes[body["Name"]] = body["Labels"]
            return httpx.Response(201, json={})
        if parts[0] == "networks":
            if method == "GET":
                return httpx.Response(200 if parts[1] in self.networks else 404, json={})
            if parts[1] == "create":
                self.networks[body["Name"]] = set()
                return httpx.Response(201, json={})
            self.networks[parts[1]].add(body["Container"])
            return httpx.Response(200, json={})
        if parts == ["containers", "json"]:
            wanted = json.loads(request.url.params["filters"])["label"]
            summaries = []
            for name, container in self.containers.items():
                if all(_has_label(container["Labels"], label) for label in wanted):
                    state = "running" if container["State"]["Running"] else "exited"
                    summaries.append({"Names": [f"/{name}"], "Labels": container["Labels"], "State": state})
            return httpx.Response(200, json=summaries)
        if parts[:2] == ["containers", "create"]:
            name = request.url.params["name"]
            self.add_container(name, running=False, config=body)
            return httpx.Response(201, json={"Id": f"id-{name}"})
        if parts[0] == "containers":
            container = self.containers.get(parts[1])
            if container is None:
                return httpx.Response(404, json={"message": "No such container"})
            if parts[2] == "json":
                return httpx.Response(200, json=container)
            if parts[2] == "start":
                if container["State"]["Running"]:
                    return httpx.Response(304)
                container["State"]["Running"] = True
                return httpx.Response(204)
            if parts[2] == "stop":
                if not container["State"]["Running"]:
                    return httpx.Response(304)
                container["State"]["Running"] = False
                return httpx.Response(204)
        return httpx.Response(500, json={"message": f"unhandled {method} {path}"})


class DockerSandboxProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.daemon = _FakeDaemon()
        self.engine = DockerEngine(transport=httpx.MockTransport(self.daemon.handler))
        healthy = patch("mash.sandbox.provider.wait_healthy")
        self.wait_healthy = healthy.start()
        self.addCleanup(healthy.stop)

    def _provider(self, **overrides: Any) -> DockerSandboxProvider:
        kwargs: dict[str, Any] = dict(
            host_secret="secret",
            in_container=False,
            image="mash-app:dev",
            environ={"MASH_DATABASE_URL": "postgresql://db/mash", "ANTHROPIC_API_KEY": "sk-1"},
            engine=self.engine,
        )
        kwargs.update(overrides)
        return DockerSandboxProvider(**kwargs)

    async def test_creates_volume_network_and_container_for_the_user(self) -> None:
        endpoint = await self._provider().ensure("u@acme.io")

        name = f"mash-sandbox-{sandbox_key('u@acme.io')}"
        self.assertEqual(sandbox_name("u@acme.io"), name)
        self.assertEqual(
            self.daemon.volumes,
            {name: {"mash.sandbox.user": "u@acme.io", "mash.namespace": "mash"}},
        )
        self.assertIn("mash-sandboxes", self.daemon.networks)
        container = self.daemon.containers[name]
        self.assertTrue(container["State"]["Running"])
        create = container["_create"]
        self.assertEqual(create["Image"], "mash-app:dev")
        self.assertEqual(create["User"], "mash")
        self.assertEqual(
            create["Cmd"], ["mash", "sandbox", "serve", "--host", "0.0.0.0", "--port", "9000"]
        )
        self.assertEqual(
            create["HostConfig"]["Mounts"],
            [{"Type": "volume", "Source": name, "Target": "/home/agent/workspaces"}],
        )
        self.assertEqual(create["NetworkingConfig"], {"EndpointsConfig": {"mash-sandboxes": {}}})
        self.assertEqual(endpoint.token, derive_sandbox_token("secret", "u@acme.io"))
        [binding] = create["HostConfig"]["PortBindings"]["9000/tcp"]
        self.assertEqual(binding["HostIp"], "127.0.0.1")
        self.assertEqual(endpoint.base_url, f"http://127.0.0.1:{binding['HostPort']}")

    async def test_the_sandbox_gets_no_host_environment(self) -> None:
        await self._provider().ensure("u-1")

        [container] = self.daemon.containers.values()
        self.assertEqual(
            container["_create"]["Env"],
            [f"MASH_SANDBOX_TOKEN={derive_sandbox_token('secret', 'u-1')}"],
        )

    async def test_existing_container_is_started_not_recreated(self) -> None:
        provider = self._provider()
        await provider.ensure("u-1")
        await provider.stop("u-1")
        creates = [call for call in self.daemon.calls if call[1] == "/containers/create"]

        await provider.ensure("u-1")

        self.assertEqual(len(creates), 1)
        self.assertEqual(
            [call for call in self.daemon.calls if call[1] == "/containers/create"], creates
        )
        [container] = self.daemon.containers.values()
        self.assertTrue(container["State"]["Running"])

    async def test_list_reports_this_namespaces_sandboxes(self) -> None:
        provider = self._provider()
        await provider.ensure("u-1")
        await provider.ensure("u-2")
        await provider.stop("u-2")
        await self._provider(namespace="other").ensure("u-3")

        listed = {info.user_id: info.running for info in await provider.list()}

        self.assertEqual(listed, {"u-1": True, "u-2": False})

    async def test_in_a_container_it_joins_the_network_and_uses_names(self) -> None:
        self.daemon.add_container(
            "host-ctr", running=True, config={"Image": "sha256:abc", "Labels": {}}
        )
        with patch("mash.sandbox.provider.socket.gethostname", return_value="host-ctr"):
            provider = self._provider(in_container=True, namespace="acme")
            endpoint = await provider.ensure("u-1")

        name = f"acme-sandbox-{sandbox_key('u-1')}"
        self.assertEqual(endpoint.base_url, f"http://{name}:9000")
        self.assertEqual(self.daemon.networks["acme-sandboxes"], {"id-host-ctr"})
        create = self.daemon.containers[name]["_create"]
        self.assertEqual(create["Image"], "mash-app:dev")
        self.assertNotIn("PortBindings", create["HostConfig"])

    async def test_sandboxes_run_mashs_image_for_this_version_and_pull_it(self) -> None:
        with patch("mash.sandbox.provider.version", return_value="9.8.7"):
            provider = self._provider(image=None)
        self.assertEqual(provider.image, "ghcr.io/imsid/mash-sandbox:9.8.7")

        await provider.ensure("u-1")

        self.assertEqual(
            self.daemon.pulls, [{"fromImage": "ghcr.io/imsid/mash-sandbox", "tag": "9.8.7"}]
        )
        [container] = self.daemon.containers.values()
        self.assertEqual(container["_create"]["Image"], "ghcr.io/imsid/mash-sandbox:9.8.7")

    async def test_mash_sandbox_image_overrides_the_default(self) -> None:
        provider = self._provider(image=None, environ={"MASH_SANDBOX_IMAGE": "mash-app:dev"})
        await provider.ensure("u-1")

        self.assertEqual(self.daemon.pulls, [])
        [container] = self.daemon.containers.values()
        self.assertEqual(container["_create"]["Image"], "mash-app:dev")

    async def test_mounts_follow_the_workspace_volume(self) -> None:
        provider = self._provider(
            environ={"MASH_SANDBOX_MOUNTS": "/src/repo:/home/agent/mashpy:ro"}
        )
        await provider.ensure("u-1")

        [container] = self.daemon.containers.values()
        self.assertEqual(
            container["_create"]["HostConfig"]["Mounts"][1],
            {"Type": "bind", "Source": "/src/repo", "Target": "/home/agent/mashpy", "ReadOnly": True},
        )

    def test_namespace_and_mount_validation(self) -> None:
        self.assertEqual(resolve_namespace(environ={}), "mash")
        self.assertEqual(resolve_namespace(environ={"MASH_SANDBOX_NAMESPACE": "acme"}), "acme")
        with self.assertRaises(ValueError):
            resolve_namespace("Bad Name", environ={})
        with self.assertRaises(ValueError):
            parse_mounts(["relative:/target"])


if __name__ == "__main__":
    unittest.main()

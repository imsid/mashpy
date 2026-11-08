"""Docker sandbox provider against a fake Docker Engine API."""

from __future__ import annotations

import ipaddress
import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from mash.sandbox.docker_engine import DockerEngine
from mash.sandbox.provider import (
    DockerSandboxProvider,
    SandboxUnavailableError,
    parse_mounts,
    resolve_namespace,
    sandbox_key,
    sandbox_name,
    start_sandbox,
    stop_sandbox,
)
from mash.sandbox.tokens import derive_sandbox_token


def _has_label(labels: dict[str, str], wanted: str) -> bool:
    """Docker's label filter: ``key`` or ``key=value``."""
    key, sep, value = wanted.partition("=")
    return key in labels and (not sep or labels[key] == value)


class _FakeDaemon:
    """Just enough Docker Engine API state for the provider.

    Networks follow the rules the real daemon showed: a network with a running
    container attached cannot be removed (403); one with only stopped
    containers attached can, and those containers then fail to start, even
    once a network of the same name exists again, because they keep the old
    network's id. Subnets that overlap are refused (403).
    """

    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.images: set[str] = {"mash-app:dev"}
        self.pulls: list[dict[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        # Subnets taken by networks the daemon does not list, as when another
        # client creates one between Mash's listing and its create.
        self.hidden_subnets: list[str] = []
        self._ids = 0

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    def add_container(self, name: str, *, running: bool, config: dict) -> None:
        host_config = config.get("HostConfig", {})
        endpoints = (config.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}
        self.containers[name] = {
            "Id": f"id-{name}",
            "Name": f"/{name}",
            "Image": f"sha256:{config['Image']}",
            "Config": {"Image": config["Image"]},
            "Labels": config.get("Labels", {}),
            "HostConfig": host_config,
            "NetworkSettings": {
                "Networks": {
                    net: {"NetworkID": self.networks[net]["Id"] if net in self.networks else "gone"}
                    for net in endpoints
                }
            },
            "State": {"Running": running},
            "_create": config,
        }

    def add_network(self, name: str, *, subnet: str, labels: dict | None = None) -> None:
        self.networks[name] = {
            "Id": self._next_id("net"),
            "Name": name,
            "Labels": labels or {},
            "IPAM": {"Config": [{"Subnet": subnet}]},
        }

    def attached(self, network: str) -> set[str]:
        """Containers whose config names ``network``, running or not."""
        return {
            name
            for name, container in self.containers.items()
            if network in container["NetworkSettings"]["Networks"]
        }

    def _container(self, ref: str) -> dict[str, Any] | None:
        for name, container in self.containers.items():
            if ref in (name, container["Id"]):
                return container
        return None

    def _subnets(self) -> list[ipaddress.IPv4Network]:
        taken = [
            ipaddress.ip_network(config["Subnet"])
            for network in self.networks.values()
            for config in network["IPAM"]["Config"]
        ]
        return taken + [ipaddress.ip_network(s) for s in self.hidden_subnets]

    def _network_json(self, network: dict[str, Any]) -> dict[str, Any]:
        running = {
            container["Id"]: {"Name": name}
            for name, container in self.containers.items()
            if container["State"]["Running"]
            and container["NetworkSettings"]["Networks"].get(network["Name"], {}).get("NetworkID")
            == network["Id"]
        }
        return {**network, "Containers": running}

    def _networks(self, request: httpx.Request, method: str, parts: list[str], body: Any):
        if parts == ["networks"]:
            wanted = json.loads(request.url.params.get("filters") or "{}").get("label", [])
            return httpx.Response(
                200,
                json=[
                    self._network_json(network)
                    for network in self.networks.values()
                    if all(_has_label(network["Labels"], label) for label in wanted)
                ],
            )
        if parts[1] == "create":
            if body["Name"] in self.networks:
                return httpx.Response(409, json={"message": "network already exists"})
            configs = (body.get("IPAM") or {}).get("Config") or []
            if configs:
                subnet = ipaddress.ip_network(configs[0]["Subnet"])
            else:
                # Docker picks from its own pools.
                subnet = ipaddress.ip_network(f"172.{20 + len(self.networks)}.0.0/16")
            if any(subnet.overlaps(taken) for taken in self._subnets()):
                return httpx.Response(
                    403,
                    json={"message": "invalid pool request: Pool overlaps with other one on this address space"},
                )
            self.add_network(body["Name"], subnet=str(subnet), labels=body.get("Labels"))
            return httpx.Response(201, json={"Id": self.networks[body["Name"]]["Id"]})
        network = self.networks.get(parts[1])
        if network is None:
            return httpx.Response(404, json={"message": f"network {parts[1]} not found"})
        if method == "GET":
            return httpx.Response(200, json=self._network_json(network))
        if method == "DELETE":
            if self._network_json(network)["Containers"]:
                return httpx.Response(403, json={"message": "network has active endpoints"})
            del self.networks[parts[1]]
            return httpx.Response(204)
        container = self._container(body["Container"])
        assert container is not None, body
        networks = container["NetworkSettings"]["Networks"]
        if parts[2] == "connect":
            if network["Name"] in networks and container["State"]["Running"]:
                return httpx.Response(403, json={"message": "endpoint already exists"})
            networks[network["Name"]] = {"NetworkID": network["Id"]}
            return httpx.Response(200, json={})
        if parts[2] == "disconnect":
            if network["Name"] not in networks:
                return httpx.Response(
                    500, json={"message": "container is not connected to the network"}
                )
            del networks[network["Name"]]
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"message": f"unhandled {method} {parts}"})

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
            return self._networks(request, method, parts, body)
        if parts == ["containers", "json"]:
            filters = json.loads(request.url.params["filters"])
            wanted = filters.get("label", [])
            on_networks = filters.get("network", [])
            summaries = []
            for name, container in self.containers.items():
                networks = container["NetworkSettings"]["Networks"]
                if all(_has_label(container["Labels"], label) for label in wanted) and all(
                    net in networks for net in on_networks
                ):
                    state = "running" if container["State"]["Running"] else "exited"
                    summaries.append(
                        {
                            "Id": container["Id"],
                            "Names": [f"/{name}"],
                            "Labels": container["Labels"],
                            "State": state,
                        }
                    )
            return httpx.Response(200, json=summaries)
        if parts[:2] == ["containers", "create"]:
            name = request.url.params["name"]
            self.add_container(name, running=False, config=body)
            return httpx.Response(201, json={"Id": f"id-{name}"})
        if parts[0] == "containers":
            container = self._container(parts[1])
            if container is None:
                return httpx.Response(404, json={"message": "No such container"})
            if method == "DELETE":
                del self.containers[container["Name"].lstrip("/")]
                return httpx.Response(204)
            if parts[2] == "json":
                return httpx.Response(200, json=container)
            if parts[2] == "start":
                if container["State"]["Running"]:
                    return httpx.Response(304)
                for net, endpoint in container["NetworkSettings"]["Networks"].items():
                    current = self.networks.get(net)
                    if current is None or current["Id"] != endpoint["NetworkID"]:
                        return httpx.Response(
                            500,
                            json={"message": f"network {endpoint['NetworkID']} not found"},
                        )
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
        self.assertIn(name, self.daemon.networks)
        container = self.daemon.containers[name]
        self.assertTrue(container["State"]["Running"])
        create = container["_create"]
        self.assertEqual(create["Image"], "mash-app:dev")
        self.assertEqual(create["User"], "root")
        self.assertEqual(
            create["Cmd"], ["mash", "sandbox", "serve", "--host", "0.0.0.0", "--port", "9000"]
        )
        self.assertEqual(
            create["HostConfig"]["Mounts"],
            [{"Type": "volume", "Source": name, "Target": "/home/agent/workspaces"}],
        )
        self.assertEqual(create["NetworkingConfig"], {"EndpointsConfig": {name: {}}})
        self.assertEqual(create["HostConfig"]["NetworkMode"], name)
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

    async def test_a_container_made_from_another_image_is_replaced(self) -> None:
        await self._provider(image="mash-app:old").ensure("u-1")
        [name] = self.daemon.containers
        volumes = dict(self.daemon.volumes)

        await self._provider().ensure("u-1")

        self.assertIn(("DELETE", f"/containers/{name}"), self.daemon.calls)
        container = self.daemon.containers[name]
        self.assertEqual(container["Config"]["Image"], "mash-app:dev")
        self.assertTrue(container["State"]["Running"])
        self.assertEqual(self.daemon.volumes, volumes)

    async def test_list_reports_this_namespaces_sandboxes(self) -> None:
        provider = self._provider()
        await provider.ensure("u-1")
        await provider.ensure("u-2")
        await provider.stop("u-2")
        await self._provider(namespace="other").ensure("u-3")

        listed = {info.user_id: info.running for info in await provider.list()}

        self.assertEqual(listed, {"u-1": True, "u-2": False})

    def _in_container_provider(self, **overrides: Any) -> DockerSandboxProvider:
        self.daemon.add_container(
            "host-ctr", running=True, config={"Image": "sha256:abc", "Labels": {}}
        )
        gethostname = patch("mash.sandbox.provider.socket.gethostname", return_value="host-ctr")
        gethostname.start()
        self.addCleanup(gethostname.stop)
        return self._provider(in_container=True, **overrides)

    async def test_in_a_container_it_joins_the_users_network_and_uses_names(self) -> None:
        provider = self._in_container_provider(namespace="acme")
        endpoint = await provider.ensure("u-1")

        name = f"acme-sandbox-{sandbox_key('u-1')}"
        self.assertEqual(endpoint.base_url, f"http://{name}:9000")
        self.assertEqual(self.daemon.attached(name), {name, "host-ctr"})
        create = self.daemon.containers[name]["_create"]
        self.assertEqual(create["Image"], "mash-app:dev")
        self.assertNotIn("PortBindings", create["HostConfig"])

    # Per-user networks.

    async def test_each_user_gets_a_network_of_their_own(self) -> None:
        provider = self._provider()
        await provider.ensure("u-1")
        await provider.ensure("u-2")

        one, two = sandbox_name("u-1"), sandbox_name("u-2")
        self.assertEqual(self.daemon.attached(one), {one})
        self.assertEqual(self.daemon.attached(two), {two})
        self.assertEqual(
            self.daemon.networks[one]["Labels"],
            {"mash.sandbox.user": "u-1", "mash.namespace": "mash"},
        )
        subnets = [
            ipaddress.ip_network(self.daemon.networks[name]["IPAM"]["Config"][0]["Subnet"])
            for name in (one, two)
        ]
        pool = ipaddress.ip_network("10.213.0.0/16")
        for subnet in subnets:
            self.assertEqual(subnet.prefixlen, 28)
            self.assertTrue(subnet.subnet_of(pool))
        self.assertFalse(subnets[0].overlaps(subnets[1]))

    async def test_in_a_container_the_host_joins_every_users_network(self) -> None:
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        await provider.ensure("u-2")

        for user in ("u-1", "u-2"):
            name = sandbox_name(user)
            self.assertEqual(self.daemon.attached(name), {name, "host-ctr"})

    async def test_stop_removes_the_users_network(self) -> None:
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        await provider.ensure("u-2")

        await provider.stop("u-1")

        one, two = sandbox_name("u-1"), sandbox_name("u-2")
        self.assertNotIn(one, self.daemon.networks)
        self.assertEqual(self.daemon.containers[one]["NetworkSettings"]["Networks"], {})
        self.assertNotIn(one, self.daemon.containers["host-ctr"]["NetworkSettings"]["Networks"])
        self.assertEqual(self.daemon.attached(two), {two, "host-ctr"})

    async def test_stop_disconnects_a_stopped_host_container_too(self) -> None:
        # The host is down when `mash sandbox stop` runs. Left attached, its
        # container could not start once the network is gone.
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        self.daemon.containers["host-ctr"]["State"]["Running"] = False

        await stop_sandbox(self.engine, "u-1", namespace="mash")

        self.assertNotIn(sandbox_name("u-1"), self.daemon.networks)
        self.assertEqual(self.daemon.containers["host-ctr"]["NetworkSettings"]["Networks"], {})

    async def test_stop_is_idempotent(self) -> None:
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        await provider.stop("u-1")

        await provider.stop("u-1")
        await provider.stop("never-started")

        self.assertNotIn(sandbox_name("u-1"), self.daemon.networks)

    async def test_a_stopped_sandbox_starts_again_on_a_new_network(self) -> None:
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        await provider.stop("u-1")

        endpoint = await provider.ensure("u-1")

        name = sandbox_name("u-1")
        self.assertTrue(self.daemon.containers[name]["State"]["Running"])
        self.assertEqual(self.daemon.attached(name), {name, "host-ctr"})
        self.assertEqual(endpoint.base_url, f"http://{name}:9000")

    async def test_a_container_on_the_shared_network_is_replaced(self) -> None:
        name = sandbox_name("u-1")
        self.daemon.add_network("mash-sandboxes", subnet="172.30.0.0/16")
        self.daemon.add_container(
            name,
            running=True,
            config={
                "Image": "mash-app:dev",
                "Labels": {"mash.sandbox.user": "u-1", "mash.namespace": "mash"},
                "HostConfig": {"PortBindings": {"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}]}},
                "NetworkingConfig": {"EndpointsConfig": {"mash-sandboxes": {}}},
            },
        )

        await self._provider().ensure("u-1")

        self.assertIn(("DELETE", f"/containers/{name}"), self.daemon.calls)
        self.assertEqual(self.daemon.attached("mash-sandboxes"), set())
        self.assertEqual(self.daemon.attached(name), {name})

    async def test_a_running_sandbox_that_lost_its_network_is_attached_again(self) -> None:
        # E.g. started by hand after a stop removed its network.
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        await provider.stop("u-1")
        name = sandbox_name("u-1")
        self.daemon.containers[name]["State"]["Running"] = True

        await provider.ensure("u-1")

        self.assertEqual(self.daemon.attached(name), {name, "host-ctr"})

    async def test_prepare_removes_networks_left_by_stopped_sandboxes(self) -> None:
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        await provider.ensure("u-2")
        # The host died between stopping u-1's container and removing its network.
        self.daemon.containers[sandbox_name("u-1")]["State"]["Running"] = False
        self.daemon.add_network(
            sandbox_name("gone"),
            subnet="10.213.9.0/28",
            labels={"mash.sandbox.user": "gone", "mash.namespace": "mash"},
        )

        await self._provider(in_container=True).prepare()

        self.assertNotIn(sandbox_name("u-1"), self.daemon.networks)
        self.assertNotIn(sandbox_name("gone"), self.daemon.networks)
        self.assertIn(sandbox_name("u-2"), self.daemon.networks)

    async def test_subnets_in_use_are_skipped(self) -> None:
        self.daemon.add_network("someone-else", subnet="10.213.0.0/24")
        self.daemon.hidden_subnets.append("10.213.1.0/28")

        await self._provider().ensure("u-1")

        subnet = self.daemon.networks[sandbox_name("u-1")]["IPAM"]["Config"][0]["Subnet"]
        self.assertEqual(subnet, "10.213.1.16/28")

    async def test_mash_sandbox_subnets_sets_the_range(self) -> None:
        provider = self._provider(environ={"MASH_SANDBOX_SUBNETS": "10.99.0.0/27"})
        await provider.ensure("u-1")
        await provider.ensure("u-2")

        subnets = {
            self.daemon.networks[sandbox_name(user)]["IPAM"]["Config"][0]["Subnet"]
            for user in ("u-1", "u-2")
        }
        self.assertEqual(subnets, {"10.99.0.0/28", "10.99.0.16/28"})
        with self.assertRaises(SandboxUnavailableError):
            await provider.ensure("u-3")

    def test_subnet_range_validation(self) -> None:
        for value in ("not-a-net", "10.0.0.0/29", "fd00::/64"):
            with self.assertRaises(ValueError, msg=value):
                self._provider(environ={"MASH_SANDBOX_SUBNETS": value})

    async def test_the_cli_stop_and_start_follow_the_same_lifecycle(self) -> None:
        provider = self._in_container_provider()
        await provider.ensure("u-1")
        name = sandbox_name("u-1")

        await stop_sandbox(self.engine, "u-1", namespace="mash")
        self.assertFalse(self.daemon.containers[name]["State"]["Running"])
        self.assertNotIn(name, self.daemon.networks)

        await start_sandbox(self.engine, "u-1", namespace="mash", environ={})
        self.assertTrue(self.daemon.containers[name]["State"]["Running"])
        self.assertEqual(self.daemon.attached(name), {name})

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

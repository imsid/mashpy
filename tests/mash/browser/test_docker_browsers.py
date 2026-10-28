"""Docker browser provider against a fake Docker Engine API."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import httpx

from mash.browser.provider import DockerBrowserProvider, browser_name
from mash.browser.tokens import derive_browser_token
from mash.sandbox.docker_engine import DockerEngine
from mash.sandbox.provider import sandbox_key
from tests.mash.sandbox.test_docker_sandboxes import _FakeDaemon


class _FakeDaemonWithVolumeRemoval(_FakeDaemon):
    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.method == "DELETE" and parts[0] == "volumes":
            self.calls.append(("DELETE", request.url.path))
            self.volumes.pop(parts[1], None)
            return httpx.Response(204)
        return super().handler(request)


class DockerBrowserProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.daemon = _FakeDaemonWithVolumeRemoval()
        self.daemon.images.add("mash-browser:dev")
        self.engine = DockerEngine(transport=httpx.MockTransport(self.daemon.handler))
        healthy = patch("mash.browser.provider.wait_healthy")
        healthy.start()
        self.addCleanup(healthy.stop)

    def _provider(self, **overrides: Any) -> DockerBrowserProvider:
        kwargs: dict[str, Any] = dict(
            host_secret="secret",
            in_container=False,
            image="mash-browser:dev",
            environ={"MASH_HOST_SECRET": "secret", "ANTHROPIC_API_KEY": "sk-1"},
            engine=self.engine,
        )
        kwargs.update(overrides)
        return DockerBrowserProvider(**kwargs)

    async def test_the_users_browser_gets_its_own_volume_network_and_token(self) -> None:
        endpoint = await self._provider().ensure("u@acme.io")

        name = f"mash-browser-{sandbox_key('u@acme.io')}"
        self.assertEqual(browser_name("u@acme.io", "mash"), name)
        self.assertEqual(
            self.daemon.volumes, {name: {"mash.browser.user": "u@acme.io", "mash.namespace": "mash"}}
        )
        self.assertIn("mash-browsers", self.daemon.networks)
        self.assertNotIn("mash-sandboxes", self.daemon.networks)
        create = self.daemon.containers[name]["_create"]
        self.assertEqual(create["User"], "browser")
        self.assertEqual(
            create["Cmd"], ["mash", "browser", "serve", "--host", "0.0.0.0", "--port", "9000"]
        )
        self.assertEqual(
            create["HostConfig"]["Mounts"],
            [{"Type": "volume", "Source": name, "Target": "/home/browser"}],
        )
        self.assertGreaterEqual(create["HostConfig"]["ShmSize"], 512 * 1024 * 1024)
        self.assertEqual(create["NetworkingConfig"], {"EndpointsConfig": {"mash-browsers": {}}})
        # Only its token: nothing from the host's environment.
        self.assertEqual(create["Env"], [f"MASH_BROWSER_TOKEN={derive_browser_token('secret', 'u@acme.io')}"])
        self.assertEqual(endpoint.token, derive_browser_token("secret", "u@acme.io"))

    async def test_a_host_in_a_container_reaches_the_daemon_by_name(self) -> None:
        self.daemon.add_container("myhost", running=True, config={"Image": "host", "Labels": {}})
        with patch("mash.browser.provider.socket.gethostname", return_value="myhost"):
            endpoint = await self._provider(in_container=True).ensure("u-1")

        self.assertEqual(endpoint.base_url, f"http://{browser_name('u-1', 'mash')}:9000")
        self.assertIn("id-myhost", self.daemon.networks["mash-browsers"])

    async def test_delete_removes_the_container_and_the_profile_volume(self) -> None:
        provider = self._provider()
        await provider.ensure("u-1")

        await provider.delete("u-1")

        self.assertEqual(self.daemon.containers, {})
        self.assertEqual(self.daemon.volumes, {})


if __name__ == "__main__":
    unittest.main()

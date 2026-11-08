"""SandboxManager over LocalSandboxProvider: exec, idle stop, and the cap."""

from __future__ import annotations

import asyncio
import unittest

from mash.sandbox.manager import SandboxCapacityError, SandboxManager
from mash.sandbox.provider import SandboxEndpoint, SandboxInfo
from mash.testing.sandboxes import LocalSandboxProvider


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SandboxManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = _Clock()
        self.provider = LocalSandboxProvider()
        self.manager = SandboxManager(
            self.provider, idle_seconds=900, max_running=2, clock=self.clock
        )
        await self.manager.start()

    async def asyncTearDown(self) -> None:
        await self.manager.close()

    async def _exec(self, user_id: str, command: str, *, agent_id: str = "research") -> dict:
        return await self.manager.exec(user_id, agent_id=agent_id, command=command, timeout=10)

    async def test_each_user_has_their_own_sandbox_and_each_agent_its_folder(self) -> None:
        await self._exec("u-1", "echo mine > notes.txt")
        await self._exec("u-1", "echo writer > notes.txt", agent_id="writer")

        self.assertEqual((await self._exec("u-1", "cat notes.txt"))["output"], "mine")
        self.assertEqual((await self._exec("u-2", "ls"))["output"], "")
        workspaces = self.provider.workspaces("u-1")
        self.assertEqual((workspaces / "writer" / "notes.txt").read_text(), "writer\n")
        self.assertEqual(sorted(self.manager.running()), ["u-1", "u-2"])

    async def test_idle_sandbox_stops_and_restarts_with_its_files(self) -> None:
        await self._exec("u-1", "echo kept > notes.txt")

        self.clock.now += 899
        self.assertEqual(await self.manager.reap_idle(), [])
        self.clock.now += 1
        self.assertEqual(await self.manager.reap_idle(), ["u-1"])
        self.assertEqual(self.manager.running(), [])

        self.assertEqual((await self._exec("u-1", "cat notes.txt"))["output"], "kept")
        self.assertEqual(self.provider.ensure_calls, ["u-1", "u-1"])

    async def test_file_reads_and_writes_start_the_sandbox_and_count_as_use(self) -> None:
        written = await self.manager.write_file(
            "u-1", agent_id="research", path="in/data.csv", data=b"a,b\n1,2\n"
        )
        self.assertEqual(written.status_code, 200)
        self.assertEqual(self.provider.ensure_calls, ["u-1"])

        self.clock.now += 600
        read = await self.manager.read_file("u-1", agent_id="research", path="in/data.csv")
        self.assertEqual(read.content, b"a,b\n1,2\n")
        self.clock.now += 600
        self.assertEqual(await self.manager.reap_idle(), [])

        missing = await self.manager.read_file("u-1", agent_id="research", path="nope")
        self.assertEqual(missing.status_code, 404)

    async def test_stdin_and_env_reach_the_command(self) -> None:
        result = await self.manager.exec(
            "u-1",
            agent_id="research",
            command='cat; echo " $MASH_FILES_URL"',
            timeout=10,
            stdin="from stdin",
            env={"MASH_FILES_URL": "http://h/s/t/files/research"},
        )
        self.assertEqual(result["output"], "from stdin http://h/s/t/files/research")

    async def test_a_sandbox_with_an_exec_in_flight_is_not_stopped(self) -> None:
        running = asyncio.create_task(self._exec("u-1", "sleep 1; echo done"))
        while not self.provider.ensure_calls:
            await asyncio.sleep(0.01)
        self.clock.now += 10_000

        self.assertEqual(await self.manager.reap_idle(), [])
        self.assertEqual((await running)["output"], "done")

    async def test_cap_stops_the_least_recently_used_idle_sandbox(self) -> None:
        await self._exec("u-1", "true")
        self.clock.now += 1
        await self._exec("u-2", "true")
        self.clock.now += 1

        await self._exec("u-3", "true")

        self.assertEqual(self.provider.stop_calls, ["u-1"])
        self.assertEqual(sorted(self.manager.running()), ["u-2", "u-3"])

    async def test_cap_with_every_sandbox_busy_is_an_error(self) -> None:
        busy = [asyncio.create_task(self._exec(user, "sleep 1")) for user in ("u-1", "u-2")]
        while len(set(self.provider.ensure_calls)) < 2:
            await asyncio.sleep(0.01)

        with self.assertRaises(SandboxCapacityError):
            await self._exec("u-3", "true")
        await asyncio.gather(*busy)
        self.assertNotIn("u-3", self.manager.running())

    async def test_after_a_restart_running_sandboxes_count_as_active(self) -> None:
        await self.provider.ensure("u-9")
        manager = SandboxManager(self.provider, idle_seconds=900, clock=self.clock)
        await manager.start()
        try:
            self.assertEqual(manager.running(), ["u-9"])
            self.clock.now += 899
            self.assertEqual(await manager.reap_idle(), [])
            self.clock.now += 1
            self.assertEqual(await manager.reap_idle(), ["u-9"])
        finally:
            await manager.close()


class _SlowStopProvider:
    """Records overlapping ensure/stop calls per user; stops wait on ``release``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.stopping: set[str] = set()
        self.stop_calls: list[str] = []
        self.overlaps: list[str] = []

    async def ensure(self, user_id: str) -> SandboxEndpoint:
        if user_id in self.stopping:
            self.overlaps.append(f"ensure {user_id}")
        return SandboxEndpoint(user_id=user_id, base_url=f"http://{user_id}", token="t")

    async def stop(self, user_id: str) -> None:
        if user_id in self.stopping:
            self.overlaps.append(f"stop {user_id}")
        self.stop_calls.append(user_id)
        self.stopping.add(user_id)
        try:
            await self.release.wait()
        finally:
            self.stopping.discard(user_id)

    async def list(self) -> list[SandboxInfo]:
        return []

    async def close(self) -> None:
        return None


class EvictionTests(unittest.IsolatedAsyncioTestCase):
    async def test_making_room_waits_for_a_stop_already_running(self) -> None:
        clock = _Clock()
        provider = _SlowStopProvider()
        manager = SandboxManager(provider, idle_seconds=900, max_running=1, clock=clock)
        await manager.endpoint("u-1")
        clock.now += 900

        reaping = asyncio.create_task(manager.reap_idle())
        while not provider.stopping:
            await asyncio.sleep(0)
        # The reaper is stopping u-1; u-2 needs u-1's slot.
        starting = asyncio.create_task(manager.endpoint("u-2"))
        for _ in range(10):
            await asyncio.sleep(0)
        provider.release.set()

        self.assertEqual(await reaping, ["u-1"])
        self.assertEqual((await starting).user_id, "u-2")
        self.assertEqual(provider.overlaps, [])
        self.assertEqual(provider.stop_calls, ["u-1"])
        self.assertEqual(manager.running(), ["u-2"])


class _CountingProvider:
    """Tracks running containers and concurrent stops; stops wait on ``release``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.running: set[str] = set()
        self.most_running = 0
        self.stopping = 0
        self.most_stopping = 0

    async def ensure(self, user_id: str) -> SandboxEndpoint:
        self.running.add(user_id)
        self.most_running = max(self.most_running, len(self.running))
        return SandboxEndpoint(user_id=user_id, base_url=f"http://{user_id}", token="t")

    async def stop(self, user_id: str) -> None:
        self.stopping += 1
        self.most_stopping = max(self.most_stopping, self.stopping)
        try:
            await self.release.wait()
        finally:
            self.stopping -= 1
            self.running.discard(user_id)

    async def list(self) -> list[SandboxInfo]:
        return []

    async def close(self) -> None:
        return None


class ParallelEvictionTests(unittest.IsolatedAsyncioTestCase):
    async def test_making_room_for_two_users_stops_two_containers_at_once(self) -> None:
        clock = _Clock()
        provider = _CountingProvider()
        manager = SandboxManager(provider, idle_seconds=900, max_running=2, clock=clock)
        await manager.endpoint("u-1")
        clock.now += 1
        await manager.endpoint("u-2")

        starts = [asyncio.create_task(manager.endpoint(user)) for user in ("u-3", "u-4")]
        for _ in range(50):
            if provider.stopping == 2:
                break
            await asyncio.sleep(0)
        # Both stops run while neither has finished: the capacity lock is not
        # held across a stop.
        self.assertEqual(provider.stopping, 2)
        provider.release.set()

        self.assertEqual(
            sorted(endpoint.user_id for endpoint in await asyncio.gather(*starts)),
            ["u-3", "u-4"],
        )
        self.assertEqual(provider.most_stopping, 2)
        self.assertLessEqual(provider.most_running, 2)
        self.assertEqual(sorted(manager.running()), ["u-3", "u-4"])

    async def test_a_caller_waits_for_a_stop_already_making_room(self) -> None:
        clock = _Clock()
        provider = _CountingProvider()
        manager = SandboxManager(provider, idle_seconds=900, max_running=1, clock=clock)
        await manager.endpoint("u-1")

        first = asyncio.create_task(manager.endpoint("u-2"))
        for _ in range(50):
            if provider.stopping:
                break
            await asyncio.sleep(0)
        # u-1 is the only idle container and it is already being stopped, so
        # u-3 waits for that stop rather than failing at the cap.
        second = asyncio.create_task(manager.endpoint("u-3"))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(second.done())
        provider.release.set()

        self.assertEqual((await first).user_id, "u-2")
        self.assertEqual((await second).user_id, "u-3")
        self.assertLessEqual(provider.most_running, 1)
        self.assertEqual(manager.running(), ["u-3"])

    async def test_a_full_pool_of_busy_containers_still_refuses(self) -> None:
        provider = _CountingProvider()
        manager = SandboxManager(provider, idle_seconds=900, max_running=1, clock=_Clock())
        async with manager.hold("u-1"):
            with self.assertRaises(SandboxCapacityError):
                await manager.endpoint("u-2")


if __name__ == "__main__":
    unittest.main()

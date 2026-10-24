"""The sandbox daemon on a localhost port, with bash as the current user."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pwd
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn

from mash.sandbox.daemon import (
    BASH_MAX_OUTPUT_CHARS,
    BASH_MAX_OUTPUT_LINES,
    SandboxDaemon,
    build_bash_env,
    resolve_bash_user,
    run_command,
)
from mash.sandbox.tokens import derive_sandbox_token

TOKEN = derive_sandbox_token("secret", "u-1")


class _Server(uvicorn.Server):
    def capture_signals(self):  # type: ignore[override]
        return contextlib.nullcontext()


class SandboxDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp()
        daemon = SandboxDaemon(token=TOKEN, workspaces_root=self.root)
        self.server = _Server(
            uvicorn.Config(
                daemon.app, host="127.0.0.1", port=0, log_level="warning", ws="none"
            )
        )
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30,
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.server.should_exit = True
        await self.task

    async def _exec(self, command: str, **body) -> dict:
        response = await self.client.post(
            "/exec", json={"agent_id": "research", "command": command, **body}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_health_needs_no_token(self) -> None:
        response = await httpx.AsyncClient(base_url=self.client.base_url).get("/health")
        self.assertEqual(response.json(), {"status": "ok"})

    async def test_bad_token_is_refused(self) -> None:
        for headers in ({}, {"Authorization": f"Bearer {derive_sandbox_token('secret', 'u-2')}"}):
            with self.subTest(headers=headers):
                response = await self.client.post(
                    "/exec",
                    json={"agent_id": "research", "command": "touch x"},
                    headers={"Authorization": ""} | headers,
                )
                self.assertEqual(response.status_code, 401)
        self.assertFalse(os.path.exists(os.path.join(self.root, "research")))

    async def test_runs_in_a_folder_per_agent(self) -> None:
        result = await self._exec("pwd; echo notes > notes.txt")
        self.assertEqual(result["output"], os.path.join(self.root, "research"))
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, "research", "notes.txt")))

        other = await self.client.post("/exec", json={"agent_id": "writer", "command": "ls"})
        self.assertEqual(other.json()["output"], "")

        again = await self._exec("cat notes.txt")
        self.assertEqual(again["output"], "notes")

    async def test_reports_exit_codes(self) -> None:
        result = await self._exec("echo failing >&2; exit 3")
        self.assertEqual(result, {"output": "failing", "exit_code": 3, "timed_out": False})

    async def test_timeout_kills_the_command_and_its_children(self) -> None:
        marker = os.path.join(self.root, "late")
        started = time.monotonic()
        result = await self._exec(f"(sleep 3; touch {marker}) & sleep 3", timeout=1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        await asyncio.sleep(3)
        self.assertFalse(os.path.exists(marker))

    async def test_background_jobs_do_not_hold_the_call(self) -> None:
        started = time.monotonic()
        result = await self._exec("sleep 5 >/dev/null 2>&1 & echo started")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["output"], "started")

    async def test_truncates_output(self) -> None:
        many = await self._exec(f"seq 1 {BASH_MAX_OUTPUT_LINES + 10}")
        self.assertIn(f"{BASH_MAX_OUTPUT_LINES + 10} total lines", many["output"])
        self.assertNotIn(f"\n{BASH_MAX_OUTPUT_LINES + 1}\n", many["output"])

        wide = await self._exec(f"printf 'x%.0s' $(seq 1 {BASH_MAX_OUTPUT_CHARS + 10})")
        self.assertIn(f"max {BASH_MAX_OUTPUT_CHARS} chars", wide["output"])

    async def test_environment_is_clean(self) -> None:
        with patch.dict(os.environ, {"MASH_SANDBOX_TOKEN": TOKEN, "MASH_HOST_SECRET": "secret"}):
            result = await self._exec("env")
        names = {line.split("=", 1)[0] for line in result["output"].splitlines()}
        self.assertNotIn("MASH_SANDBOX_TOKEN", names)
        self.assertNotIn("MASH_HOST_SECRET", names)

    async def test_rejects_unsafe_agent_ids_and_bad_bodies(self) -> None:
        for body in (
            {"agent_id": "../escape", "command": "ls"},
            {"agent_id": "", "command": "ls"},
            {"agent_id": "research", "command": ""},
            {"agent_id": "research", "command": "ls", "timeout": 0},
            {"agent_id": "research", "command": "ls", "timeout": True},
        ):
            with self.subTest(body=body):
                response = await self.client.post("/exec", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")


class BashUserTests(unittest.TestCase):
    def _agent_entry(self, uid: int) -> pwd.struct_passwd:
        return pwd.struct_passwd(("agent", "x", uid, uid, "", "/home/agent", "/bin/bash"))

    def test_runs_as_current_user_when_agent_user_missing(self) -> None:
        with patch("mash.sandbox.daemon.pwd.getpwnam", side_effect=KeyError("agent")):
            self.assertIsNone(resolve_bash_user())

    def test_runs_as_agent_when_agent_user_exists(self) -> None:
        with patch(
            "mash.sandbox.daemon.pwd.getpwnam",
            return_value=self._agent_entry(os.geteuid() + 1),
        ):
            self.assertEqual(resolve_bash_user(), "agent")

    def test_runs_as_current_user_when_already_agent(self) -> None:
        with patch(
            "mash.sandbox.daemon.pwd.getpwnam",
            return_value=self._agent_entry(os.geteuid()),
        ):
            self.assertIsNone(resolve_bash_user())

    def test_agent_bash_starts_through_sudo_with_clean_env(self) -> None:
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("stop here")

        with patch.dict(os.environ, {"MASH_DATABASE_URL": "postgresql://secret"}), patch(
            "mash.sandbox.daemon.pwd.getpwnam",
            return_value=self._agent_entry(os.geteuid() + 1),
        ), patch("mash.sandbox.daemon.asyncio.create_subprocess_exec", fake_exec):
            with self.assertRaises(RuntimeError):
                asyncio.run(run_command("ls", folder="/w/research", user="agent"))

        [(args, kwargs)] = calls
        self.assertEqual(args[:5], ("sudo", "-n", "-u", "agent", "/bin/bash"))
        self.assertEqual(args[-2:], ("/w/research", "ls"))
        self.assertEqual(set(kwargs["env"]), {"HOME", "PATH", "LANG", "PWD"})
        self.assertEqual(kwargs["env"]["HOME"], "/home/agent")
        self.assertEqual(kwargs["env"]["PWD"], "/w/research")

    def test_env_for_the_current_user_keeps_only_home_path_and_locale(self) -> None:
        with patch.dict(os.environ, {"MASH_DATABASE_URL": "postgresql://secret"}):
            env = build_bash_env(None, "/tmp")
        self.assertEqual(set(env), {"HOME", "PATH", "LANG", "PWD"})


if __name__ == "__main__":
    unittest.main()

"""sandbox_put and sandbox_get stay inside the spec's file roots."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from mash.logging import bound_request_id
from mash.tools.sandbox import (
    FileRoots,
    RunCodeTool,
    SandboxGetTool,
    SandboxPutTool,
    _read_local,
    _write_local,
    build_sandbox_tools,
)


def _tools(roots):
    return build_sandbox_tools(
        host_api_url="http://host", token="tok", agent_id="research", file_roots=roots
    )


class FileRootsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "exports"
        self.root.mkdir()
        self.outside = self.base / "secrets.json"
        self.outside.write_text("{}")
        self.roots = FileRoots([self.root])

    def test_roots_must_be_absolute_and_outside_pseudo_filesystems(self) -> None:
        with self.assertRaises(ValueError):
            FileRoots(["exports"])
        with self.assertRaises(ValueError):
            FileRoots(["/proc"])
        with self.assertRaises(ValueError):
            FileRoots([])

    def test_a_path_inside_a_root_resolves(self) -> None:
        (self.root / "rows.csv").write_text("a\n")

        self.assertEqual(self.roots.resolve(str(self.root / "rows.csv")), self.root / "rows.csv")
        self.assertEqual(self.roots.resolve("rows.csv"), self.root / "rows.csv")

    def test_a_path_outside_the_roots_is_refused(self) -> None:
        for raw in (str(self.outside), "../secrets.json", "/etc/passwd"):
            with self.assertRaises(ValueError, msg=raw):
                self.roots.resolve(raw)

    def test_a_symlink_out_of_a_root_is_refused(self) -> None:
        (self.root / "link.json").symlink_to(self.outside)

        with self.assertRaises(ValueError):
            self.roots.resolve("link.json")

    def test_pseudo_filesystems_are_refused_even_under_a_root(self) -> None:
        roots = FileRoots(["/"])

        with self.assertRaises(ValueError):
            roots.resolve("/proc/self/environ")
        with self.assertRaises(ValueError):
            roots.resolve("/dev/null")

    def test_only_regular_files_are_read(self) -> None:
        with self.assertRaises(ValueError):
            _read_local(self.root)

    def test_a_write_through_a_symlinked_folder_is_refused(self) -> None:
        (self.root / "out").symlink_to(self.base)

        with self.assertRaises(ValueError):
            _write_local(self.roots, self.root / "out" / "x.txt", b"x")
        self.assertFalse((self.base / "x.txt").exists())

    def test_a_write_inside_a_root_creates_its_folders(self) -> None:
        _write_local(self.roots, self.root / "a" / "b.txt", b"hi")

        self.assertEqual((self.root / "a" / "b.txt").read_bytes(), b"hi")


class BuildSandboxToolsTests(unittest.TestCase):
    def test_without_roots_the_agent_gets_run_code_and_edit_file(self) -> None:
        tools = _tools(())

        self.assertEqual([tool.name for tool in tools], ["run_code", "edit_file"])
        self.assertIsInstance(tools[0], RunCodeTool)
        self.assertNotIn("sandbox_put", tools[0].description)

    def test_with_roots_the_file_tools_name_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            run_code, _, put, get = _tools([root])

        self.assertIsInstance(put, SandboxPutTool)
        self.assertIsInstance(get, SandboxGetTool)
        for tool in (run_code, put, get):
            self.assertIn(str(root), tool.description)


class FileToolRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        _, _, self.put, self.get = _tools([self.root])

    def _run(self, tool, args):
        with bound_request_id("r-1"):
            return asyncio.run(tool.execute(args))

    def test_sandbox_put_refuses_the_process_environment(self) -> None:
        result = self._run(self.put, {"src": "/proc/self/environ", "dest": "env"})

        self.assertTrue(result.is_error)
        self.assertIn("outside the folders you can use", result.content)

    def test_sandbox_put_refuses_a_file_outside_the_roots(self) -> None:
        result = self._run(self.put, {"src": os.path.expanduser("~/.ssh/id_rsa"), "dest": "k"})

        self.assertTrue(result.is_error)
        self.assertIn(str(self.root), result.content)

    def test_sandbox_get_refuses_a_destination_outside_the_roots(self) -> None:
        result = self._run(self.get, {"src": "report.csv", "dest": "/tmp/../etc/cron.d/x"})

        self.assertTrue(result.is_error)
        self.assertIn("outside the folders you can use", result.content)


if __name__ == "__main__":
    unittest.main()

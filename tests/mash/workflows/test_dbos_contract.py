"""The DBOS surface Mash depends on, checked against the installed package.

The rest of the suite fakes DBOS, so a release that resolves a DBOS version
Mash cannot drive still passes it. Issue #185 was exactly that: the published
dependency admitted DBOS 3, whose queues are no longer constructible directly,
and the host died during workflow registration. These tests bind the code to
the real package so the mismatch fails here instead of at a customer's
startup.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import inspect
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

from mash.workflows.dbos import _load_dbos_api

try:  # pragma: no cover - 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    import tomli as tomllib

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def _declared_dbos_specifier() -> Requirement:
    """The dbos bound Mash publishes, read from pyproject.

    Read from source rather than installed metadata: a dev checkout keeps
    stale ``.egg-info`` directories on the path, and what ships to PyPI is the
    thing under test.
    """
    if not _PYPROJECT.is_file():  # pragma: no cover - installed, not a checkout
        raise unittest.SkipTest("pyproject.toml is not available")
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    for raw in data["project"]["dependencies"]:
        requirement = Requirement(raw)
        if requirement.name == "dbos":
            return requirement
    raise AssertionError("mashpy does not declare a dbos dependency")


class DeclaredDependencyTests(unittest.TestCase):
    def test_installed_dbos_satisfies_the_declared_range(self) -> None:
        requirement = _declared_dbos_specifier()
        installed = importlib_metadata.version("dbos")
        self.assertTrue(
            requirement.specifier.contains(installed, prereleases=True),
            f"installed dbos {installed} is outside the declared '{requirement}'",
        )

    def test_the_declared_range_has_an_upper_bound(self) -> None:
        # An unbounded floor lets a resolver pick a future major release whose
        # API this code has never been run against.
        requirement = _declared_dbos_specifier()
        upper_bounds = [
            spec for spec in requirement.specifier if spec.operator in {"<", "<=", "=="}
        ]
        self.assertTrue(
            upper_bounds,
            f"dbos is declared as '{requirement}' with no upper bound",
        )

    def test_every_version_in_range_shares_one_major(self) -> None:
        requirement = _declared_dbos_specifier()
        installed = Version(importlib_metadata.version("dbos"))
        next_major = Version(f"{installed.major + 1}.0.0")
        self.assertFalse(
            requirement.specifier.contains(next_major, prereleases=True),
            f"dbos '{requirement}' admits {next_major}, an untested major release",
        )


class LoadedApiTests(unittest.TestCase):
    """Everything ``_load_dbos_api`` hands back must exist and be callable."""

    def test_load_dbos_api_resolves_against_the_installed_package(self) -> None:
        dbos_class, set_workflow_id, set_enqueue_options, dedup_error = _load_dbos_api()
        self.assertTrue(callable(set_workflow_id))
        self.assertTrue(callable(set_enqueue_options))
        self.assertTrue(issubclass(dedup_error, Exception))
        self.assertTrue(hasattr(dbos_class, "workflow"))

    def test_the_dbos_methods_the_runtime_calls_all_exist(self) -> None:
        dbos_class, *_ = _load_dbos_api()
        for name in (
            "workflow",
            "launch",
            "run_step_async",
            "start_workflow_async",
            "get_workflow_status_async",
            "resume_workflow_async",
            "cancel_workflow_async",
            "send_async",
            "recv_async",
            "register_queue_async",
            "retrieve_queue",
        ):
            with self.subTest(method=name):
                self.assertTrue(
                    callable(getattr(dbos_class, name, None)),
                    f"DBOS.{name} is missing from the installed dbos",
                )


class QueueRegistrationTests(unittest.TestCase):
    def test_queues_are_declared_through_register_queue(self) -> None:
        # The queue's concurrency cap has to survive the API it is declared
        # through, so pin the keyword Mash passes.
        dbos_class, *_ = _load_dbos_api()
        signature = inspect.signature(dbos_class.register_queue_async)
        self.assertIn("global_concurrency", signature.parameters)
        self.assertIn("name", signature.parameters)

    def test_constructing_a_queue_directly_is_not_supported(self) -> None:
        # Issue #185's actual failure. If a future DBOS re-allows this, the
        # test tells us the workaround it forced is no longer needed.
        import dbos

        with self.assertRaises(Exception) as caught:
            dbos.Queue("mash.contract.check", concurrency=1)
        self.assertIn("register_queue", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

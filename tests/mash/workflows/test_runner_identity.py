"""Durable runner identity: a persisted run must resolve after a restart."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mash.runtime.host.host import Pool
from mash.workflows.dbos import (
    DEFAULT_RUNNER_ID,
    MASH_RUNNER_ID_ENV,
    make_run_id,
    register_runner,
    require_runner,
    resolve_runner_id,
    unregister_runner,
    workflow_run_id_prefix,
)


class ResolveRunnerIdTests(unittest.TestCase):
    def test_default_is_stable_across_calls(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MASH_RUNNER_ID_ENV, None)
            self.assertEqual(resolve_runner_id(), DEFAULT_RUNNER_ID)
            self.assertEqual(resolve_runner_id(), resolve_runner_id())

    def test_environment_overrides_the_default(self) -> None:
        with patch.dict(os.environ, {MASH_RUNNER_ID_ENV: "blue"}):
            self.assertEqual(resolve_runner_id(), "blue")

    def test_explicit_value_wins_over_the_environment(self) -> None:
        with patch.dict(os.environ, {MASH_RUNNER_ID_ENV: "blue"}):
            self.assertEqual(resolve_runner_id("green"), "green")

    def test_a_colon_is_rejected_because_run_ids_are_colon_delimited(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_id("blue:green")

    def test_blank_falls_back_rather_than_producing_an_empty_id(self) -> None:
        with patch.dict(os.environ, {MASH_RUNNER_ID_ENV: "   "}):
            self.assertEqual(resolve_runner_id("  "), DEFAULT_RUNNER_ID)


class RunnerRegistryTests(unittest.TestCase):
    """The registry is how a durable run finds the pool that owns it."""

    def setUp(self) -> None:
        self.registered: list[tuple[str, Pool]] = []

    def tearDown(self) -> None:
        for runner_id, pool in self.registered:
            unregister_runner(runner_id, pool)

    def _start(self, pool: Pool) -> Pool:
        register_runner(pool.runner_id, pool)
        self.registered.append((pool.runner_id, pool))
        return pool

    def test_a_persisted_run_resolves_after_a_restart(self) -> None:
        # Issue #187: a run queued by one process carries its runner id in the
        # durable workflow arguments. The process dies; a new one comes up
        # against the same database and must be able to resolve that id.
        old = self._start(Pool(runner_id="deploy-a"))
        persisted_run_id = make_run_id(old.runner_id, "changelog")
        unregister_runner(old.runner_id, old)

        new = self._start(Pool(runner_id="deploy-a"))

        self.assertIsNot(new, old)
        self.assertEqual(new.runner_id, old.runner_id)
        self.assertIs(require_runner("deploy-a"), new)
        self.assertTrue(
            persisted_run_id.startswith(
                workflow_run_id_prefix(new.runner_id, "changelog")
            )
        )

    def test_a_restart_with_the_configured_default_also_resolves(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MASH_RUNNER_ID_ENV, None)
            old = self._start(Pool())
            unregister_runner(old.runner_id, old)
            new = self._start(Pool())
        self.assertIs(require_runner(DEFAULT_RUNNER_ID), new)

    def test_separate_deployments_stay_isolated(self) -> None:
        # Recovery must not route one deployment's persisted runs into
        # whichever pool happens to be up.
        blue = self._start(Pool(runner_id="blue"))
        green = self._start(Pool(runner_id="green"))

        self.assertIs(require_runner("blue"), blue)
        self.assertIs(require_runner("green"), green)
        self.assertNotEqual(
            workflow_run_id_prefix("blue", "wf"),
            workflow_run_id_prefix("green", "wf"),
        )

    def test_an_unknown_runner_id_still_fails_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            require_runner("never-registered")

    def test_two_pools_cannot_share_one_runner_id(self) -> None:
        self._start(Pool(runner_id="blue"))
        with self.assertRaises(ValueError) as caught:
            self._start(Pool(runner_id="blue"))
        self.assertIn(MASH_RUNNER_ID_ENV, str(caught.exception))

    def test_re_registering_the_same_pool_is_idempotent(self) -> None:
        pool = self._start(Pool(runner_id="blue"))
        register_runner(pool.runner_id, pool)
        self.assertIs(require_runner("blue"), pool)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

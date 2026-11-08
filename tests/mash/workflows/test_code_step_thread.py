"""A sync CodeStep runs off the host's event loop, and its timeout fires."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

from pydantic import BaseModel

from mash.workflows import CodeStep, StepContext, WorkflowSpec
from mash.workflows.engine import WorkflowExecutionContext, _run_code_step


class In(BaseModel):
    seconds: float


class Out(BaseModel):
    slept: float


def _sleep(inp: In, _ctx: StepContext) -> Out:
    time.sleep(inp.seconds)
    return Out(slept=inp.seconds)


async def _sleep_async(inp: In, _ctx: StepContext) -> Out:
    await asyncio.sleep(inp.seconds)
    return Out(slept=inp.seconds)


def _boom(_inp: In, _ctx: StepContext) -> Out:
    raise ValueError("kaboom")


def _context(step: CodeStep) -> WorkflowExecutionContext:
    workflow = WorkflowSpec(workflow_id="wf-thread", input_model=In, steps=[step])
    return WorkflowExecutionContext(
        pool=object(),  # type: ignore[arg-type]
        workflow=workflow,
        run_id="run-1",
        workflow_input={},
    )


async def _run(step: CodeStep, seconds: float) -> dict:
    return await _run_code_step(_context(step), step, {"seconds": seconds}, 1)


class SyncCodeStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_body_lets_other_coroutines_run(self) -> None:
        step = CodeStep(step_id="sleep", run=_sleep, input=In, output=Out)
        ticks = 0
        done = asyncio.Event()

        async def _ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.05)
                ticks += 1

        ticker = asyncio.create_task(_ticker())
        out = await _run(step, 1)
        done.set()
        await ticker

        self.assertEqual(out, {"slept": 1.0})
        self.assertGreaterEqual(ticks, 10)

    async def test_sync_body_timeout_fails_the_step_at_the_deadline(self) -> None:
        step = CodeStep(step_id="sleep", run=_sleep, input=In, output=Out, timeout_s=0.5)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "exceeded its 0.5s timeout"):
            await _run(step, 2)
        self.assertLess(time.monotonic() - started, 1.2)

    async def test_sync_body_runs_in_a_daemon_thread(self) -> None:
        seen: list[threading.Thread] = []

        def _record(inp: In, _ctx: StepContext) -> Out:
            seen.append(threading.current_thread())
            return Out(slept=0)

        step = CodeStep(step_id="record", run=_record, input=In, output=Out)
        await _run(step, 0)
        self.assertIsNot(seen[0], threading.main_thread())
        self.assertTrue(seen[0].daemon)

    async def test_sync_body_error_fails_the_step(self) -> None:
        step = CodeStep(step_id="boom", run=_boom, input=In, output=Out)
        with self.assertRaisesRegex(ValueError, "kaboom"):
            await _run(step, 0)

    async def test_async_body_timeout_still_fires(self) -> None:
        step = CodeStep(step_id="sleep", run=_sleep_async, input=In, output=Out, timeout_s=0.3)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "exceeded its 0.3s timeout"):
            await _run(step, 2)
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()

"""Tests for the v2 workflow step specs and build-time pipeline validation."""

from __future__ import annotations

import unittest

from pydantic import BaseModel

from mash.testing.runtime_fixtures import build_spec
from mash.workflows import (
    AgentStep,
    CodeStep,
    StepContext,
    WorkflowSpec,
    validate_step_pipeline,
)


class TriggerIn(BaseModel):
    repo_url: str


class ScanOut(BaseModel):
    files_changed: list[str]
    head_sha: str


class SummaryOut(BaseModel):
    summary: str
    head_sha: str


class NeedsMissing(BaseModel):
    missing_field: str


def _scan(_inp: TriggerIn, _ctx: StepContext) -> ScanOut:  # pragma: no cover - not run in Phase 0
    return ScanOut(files_changed=[], head_sha="deadbeef")


class StepConstructionTests(unittest.TestCase):
    def test_code_step_valid(self) -> None:
        step = CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut)
        self.assertEqual(step.kind, "code")
        self.assertEqual(step.step_id, "scan")
        self.assertIs(step.input, TriggerIn)
        self.assertIsNone(step.timeout_s)

    def test_agent_step_valid_with_agent_id(self) -> None:
        step = AgentStep(
            step_id="summarize", agent_id="writer", input=ScanOut, output=SummaryOut
        )
        self.assertEqual(step.kind, "agent")
        self.assertEqual(step.agent_id, "writer")

    def test_agent_step_derives_agent_id_from_spec(self) -> None:
        spec = build_spec(agent_id="writer", response_text="{}")
        step = AgentStep(step_id="summarize", agent_spec=spec, input=ScanOut, output=SummaryOut)
        self.assertEqual(step.agent_id, "writer")

    def test_agent_step_id_mismatch_rejected(self) -> None:
        spec = build_spec(agent_id="writer", response_text="{}")
        with self.assertRaises(ValueError):
            AgentStep(
                step_id="summarize",
                agent_id="other",
                agent_spec=spec,
                input=ScanOut,
                output=SummaryOut,
            )

    def test_empty_step_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodeStep(step_id="  ", run=_scan, input=TriggerIn, output=ScanOut)

    def test_non_model_io_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodeStep(step_id="scan", run=_scan, input=dict, output=ScanOut)  # type: ignore[arg-type]

    def test_agent_step_requires_agent(self) -> None:
        with self.assertRaises(ValueError):
            AgentStep(step_id="summarize", input=ScanOut, output=SummaryOut)

    def test_non_callable_run_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodeStep(step_id="scan", run=object(), input=TriggerIn, output=ScanOut)  # type: ignore[arg-type]

    def test_non_positive_timeout_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut, timeout_s=0)


class PipelineValidationTests(unittest.TestCase):
    def test_valid_pipeline_builds(self) -> None:
        workflow = WorkflowSpec(
            workflow_id="changelog",
            input_model=TriggerIn,
            steps=[
                CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut),
                AgentStep(
                    step_id="summarize", agent_id="writer", input=ScanOut, output=SummaryOut
                ),
            ],
        )
        self.assertEqual([s.step_id for s in workflow.steps], ["scan", "summarize"])

    def test_duplicate_step_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowSpec(
                workflow_id="dup",
                steps=[
                    CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut),
                    CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut),
                ],
            )

    def test_empty_steps_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_step_pipeline("empty", [], None)

    def test_adjacency_missing_field_rejected(self) -> None:
        # Second step requires a field neither workflow_input nor scan output provides.
        with self.assertRaises(ValueError) as ctx:
            WorkflowSpec(
                workflow_id="broken",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut),
                    AgentStep(
                        step_id="summarize",
                        agent_id="writer",
                        input=NeedsMissing,
                        output=SummaryOut,
                    ),
                ],
            )
        self.assertIn("missing_field", str(ctx.exception))

    def test_adjacency_satisfied_by_workflow_input(self) -> None:
        # First step's required field comes from workflow_input, not a prior step.
        WorkflowSpec(
            workflow_id="ok",
            input_model=TriggerIn,
            steps=[CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut)],
        )

    def test_adjacency_skipped_without_input_model(self) -> None:
        # No input_model => workflow_input fields unknown => adjacency not enforced.
        WorkflowSpec(
            workflow_id="loose",
            steps=[
                CodeStep(step_id="scan", run=_scan, input=TriggerIn, output=ScanOut),
                AgentStep(
                    step_id="summarize",
                    agent_id="writer",
                    input=NeedsMissing,
                    output=SummaryOut,
                ),
            ],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The projection of runtime events onto OpenTelemetry spans and log records."""

from __future__ import annotations

import unittest

from otel_fixtures import RequestEvents, completed_request

from mash.otel.ids import span_id_hex
from mash.otel.projection import project_request, serialize_projection
from mash.otel.workflows import project_workflow_run
from mash.workflows.store import WorkflowRunRecord, WorkflowStepRecord


def _by_name(projection):
    return {span.name: span for span in projection.spans}


class CompletedRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projection = project_request(completed_request().events, capture_content=False)
        self.spans = _by_name(self.projection)

    def test_span_names_follow_the_genai_conventions(self) -> None:
        self.assertEqual(
            sorted(self.spans),
            sorted(
                [
                    "invoke_agent assistant",
                    "mash.cold_start",
                    "mash.context_load",
                    "mash.step 0",
                    "mash.step 1",
                    "chat claude-test",
                    "execute_tool search",
                ]
            ),
        )
        # Two chat spans share a name; both are there.
        self.assertEqual(len([s for s in self.projection.spans if s.name.startswith("chat")]), 2)

    def test_tree_nests_steps_under_the_root_and_calls_under_steps(self) -> None:
        root = self.spans["invoke_agent assistant"]
        self.assertIsNone(root.parent_span_id)
        step0 = self.spans["mash.step 0"]
        self.assertEqual(step0.parent_span_id, root.span_id)
        self.assertEqual(self.spans["execute_tool search"].parent_span_id, step0.span_id)
        for name in ("mash.cold_start", "mash.context_load"):
            self.assertEqual(self.spans[name].parent_span_id, root.span_id)

    def test_times_come_from_the_paired_events(self) -> None:
        root = self.spans["invoke_agent assistant"]
        self.assertEqual((root.start_time, root.end_time), (100.0, 104.8))
        self.assertEqual(
            (self.spans["mash.cold_start"].start_time, self.spans["mash.cold_start"].end_time),
            (100.0, 100.5),
        )
        tool = self.spans["execute_tool search"]
        self.assertEqual((tool.start_time, tool.end_time), (102.0, 103.5))

    def test_attributes_are_copied_from_the_events(self) -> None:
        chat = next(s for s in self.projection.spans if s.key == "chat.0")
        self.assertEqual(chat.kind, "CLIENT")
        self.assertEqual(chat.attributes["gen_ai.provider.name"], "anthropic")
        self.assertEqual(chat.attributes["gen_ai.usage.input_tokens"], 100)
        self.assertEqual(chat.attributes["gen_ai.usage.cache_read.input_tokens"], 50)
        self.assertEqual(chat.attributes["gen_ai.response.finish_reasons"], ["end_turn"])
        root = self.spans["invoke_agent assistant"]
        self.assertEqual(root.attributes["gen_ai.conversation.id"], "s-1")
        self.assertEqual(root.status, "UNSET")

    def test_ids_are_derived_from_the_request(self) -> None:
        self.assertEqual(self.projection.trace_id, "11111111222233334444555555555555")
        root = self.spans["invoke_agent assistant"]
        self.assertEqual(root.span_id, span_id_hex("req-1", "root"))
        again = project_request(completed_request().events, capture_content=False)
        self.assertEqual(
            [s.span_id for s in again.spans], [s.span_id for s in self.projection.spans]
        )

    def test_no_content_and_no_log_records_by_default(self) -> None:
        self.assertEqual(self.projection.logs, ())
        tool = self.spans["execute_tool search"]
        self.assertNotIn("gen_ai.tool.call.arguments", tool.attributes)

    def test_serialized_tree_carries_children_and_logs(self) -> None:
        tree = serialize_projection(self.projection)["root"]
        self.assertEqual(tree["name"], "invoke_agent assistant")
        names = [child["name"] for child in tree["children"]]
        self.assertIn("mash.step 0", names)
        self.assertEqual(tree["logs"], [])


class ContentCaptureTests(unittest.TestCase):
    def test_capture_adds_tool_io_and_the_inference_details_event(self) -> None:
        projection = project_request(completed_request().events, capture_content=True)
        spans = _by_name(projection)
        tool = spans["execute_tool search"]
        self.assertEqual(tool.attributes["gen_ai.tool.call.arguments"], '{"q": "x"}')
        self.assertEqual(tool.attributes["gen_ai.tool.call.result"], "ok")
        details = [r for r in projection.logs if r.event_name == "gen_ai.client.inference.operation.details"]
        self.assertEqual(len(details), 2)
        chat = next(s for s in projection.spans if s.key == "chat.1")
        last = next(r for r in details if r.span_id == chat.span_id)
        messages = last.attributes["gen_ai.output.messages"]
        self.assertEqual(messages[0]["parts"][0], {"type": "text", "content": "answer"})


class FailureTests(unittest.TestCase):
    def test_a_failed_model_call_closes_its_spans_as_errors(self) -> None:
        events = (
            RequestEvents()
            .accepted(100.0)
            .context(100.1, 100.2)
            .chat_start(0, 100.3)
            .end(101.0, "error", error="rate limited")
            .events
        )
        projection = project_request(events, capture_content=False)
        chat = next(s for s in projection.spans if s.key == "chat.0")
        root = projection.span(projection.root_span_id)
        self.assertEqual((chat.status, chat.end_time), ("ERROR", 101.0))
        self.assertEqual(chat.attributes["error.type"], "RuntimeError")
        self.assertEqual(root.status, "ERROR")
        self.assertEqual(root.status_message, "rate limited")
        step = next(s for s in projection.spans if s.key == "step.0")
        self.assertEqual(step.end_time, 101.0)
        [exception] = projection.logs
        self.assertEqual(exception.event_name, "exception")
        self.assertEqual(exception.span_id, chat.span_id)
        self.assertEqual(exception.attributes["exception.message"], "rate limited")
        self.assertEqual(exception.severity_text, "ERROR")

    def test_a_failed_tool_call_is_an_error_span_with_an_exception_record(self) -> None:
        events = (
            RequestEvents()
            .accepted(100.0)
            .chat(0, 100.1, 100.2, tool_calls=[{"id": "c1", "name": "lookup"}])
            .tool(0, "c1", "lookup", 100.3, 100.4, error=True, content="not found")
            .step_end(0, 100.5)
            .chat(1, 100.5, 100.6)
            .step_end(1, 100.7)
            .end(100.8)
            .events
        )
        projection = project_request(events, capture_content=False)
        tool = _by_name(projection)["execute_tool lookup"]
        self.assertEqual(tool.status, "ERROR")
        [exception] = projection.logs
        self.assertEqual(exception.span_id, tool.span_id)
        self.assertEqual(exception.attributes, {"exception.type": "tool_error"})
        self.assertEqual(projection.span(projection.root_span_id).status, "UNSET")

    def test_a_cancelled_request_ends_unset_and_says_so(self) -> None:
        events = RequestEvents().accepted(100.0).chat_start(0, 100.1).end(100.5, "cancelled").events
        projection = project_request(events, capture_content=False)
        root = projection.span(projection.root_span_id)
        self.assertEqual(root.status, "UNSET")
        self.assertEqual(root.attributes["mash.request.status"], "cancelled")
        self.assertTrue(all(s.end_time == 100.5 for s in projection.spans if s is not root))
        self.assertEqual(projection.logs, ())


class RunningRequestTests(unittest.TestCase):
    def test_open_spans_have_no_end_time(self) -> None:
        events = (
            RequestEvents()
            .accepted(100.0)
            .context(100.1, 100.2)
            .chat(0, 100.3, 101.0, tool_calls=[{"id": "c1", "name": "slow"}])
            .tool_start(0, "c1", "slow", 101.1)
            .events
        )
        projection = project_request(events, capture_content=False)
        spans = _by_name(projection)
        self.assertIsNone(spans["invoke_agent assistant"].end_time)
        self.assertIsNone(spans["mash.step 0"].end_time)
        self.assertIsNone(spans["execute_tool slow"].end_time)
        self.assertEqual(spans["chat claude-test"].end_time, 101.0)
        self.assertEqual(projection.observed_at, 101.1)
        self.assertAlmostEqual(spans["invoke_agent assistant"].duration_ms(projection.observed_at), 1100.0)

    def test_an_approval_wait_is_a_span_under_its_step(self) -> None:
        events = (
            RequestEvents()
            .accepted(100.0)
            .chat(0, 100.1, 100.2, tool_calls=[{"id": "c1", "name": "deploy"}])
            .interaction("itr_1", "approval", 100.3)
            .events
        )
        projection = project_request(events, capture_content=False)
        wait = _by_name(projection)["mash.interaction approval"]
        self.assertIsNone(wait.end_time)
        self.assertEqual(wait.parent_span_id, _by_name(projection)["mash.step 0"].span_id)
        self.assertEqual(wait.attributes["mash.interaction.type"], "approval")

    def test_resume_and_mode_changes_are_log_records_on_the_root(self) -> None:
        events = (
            RequestEvents()
            .accepted(100.0)
            .mode_changed(100.1, "bg")
            .end(100.5, "cancelled")
            .resumed(101.0)
            .events
        )
        projection = project_request(events, capture_content=False)
        root = projection.span(projection.root_span_id)
        self.assertIsNone(root.end_time)
        self.assertEqual(
            [(r.event_name, r.span_id) for r in projection.logs],
            [
                ("mash.request.mode_changed", root.span_id),
                ("mash.request.resumed", root.span_id),
            ],
        )


class SubagentTests(unittest.TestCase):
    def test_children_nest_under_the_call_that_started_them(self) -> None:
        parent = (
            RequestEvents("parent")
            .accepted(100.0)
            .chat(0, 100.1, 100.2, tool_calls=[{"id": "c1", "name": "RunSubAgents"}])
            .tool(
                0, "c1", "RunSubAgents", 100.3, 102.0,
                subagents=[
                    {"agent_id": "research", "request_id": "child-a"},
                    {"agent_id": "assistant", "request_id": "child-b"},
                ],
            )
            .step_end(0, 102.1)
            .end(102.2)
        )
        child_a = (
            RequestEvents("child-a", agent_id="research", trace_id="99999999-0000-0000-0000-000000000000",
                          parent_request_id="parent")
            .accepted(100.4).chat(0, 100.5, 101.0).step_end(0, 101.1).end(101.2)
        )
        child_b = (
            RequestEvents("child-b", trace_id="88888888-0000-0000-0000-000000000000",
                          parent_request_id="parent")
            .accepted(100.4).chat(0, 100.5, 101.5).step_end(0, 101.6).end(101.7)
        )
        projection = project_request(
            parent.events,
            children={"child-a": child_a.events, "child-b": child_b.events},
            capture_content=False,
        )
        call = _by_name(projection)["execute_tool RunSubAgents"]
        children = [s for s in projection.spans if s.parent_span_id == call.span_id]
        self.assertEqual(
            sorted(s.name for s in children),
            ["invoke_agent assistant", "invoke_agent research"],
        )
        # One trace: the children take the root's trace id, not their own.
        self.assertEqual({s.trace_id for s in projection.spans}, {projection.trace_id})


class TraceParentTests(unittest.TestCase):
    def test_a_stored_traceparent_puts_the_request_in_the_callers_trace(self) -> None:
        traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        events = RequestEvents(traceparent=traceparent).accepted(100.0).end(101.0).events
        projection = project_request(events, capture_content=False)
        root = projection.span(projection.root_span_id)
        self.assertEqual(projection.trace_id, "0af7651916cd43dd8448eb211c80319c")
        self.assertEqual(root.parent_span_id, "b7ad6b7169203331")

    def test_an_invalid_traceparent_is_ignored(self) -> None:
        events = RequestEvents(traceparent="00-zz-bad-01").accepted(100.0).end(101.0).events
        projection = project_request(events, capture_content=False)
        self.assertEqual(projection.trace_id, "11111111222233334444555555555555")
        self.assertIsNone(projection.span(projection.root_span_id).parent_span_id)


class WorkflowTests(unittest.TestCase):
    def test_an_agent_step_parents_the_request_it_submitted(self) -> None:
        run = WorkflowRunRecord(
            run_id="run-1", workflow_id="changelog", status="completed",
            created_at=99.0, started_at=99.5, finished_at=106.0,
        )
        steps = [
            WorkflowStepRecord(run_id="run-1", workflow_id="changelog", step_id="scan",
                               ordinal=0, kind="code", status="completed",
                               started_at=99.6, finished_at=99.9),
            WorkflowStepRecord(run_id="run-1", workflow_id="changelog", step_id="summarize",
                               ordinal=1, kind="agent", status="completed",
                               agent_request_id="req-1", started_at=100.0, finished_at=105.0),
        ]
        request = completed_request(workflow_run_id="run-1")
        projection = project_workflow_run(
            run, steps, {"req-1": (request.events, {})}, capture_content=False
        )
        spans = _by_name(projection)
        root = spans["mash.workflow changelog"]
        self.assertIsNone(root.parent_span_id)
        step = spans["mash.workflow.step summarize"]
        self.assertEqual(step.parent_span_id, root.span_id)
        self.assertEqual(spans["invoke_agent assistant"].parent_span_id, step.span_id)
        self.assertEqual({s.trace_id for s in projection.spans}, {projection.trace_id})
        self.assertEqual(spans["mash.workflow.step scan"].attributes["mash.workflow.step.kind"], "code")


if __name__ == "__main__":
    unittest.main()

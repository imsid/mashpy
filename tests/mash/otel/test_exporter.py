"""The OTLP exporter, against the SDK's in-memory exporters."""

from __future__ import annotations

import unittest

from otel_fixtures import RequestEvents, completed_request

from conftest import build_test_stores

try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:  # pragma: no cover - the otel extra is not installed
    InMemorySpanExporter = None  # type: ignore[assignment,misc]

from mash.otel.exporter import OtlpExporter, otlp_endpoint_configured
from mash.otel.ids import span_id_hex


@unittest.skipIf(InMemorySpanExporter is None, "mashpy[otel] is not installed")
class OtlpExporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store, _ = build_test_stores()
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.metrics = InMemoryMetricReader()
        self.exporter = self._exporter()

    def _exporter(self, metric_reader=None) -> OtlpExporter:
        return OtlpExporter(
            self.store,
            span_exporter=self.spans,
            log_exporter=self.logs,
            metric_reader=metric_reader or self.metrics,
            batch=False,
        )

    async def _seed(self, request: RequestEvents) -> None:
        for event in request.events:
            await self.store.append_event(event)

    async def test_a_finished_request_exports_its_spans(self) -> None:
        await self._seed(completed_request())

        self.assertEqual(await self.exporter.run_once(), 1)

        finished = self.spans.get_finished_spans()
        names = sorted(span.name for span in finished)
        self.assertIn("invoke_agent assistant", names)
        self.assertIn("execute_tool search", names)
        root = next(s for s in finished if s.name == "invoke_agent assistant")
        self.assertEqual(format(root.context.trace_id, "032x"), "11111111222233334444555555555555")
        self.assertEqual(format(root.context.span_id, "016x"), span_id_hex("req-1", "root"))
        self.assertIsNone(root.parent)
        self.assertEqual(root.resource.attributes["service.name"], "mash")
        self.assertEqual(root.instrumentation_scope.name, "mash")
        chat = next(s for s in finished if s.name == "chat claude-test")
        self.assertEqual(chat.kind.name, "CLIENT")
        self.assertEqual(chat.attributes["gen_ai.usage.input_tokens"], 100)
        self.assertEqual(list(chat.attributes["gen_ai.response.finish_reasons"]), ["end_turn"])
        self.assertEqual(chat.start_time, int(101.0 * 1e9))

    async def test_a_running_request_is_not_exported(self) -> None:
        await self._seed(RequestEvents().accepted(100.0).chat_start(0, 100.1))
        self.assertEqual(await self.exporter.run_once(), 0)
        self.assertEqual(self.spans.get_finished_spans(), ())

    async def test_the_cursor_keeps_a_restart_from_exporting_again(self) -> None:
        await self._seed(completed_request())
        await self.exporter.run_once()
        count = len(self.spans.get_finished_spans())

        restarted = self._exporter(InMemoryMetricReader())
        self.assertEqual(await restarted.run_once(), 0)
        self.assertEqual(len(self.spans.get_finished_spans()), count)

        await self._seed(completed_request("req-2", trace_id="22222222-2222-2222-2222-222222222222"))
        self.assertEqual(await restarted.run_once(), 1)

    async def test_failures_export_exception_log_records(self) -> None:
        await self._seed(
            RequestEvents().accepted(100.0).chat_start(0, 100.1).end(101.0, "error", error="boom")
        )
        await self.exporter.run_once()

        [record] = self.logs.get_finished_logs()
        log = record.log_record
        self.assertEqual(log.event_name, "exception")
        self.assertEqual(log.attributes["exception.message"], "boom")
        chat = next(s for s in self.spans.get_finished_spans() if s.name.startswith("chat"))
        self.assertEqual(log.span_id, chat.context.span_id)
        self.assertEqual(log.trace_id, chat.context.trace_id)
        self.assertEqual(chat.status.status_code.name, "ERROR")

    async def test_a_child_is_exported_with_its_root(self) -> None:
        parent = (
            RequestEvents("parent")
            .accepted(100.0)
            .chat(0, 100.1, 100.2, tool_calls=[{"id": "c1", "name": "RunSubAgents"}])
            .tool(0, "c1", "RunSubAgents", 100.3, 102.0,
                  subagents=[{"agent_id": "research", "request_id": "child"}])
            .step_end(0, 102.1)
        )
        child = (
            RequestEvents("child", agent_id="research", trace_id="99999999-0000-0000-0000-000000000000",
                          parent_request_id="parent")
            .accepted(100.4).chat(0, 100.5, 101.0).step_end(0, 101.1).end(101.2)
        )
        await self._seed(parent)
        await self._seed(child)
        # The child ended first, but it waits for its root.
        self.assertEqual(await self.exporter.run_once(), 0)

        parent.end(102.2)
        await self.store.append_event(parent.events[-1])
        self.assertEqual(await self.exporter.run_once(), 1)

        finished = self.spans.get_finished_spans()
        self.assertEqual({s.context.trace_id for s in finished}, {finished[0].context.trace_id})
        child_root = next(s for s in finished if s.name == "invoke_agent research")
        call = next(s for s in finished if s.name == "execute_tool RunSubAgents")
        self.assertEqual(child_root.parent.span_id, call.context.span_id)

    async def test_chat_spans_record_the_genai_client_metrics(self) -> None:
        await self._seed(completed_request())
        await self.exporter.run_once()

        data = self.metrics.get_metrics_data()
        metrics = {
            metric.name: metric
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        self.assertEqual(
            set(metrics), {"gen_ai.client.token.usage", "gen_ai.client.operation.duration"}
        )
        usage = metrics["gen_ai.client.token.usage"].data.data_points
        by_type = {p.attributes["gen_ai.token.type"]: p.sum for p in usage}
        self.assertEqual(by_type, {"input": 300, "output": 50})
        duration = metrics["gen_ai.client.operation.duration"].data.data_points
        self.assertEqual(sum(p.count for p in duration), 2)


class _WorkflowStore:
    def __init__(self, finished, steps) -> None:
        self._finished = finished
        self._steps = steps

    async def list_finished_runs(self, *, after_seq: int = 0, limit: int = 200):
        return [item for item in self._finished if item["finished_seq"] > after_seq][:limit]

    async def get_run_steps(self, run_id: str):
        return [step for step in self._steps if step.run_id == run_id]


@unittest.skipIf(InMemorySpanExporter is None, "mashpy[otel] is not installed")
class WorkflowExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_run_exports_once_with_its_agent_request_nested(self) -> None:
        from mash.workflows.store import WorkflowRunRecord, WorkflowStepRecord

        store, _ = build_test_stores()
        request = completed_request(workflow_run_id="run-1")
        for event in request.events:
            await store.append_event(event)
        run = WorkflowRunRecord(
            run_id="run-1", workflow_id="changelog", status="completed",
            created_at=99.0, started_at=99.5, finished_at=106.0,
        )
        step = WorkflowStepRecord(
            run_id="run-1", workflow_id="changelog", step_id="summarize", ordinal=0,
            kind="agent", status="completed", agent_request_id="req-1",
            started_at=100.0, finished_at=105.0,
        )
        spans = InMemorySpanExporter()
        exporter = OtlpExporter(
            store,
            workflow_store=_WorkflowStore([{"run": run, "finished_seq": 1}], [step]),
            span_exporter=spans,
            log_exporter=InMemoryLogRecordExporter(),
            metric_reader=InMemoryMetricReader(),
            batch=False,
        )

        # The request belongs to the run, so it goes out with the run only.
        self.assertEqual(await exporter.run_once(), 1)
        self.assertEqual(await exporter.run_once(), 0)

        finished = spans.get_finished_spans()
        names = [s.name for s in finished]
        self.assertEqual(names.count("invoke_agent assistant"), 1)
        root = next(s for s in finished if s.name == "mash.workflow changelog")
        step_span = next(s for s in finished if s.name == "mash.workflow.step summarize")
        agent = next(s for s in finished if s.name == "invoke_agent assistant")
        self.assertIsNone(root.parent)
        self.assertEqual(agent.parent.span_id, step_span.context.span_id)
        self.assertEqual({s.context.trace_id for s in finished}, {root.context.trace_id})


class EndpointTests(unittest.TestCase):
    def test_off_without_an_endpoint(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(otlp_endpoint_configured())
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}):
            self.assertTrue(otlp_endpoint_configured())


if __name__ == "__main__":
    unittest.main()

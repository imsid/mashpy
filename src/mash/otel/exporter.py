"""Export the projection over OTLP.

The exporter runs in the host process. It tails ``runtime_event_log`` by
``event_id`` and, when a request's ``invoke_agent.end`` is stored, projects
the request (its RunSubAgents children nested) and hands the ended spans to a
``BatchSpanProcessor`` and the log records to a ``BatchLogRecordProcessor``.
Its position is kept in ``otel_export_cursor``, so a restart picks up where
it stopped. On its first run there is no position yet, and it starts at the
end of the log: requests and workflow runs that finished before the exporter
was enabled are not exported.

A child request is exported with its root, and a request that belongs to a
workflow run is exported with the run. The export happens after the fact, so
every span's and log record's ids and timestamps come from the projection,
never from an active span.

Needs the ``mashpy[otel]`` extra. It is off unless an OTLP endpoint is set
(``OTEL_EXPORTER_OTLP_ENDPOINT`` or the per-signal traces/logs variables);
headers, protocol and ``OTEL_SERVICE_NAME`` come from the standard variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Iterable

from ..runtime.events.types import RuntimeEventType
from . import semconv
from .loading import load_request_tree
from .projection import (
    KIND_CLIENT,
    STATUS_ERROR,
    LogRecord,
    Projection,
    Span,
    project_request,
)

logger = logging.getLogger(__name__)

REQUESTS_CURSOR = "requests"
WORKFLOWS_CURSOR = "workflows"
_SCOPE_NAME = "mash"

_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)
# The GenAI client metrics' bucket boundaries, from the conventions.
_TOKEN_BUCKETS = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864)
_DURATION_BUCKETS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92)


def otlp_endpoint_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in _ENDPOINT_VARS)


def _ns(seconds: float) -> int:
    return int(round(seconds * 1_000_000_000))


def _attribute_value(value: Any) -> Any:
    """An attribute value OTLP accepts: a primitive or a list of one primitive type."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
    ):
        return list(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {k: _attribute_value(v) for k, v in attributes.items() if v is not None}


class OtlpExporter:
    """Tails the event log and exports finished requests (and workflow runs).

    ``span_exporter``/``log_exporter``/``metric_reader`` default to the OTLP
    HTTP exporters; tests pass the SDK's in-memory ones. ``workflow_store``
    enables the export of workflow runs.

    Each exported ``chat`` span also records the two GenAI client metrics,
    ``gen_ai.client.token.usage`` and ``gen_ai.client.operation.duration``.
    """

    def __init__(
        self,
        runtime_store: Any,
        *,
        workflow_store: Any = None,
        span_exporter: Any = None,
        log_exporter: Any = None,
        metric_reader: Any = None,
        poll_interval: float = 2.0,
        batch_size: int = 200,
        resource: dict[str, Any] | None = None,
        batch: bool = True,
    ) -> None:
        # Imported here so a deployment without the extra never loads the SDK.
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope

        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            span_exporter = OTLPSpanExporter()
        if log_exporter is None:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter,
            )

            log_exporter = OTLPLogExporter()
        self._store = runtime_store
        self._workflow_store = workflow_store
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._resource_attributes = resource
        span_processor_class = BatchSpanProcessor if batch else SimpleSpanProcessor
        log_processor_class = BatchLogRecordProcessor if batch else SimpleLogRecordProcessor
        self._span_processor = span_processor_class(span_exporter)
        self._log_processor = log_processor_class(log_exporter)
        self._Resource = Resource
        self._metric_reader = metric_reader
        self._meter_provider: Any = None
        self._token_usage: Any = None
        self._operation_duration: Any = None
        self._scope = InstrumentationScope(
            _SCOPE_NAME, _package_version(), schema_url=semconv.SCHEMA_URL
        )
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="mash-otel-exporter")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
        await asyncio.to_thread(self._span_processor.shutdown)
        await asyncio.to_thread(self._log_processor.shutdown)
        if self._meter_provider is not None:
            await asyncio.to_thread(self._meter_provider.shutdown)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                exported = await self.run_once()
            except Exception:  # pragma: no cover - keep the loop alive
                logger.exception("otel export failed; retrying")
                exported = 0
            if exported:
                continue
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        """Export what ended since the cursors; returns how many traces went out."""
        exported = await self._export_requests()
        if self._workflow_store is not None:
            exported += await self._export_workflows()
        return exported

    async def flush(self) -> None:
        await asyncio.to_thread(self._span_processor.force_flush)
        await asyncio.to_thread(self._log_processor.force_flush)
        if self._meter_provider is not None:
            await asyncio.to_thread(self._meter_provider.force_flush)

    async def _cursor(self, name: str, head: Any) -> int:
        """The cursor ``name``; on the first run, set to the current ``head()``."""
        cursor = await self._store.get_export_cursor(name)
        if cursor is None:
            cursor = int(await head())
            await self._store.set_export_cursor(name, cursor)
        return cursor

    async def _export_requests(self) -> int:
        cursor = await self._cursor(REQUESTS_CURSOR, self._store.latest_event_id)
        ends = await self._store.list_request_ends(
            after_event_id=cursor, limit=self._batch_size
        )
        exported = 0
        for end in ends:
            if end.request_id and not (
                end.workflow_run_id and self._workflow_store is not None
            ):
                events, children = await load_request_tree(self._store, end.request_id)
                if events and not _is_child(events):
                    self.export_projection(project_request(events, children=children))
                    exported += 1
        if ends:
            await self._store.set_export_cursor(REQUESTS_CURSOR, ends[-1].event_id)
        return exported

    async def _export_workflows(self) -> int:
        from .workflows import load_workflow_projection

        cursor = await self._cursor(
            WORKFLOWS_CURSOR, self._workflow_store.latest_finished_seq
        )
        runs = await self._workflow_store.list_finished_runs(
            after_seq=cursor, limit=self._batch_size
        )
        for run in runs:
            projection = await load_workflow_projection(
                self._workflow_store, self._store, run
            )
            if projection is not None:
                self.export_projection(projection)
        if runs:
            await self._store.set_export_cursor(
                WORKFLOWS_CURSOR, int(runs[-1]["finished_seq"])
            )
        return len(runs)

    def export_projection(self, projection: Projection) -> None:
        """Hand a projection's ended spans and its log records to the processors."""
        resource = self._Resource.create(
            {**projection.resource, **(self._resource_attributes or {})}
        )
        for span in projection.spans:
            if span.end_time is not None:
                self._span_processor.on_end(self._readable_span(span, resource))
                if span.operation == semconv.OP_CHAT:
                    self._record_metrics(span, projection.resource)
        for record in projection.logs:
            self._log_processor.on_emit(self._log_record(record, resource))

    def _meters(self, resource_attributes: dict[str, Any]) -> tuple[Any, Any]:
        if self._token_usage is None:
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.view import (
                ExplicitBucketHistogramAggregation,
                View,
            )

            reader = self._metric_reader
            if reader is None:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
                from opentelemetry.sdk.metrics.export import (
                    PeriodicExportingMetricReader,
                )

                reader = PeriodicExportingMetricReader(OTLPMetricExporter())
            self._meter_provider = MeterProvider(
                metric_readers=[reader],
                resource=self._Resource.create(
                    {**resource_attributes, **(self._resource_attributes or {})}
                ),
                views=[
                    View(
                        instrument_name="gen_ai.client.token.usage",
                        aggregation=ExplicitBucketHistogramAggregation(_TOKEN_BUCKETS),
                    ),
                    View(
                        instrument_name="gen_ai.client.operation.duration",
                        aggregation=ExplicitBucketHistogramAggregation(_DURATION_BUCKETS),
                    ),
                ],
            )
            meter = self._meter_provider.get_meter(
                _SCOPE_NAME, _package_version(), schema_url=semconv.SCHEMA_URL
            )
            self._token_usage = meter.create_histogram(
                "gen_ai.client.token.usage",
                unit="{token}",
                description="Number of input and output tokens used.",
            )
            self._operation_duration = meter.create_histogram(
                "gen_ai.client.operation.duration",
                unit="s",
                description="GenAI operation duration.",
            )
        return self._token_usage, self._operation_duration

    def _record_metrics(self, span: Span, resource_attributes: dict[str, Any]) -> None:
        token_usage, operation_duration = self._meters(resource_attributes)
        attributes = {
            name: span.attributes[name]
            for name in (
                semconv.GEN_AI_OPERATION_NAME,
                semconv.GEN_AI_PROVIDER_NAME,
                semconv.GEN_AI_REQUEST_MODEL,
            )
            if span.attributes.get(name) is not None
        }
        if span.status == STATUS_ERROR:
            attributes[semconv.ERROR_TYPE] = str(
                span.attributes.get(semconv.ERROR_TYPE) or "error"
            )
        operation_duration.record(span.duration_ms() / 1000.0, attributes)
        for token_type, name in (
            ("input", semconv.GEN_AI_USAGE_INPUT_TOKENS),
            ("output", semconv.GEN_AI_USAGE_OUTPUT_TOKENS),
        ):
            count = span.attributes.get(name)
            if count is not None:
                token_usage.record(
                    int(count), {**attributes, semconv.GEN_AI_TOKEN_TYPE: token_type}
                )

    def _readable_span(self, span: Span, resource: Any) -> Any:
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode

        flags = TraceFlags(TraceFlags.SAMPLED)
        context = SpanContext(
            trace_id=int(span.trace_id, 16),
            span_id=int(span.span_id, 16),
            is_remote=False,
            trace_flags=flags,
        )
        parent = None
        if span.parent_span_id:
            parent = SpanContext(
                trace_id=int(span.trace_id, 16),
                span_id=int(span.parent_span_id, 16),
                is_remote=False,
                trace_flags=flags,
            )
        status = (
            Status(StatusCode.ERROR, span.status_message)
            if span.status == STATUS_ERROR
            else Status(StatusCode.UNSET)
        )
        return ReadableSpan(
            name=span.name,
            context=context,
            parent=parent,
            resource=resource,
            attributes=_attributes(span.attributes),
            kind=SpanKind.CLIENT if span.kind == KIND_CLIENT else SpanKind.INTERNAL,
            status=status,
            start_time=_ns(span.start_time),
            end_time=_ns(span.end_time or span.start_time),
            instrumentation_scope=self._scope,
        )

    def _log_record(self, record: LogRecord, resource: Any) -> Any:
        from opentelemetry._logs import LogRecord as ApiLogRecord
        from opentelemetry._logs import SeverityNumber
        from opentelemetry.sdk._logs import ReadWriteLogRecord
        from opentelemetry.trace import TraceFlags

        api_record = ApiLogRecord(
            timestamp=_ns(record.timestamp),
            observed_timestamp=_ns(record.timestamp),
            trace_id=int(record.trace_id, 16),
            span_id=int(record.span_id, 16) if record.span_id else 0,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            severity_text=record.severity_text,
            severity_number=SeverityNumber(record.severity_number),
            body=record.body,
            attributes=dict(record.attributes),
            event_name=record.event_name,
        )
        return ReadWriteLogRecord(
            api_record, resource=resource, instrumentation_scope=self._scope
        )


def _is_child(events: Iterable[Any]) -> bool:
    for event in events:
        if event.event_type == RuntimeEventType.INVOKE_AGENT_START.value:
            return bool((event.payload or {}).get("parent_request_id"))
    return False


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("mashpy")
    except Exception:  # pragma: no cover
        return "0"


def build_exporter(runtime_store: Any, *, workflow_store: Any = None) -> OtlpExporter | None:
    """The host's exporter, or None when no endpoint is set or the extra is missing."""
    if not otlp_endpoint_configured():
        return None
    try:
        return OtlpExporter(runtime_store, workflow_store=workflow_store)
    except ImportError:
        logger.warning(
            "An OTLP endpoint is set but OpenTelemetry is not installed; "
            "install mashpy[otel] to export traces."
        )
        return None

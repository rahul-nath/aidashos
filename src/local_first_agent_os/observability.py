# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import socket
import sys
import time
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from prometheus_client import Counter, Gauge, Histogram

from .coordination.failures import exceptional_failure
from .ids import sha256_text
from .settings import Settings

WORKFLOW_RUNS_TOTAL = Counter(
    "local_agent_workflow_runs_total",
    "Workflow runs by type and terminal status.",
    ["workflow_type", "status"],
)
WORKFLOW_ACTIVE = Gauge(
    "local_agent_workflow_active",
    "In-flight workflow runs by type.",
    ["workflow_type"],
    multiprocess_mode="livesum",
)
WORKFLOW_LATENCY_SECONDS = Histogram(
    "local_agent_workflow_latency_seconds",
    "Workflow run latency by type and terminal status.",
    ["workflow_type", "status"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
MODEL_CALLS_TOTAL = Counter(
    "local_agent_model_calls_total",
    "Model calls by role and status.",
    ["model_role", "status"],
)
MODEL_CALL_LATENCY_SECONDS = Histogram(
    "local_agent_model_call_latency_seconds",
    "Model call latency by role and status.",
    ["model_role", "status"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
EMBEDDING_BATCH_SIZE = Histogram(
    "local_agent_embedding_batch_size",
    "Embedding batch sizes.",
    ["workflow_type"],
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256),
)
ARTIFACT_WRITES_TOTAL = Counter(
    "local_agent_artifact_writes_total",
    "Artifact writes by role and backend.",
    ["role", "backend"],
)
ARTIFACT_BYTES_TOTAL = Counter(
    "local_agent_artifact_bytes_total",
    "Artifact bytes written by role and backend.",
    ["role", "backend"],
)
# Knowledge graph layer. The split between created and merged is what the §10
# runbook reads: a merge rate that collapses means resolution stopped working,
# and a resolution-collision spike means the threshold is too loose.
GRAPH_EXTRACTION_LATENCY_SECONDS = Histogram(
    "local_agent_graph_extraction_latency_seconds",
    "Entity extraction latency per source artifact.",
    ["workflow_type"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 20, 45, 90, 180, 300),
)
GRAPH_ANALYTICS_LATENCY_SECONDS = Histogram(
    "local_agent_graph_analytics_latency_seconds",
    "Whole-graph analytics pass latency.",
    ["workflow_type"],
    buckets=(0.05, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
GRAPH_AUGMENTED_QUERY_LATENCY_SECONDS = Histogram(
    "local_agent_graph_augmented_query_latency_seconds",
    "Graph-augmented retrieval latency.",
    ["workflow_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
)
GRAPH_ENTITIES_EXTRACTED = Histogram(
    "local_agent_graph_entities_extracted",
    "Entities surviving the ontology filter, per artifact.",
    ["workflow_type"],
    buckets=(0, 1, 2, 5, 10, 20, 40, 60),
)
GRAPH_RELATIONS_EXTRACTED = Histogram(
    "local_agent_graph_relations_extracted",
    "Relations surviving the ontology filter, per artifact.",
    ["workflow_type"],
    buckets=(0, 1, 2, 5, 10, 20, 40, 60),
)
GRAPH_NODES_CREATED_TOTAL = Counter(
    "local_agent_graph_nodes_created_total",
    "Graph nodes created rather than merged onto an existing node.",
    ["workflow_type"],
)
GRAPH_NODES_MERGED_TOTAL = Counter(
    "local_agent_graph_nodes_merged_total",
    "Graph node writes that folded into an existing node.",
    ["workflow_type"],
)
GRAPH_RESOLUTION_COLLISIONS_TOTAL = Counter(
    "local_agent_graph_resolution_collisions_total",
    "Node merges decided by embedding similarity rather than exact name.",
    ["workflow_type"],
)

MEMORY_CURRENT_BYTES = Gauge(
    "local_agent_memory_current_bytes",
    "Current Python memory tracked by tracemalloc when profiling is enabled.",
)
MEMORY_PEAK_BYTES = Gauge(
    "local_agent_memory_peak_bytes",
    "Peak Python memory tracked by tracemalloc when profiling is enabled.",
)

_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "local_agent_observability_context",
    default=None,
)
_settings: Settings | None = None
_configured = False
_tracer: Any = None
_pyroscope: Any = None
_tracemalloc_enabled = False
_profiled_step_count = 0


class _ReachabilityGuardedSpanExporter(SpanExporter):
    """Drop a batch quietly when a once-live collector has disappeared.

    The wrapped OTLP exporter still owns serialization and delivery. This guard
    prevents its background worker from entering a retry/logging loop for the
    normal local case where Alloy is stopped before the host process exits.
    """

    def __init__(self, delegate: Any, endpoint: str, *, probe_timeout: float = 0.1):
        self._delegate = delegate
        self._endpoint = endpoint
        self._probe_timeout = probe_timeout

    def export(self, spans: Any) -> Any:
        if not _endpoint_reachable(self._endpoint, timeout=self._probe_timeout):
            return SpanExportResult.FAILURE
        return self._delegate.export(spans)

    def shutdown(self) -> Any:
        return self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))


class JsonLogFormatter(logging.Formatter):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def format(self, record: logging.LogRecord) -> str:
        context = _current_context()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "service": context.get("service", self.settings.service_name),
            "env": context.get("env", self.settings.env),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "workflow_type": getattr(record, "workflow_type", context.get("workflow_type", "")),
            "workflow_id": getattr(record, "workflow_id", context.get("workflow_id", "")),
            "trace_id": getattr(record, "trace_id", context.get("trace_id", current_trace_id())),
            "step": getattr(record, "step", context.get("step", "")),
        }
        for key in (
            "file_id",
            "request_id",
            "model_role",
            "artifact_id",
            "phase",
            # Without this a `task_completed` line is unreadable. That phase name
            # means the turn finished, not that it worked, and `status` is the only
            # field that says which - so dropping it made every failed task look
            # like a successful one in the only log an operator reads. The values
            # are a small closed set ("completed", "failed", "blocked", "planned"),
            # not model text, so promoting it does not widen the label space.
            "status",
            # The operator sentence every `emit_progress` caller computes. It used to
            # be discarded: the log message was the literal `dispatch_progress` on all
            # eleven call sites, so the one field that said what happened existed only
            # in an in-process terminal event that the resident loops never have.
            # A body field and not a label, deliberately - it is free-form text, and
            # `stage.labels` in the Alloy config is where that boundary is kept.
            "detail",
            # The failed task's own reasons. `401 Not logged in` lived in a
            # `PowWowTaskResult.risks` tuple that nothing forwarded, so the only copy
            # was inside `agent_execution_leases.result_json`.
            "risks",
            "intent_id",
            "task_id",
            "task_name",
            "execution_lease_id",
            "checkpoint_id",
            "promotion_state",
            "error_code",
            "category",
            "retryable",
            "operation",
            "duration_seconds",
            "memory_current_bytes",
            "memory_peak_bytes",
            "memory_top",
        ):
            value = getattr(record, key, context.get(key, ""))
            if value or isinstance(value, bool):
                payload[key] = value
        if record.exc_info:
            exception = record.exc_info[1]
            if exception is not None:
                failure = exceptional_failure(
                    exception,
                    operation=str(payload.get("operation") or record.funcName),
                )
                for key, value in failure.observability_fields().items():
                    payload.setdefault(key, value)
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_observability(settings: Settings) -> None:
    global _configured, _settings
    if _configured:
        return
    _settings = settings
    if settings.structured_logs:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter(settings))
        root = logging.getLogger()
        root.handlers[:] = [handler]
        root.setLevel(settings.log_level.upper())
    _configure_tracing(settings)
    _configure_pyroscope(settings)
    _configure_memory_profiling(settings)
    _configured = True


def _endpoint_reachable(url: str, timeout: float = 0.25) -> bool:
    """Best-effort TCP probe of an observability endpoint.

    Tracing, pyroscope, and memory profiling are gated on this so a CLI command
    never hangs exporting telemetry to a collector that isn't running. When the
    observability stack is up the probe succeeds and telemetry is wired; when it
    is down each capability stays off — no manual toggle to drift out of sync.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def instrument_fastapi_app(app: Any, settings: Settings) -> None:
    if not settings.otel_traces_enabled:
        return
    if not _endpoint_reachable(settings.otel_traces_endpoint):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        logging.getLogger(__name__).debug("fastapi_instrumentation_failed", exc_info=True)


def _configure_tracing(settings: Settings) -> None:
    global _tracer
    if not settings.otel_traces_enabled:
        return
    if not _endpoint_reachable(settings.otel_traces_endpoint):
        logging.getLogger(__name__).debug("otel endpoint unreachable; tracing disabled")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.service_name,
                    "deployment.environment": settings.env,
                    "local_agent.app": settings.app_name,
                    "service.version": settings.application_version,
                }
            )
        )
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_traces_endpoint,
            headers=settings.otel_traces_headers,
            timeout=settings.otel_traces_export_timeout_seconds,
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                _ReachabilityGuardedSpanExporter(
                    exporter,
                    settings.otel_traces_endpoint,
                )
            )
        )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("local_first_agent_os")
        HTTPXClientInstrumentor().instrument()
    except Exception:
        logging.getLogger(__name__).debug("otel_configuration_failed", exc_info=True)


def _configure_pyroscope(settings: Settings) -> None:
    global _pyroscope
    if not settings.pyroscope_enabled:
        return
    if not _endpoint_reachable(settings.pyroscope_server_address):
        logging.getLogger(__name__).debug("pyroscope endpoint unreachable; profiling disabled")
        return
    try:
        import pyroscope

        pyroscope.configure(
            application_name=settings.service_name,
            server_address=settings.pyroscope_server_address,
            sample_rate=settings.pyroscope_sample_rate,
            oncpu=True,
            gil_only=False,
            enable_logging=False,
            line_no=pyroscope.LineNo.First,
            tags={
                "service": settings.service_name,
                "env": settings.env,
                "app": settings.app_name,
            },
        )
        _pyroscope = pyroscope
    except Exception:
        logging.getLogger(__name__).debug("pyroscope_configuration_failed", exc_info=True)


def _configure_memory_profiling(settings: Settings) -> None:
    global _tracemalloc_enabled
    if not settings.memory_profiling_enabled:
        return
    # Gate on the observability collector being up, so memory profiling only
    # runs once the observability stack has been started.
    if not _endpoint_reachable(settings.otel_traces_endpoint):
        return
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)
    _tracemalloc_enabled = True


def current_trace_id() -> str:
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            return f"{span_context.trace_id:032x}"
    except Exception:
        pass
    return _current_context().get("trace_id", "")


def current_workflow_type() -> str:
    return _current_context().get("workflow_type", "")


@contextlib.contextmanager
def observability_context(**values: str | None) -> Iterator[None]:
    base = _current_context()
    for key, value in values.items():
        if value:
            base[key] = value
    if "trace_id" not in base and base.get("workflow_id"):
        base["trace_id"] = f"tr_{sha256_text(base['workflow_id'])[:16]}"
    token = _context.set(base)
    try:
        yield
    finally:
        _context.reset(token)


@contextlib.contextmanager
def profiled_step(
    step: str,
    *,
    workflow_type: str | None = None,
    workflow_id: str | None = None,
    **attributes: str | int | float | bool | None,
) -> Iterator[None]:
    service = _service_for_step(step)
    current = _current_context()
    workflow_type = workflow_type or current.get("workflow_type")
    workflow_id = workflow_id or current.get("workflow_id")
    attrs = {
        "service": service,
        "workflow_type": workflow_type,
        "workflow_id": workflow_id,
        "step": step,
    }
    with observability_context(**attrs):
        start = time.perf_counter()
        memory_before = (
            tracemalloc.take_snapshot()
            if _tracemalloc_enabled and _should_capture_memory_snapshot()
            else None
        )
        with _span(step, attrs | attributes), _profile_tags(attrs | attributes):
            try:
                yield
            except Exception:
                logging.getLogger(__name__).exception("step_failed")
                raise
            finally:
                elapsed = time.perf_counter() - start
                _observe_memory_high_water()
                _log_memory_delta(step, memory_before)
                logging.getLogger(__name__).debug(
                    "step_completed",
                    extra={"step": step, "duration_seconds": elapsed},
                )


@contextlib.contextmanager
def _span(name: str, attributes: dict[str, Any]) -> Iterator[None]:
    if _tracer is None:
        yield
        return
    clean_attributes = {key: value for key, value in attributes.items() if value is not None}
    with _tracer.start_as_current_span(name) as span:
        for key, value in clean_attributes.items():
            span.set_attribute(f"local_agent.{key}", value)
        try:
            yield
        except Exception as error:
            failure = exceptional_failure(error, operation=name)
            for key, value in failure.observability_fields().items():
                span.set_attribute(f"local_agent.{key}", value)
            span.record_exception(error)
            raise


@contextlib.contextmanager
def _profile_tags(attributes: dict[str, Any]) -> Iterator[None]:
    if _pyroscope is None:
        yield
        return
    tags = {
        key: str(value)
        for key, value in attributes.items()
        if value is not None and key in {"service", "workflow_type", "step", "model_role"}
    }
    with _pyroscope.tag_wrapper(tags):
        yield


def _service_for_step(step: str) -> str | None:
    if step in {"chandra_ocr"}:
        return "ocr-worker"
    if step == "embedding_worker":
        return "embedding-worker"
    if step.startswith("llama_cpp"):
        return "llama-cpp"
    return None


def _current_context() -> dict[str, str]:
    return dict(_context.get() or {})


def _log_memory_delta(step: str, before: tracemalloc.Snapshot | None) -> None:
    if before is None or _settings is None:
        return
    try:
        after = tracemalloc.take_snapshot()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        top = []
        for stat in after.compare_to(before, "lineno")[: _settings.memory_profile_top_n]:
            frame = stat.traceback[0]
            top.append(
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "size_diff_bytes": stat.size_diff,
                    "count_diff": stat.count_diff,
                }
            )
        logging.getLogger(__name__).info(
            "memory_profile",
            extra={
                "step": step,
                "memory_current_bytes": current_bytes,
                "memory_peak_bytes": peak_bytes,
                "memory_top": top,
            },
        )
    except Exception:
        logging.getLogger(__name__).debug("memory_profile_failed", exc_info=True)


def _should_capture_memory_snapshot() -> bool:
    global _profiled_step_count
    if _settings is None:
        return False
    _profiled_step_count += 1
    return (_profiled_step_count - 1) % _settings.memory_profile_sample_every == 0


def _observe_memory_high_water() -> None:
    if not _tracemalloc_enabled:
        return
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    MEMORY_CURRENT_BYTES.set(current_bytes)
    MEMORY_PEAK_BYTES.set(peak_bytes)

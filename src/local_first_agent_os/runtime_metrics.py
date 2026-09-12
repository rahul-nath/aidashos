# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded runtime measurements, independent of ledger and model execution."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

logger = logging.getLogger(__name__)
_SECONDS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)


class EventTransaction(StrEnum):
    RAW_EVENT = "raw_event"
    USAGE_PROJECTION = "usage_projection"


EVENT_TRANSACTION_SECONDS = Histogram(
    "local_agent_execution_event_transaction_seconds",
    "Event transaction including pool checkout, statements and commit acknowledgement.",
    ["operation", "outcome"],
    buckets=_SECONDS,
)
EVENT_COMMIT_SECONDS = Histogram(
    "local_agent_execution_event_commit_seconds",
    "Client commit call through acknowledgement; excludes checkout and SQL statements.",
    ["operation", "outcome"],
    buckets=_SECONDS,
)
EVENTS_PENDING = Gauge(
    "local_agent_execution_events_pending",
    "Normalized events waiting for sequence ownership or durable append completion.",
    ["harness", "source"],
    multiprocess_mode="livesum",
)
STREAM_BUFFER_BYTES = Gauge(
    "local_agent_execution_stream_buffer_bytes",
    "Sampled unread asyncio pipe-buffer bytes; excludes kernel pipes and normalized events.",
    ["harness", "source"],
    multiprocess_mode="livesum",
)
STREAM_BUFFER_PEAK_BYTES = Histogram(
    "local_agent_execution_stream_buffer_peak_bytes",
    "Maximum sampled unread pipe-buffer bytes per supervised stream.",
    ["harness", "source"],
    buckets=(0, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304),
)
CANCELLATION_SECONDS = Histogram(
    "local_agent_execution_cancellation_seconds",
    "Cancellation timing: request observation uses wall clocks, other phases use monotonic time.",
    ["harness", "phase"],
    buckets=_SECONDS,
)
INVALID_TIMING_TOTAL = Counter(
    "local_agent_execution_invalid_timing_total",
    "Wall-clock cancellation observations omitted because the request is in the future.",
)
_transaction: contextvars.ContextVar[EventTransaction | None] = contextvars.ContextVar(
    "measured_event_transaction",
    default=None,
)


@contextmanager
def event_transaction(operation: EventTransaction) -> Iterator[None]:
    token = _transaction.set(operation)
    started = time.monotonic()
    outcome = "failed"
    try:
        yield
        outcome = "committed"
    finally:
        EVENT_TRANSACTION_SECONDS.labels(operation, outcome).observe(time.monotonic() - started)
        _transaction.reset(token)


@contextmanager
def event_commit() -> Iterator[None]:
    operation = _transaction.get()
    if operation is None:
        yield
        return
    started = time.monotonic()
    outcome = "failed"
    try:
        yield
        outcome = "committed"
    finally:
        EVENT_COMMIT_SECONDS.labels(operation, outcome).observe(time.monotonic() - started)


def harness_label(harness: str) -> str:
    return harness if harness in {"codex", "claude", "pi"} else "other"


class SupervisionMetrics:
    """One invocation's deltas, so concurrent invocations cannot clear one another."""

    def __init__(self, harness: str) -> None:
        self.harness = harness_label(harness)
        self._buffers: dict[str, int] = {}
        self._peaks: dict[str, int] = {}
        self.cancel_observed_at: float | None = None
        self.signal_sent_at: float | None = None
        self.exit_observed_at: float | None = None

    @contextmanager
    def pending(self, source: str) -> Iterator[None]:
        gauge = EVENTS_PENDING.labels(self.harness, source)
        gauge.inc()
        try:
            yield
        finally:
            gauge.dec()

    def sample_buffer(self, source: str, stream: asyncio.StreamReader) -> None:
        # CPython exposes no public unread-size API. Keep that assumption here;
        # the supported Python runtime is exercised by the subprocess regression.
        buffer = getattr(stream, "_buffer", None)
        if not isinstance(buffer, (bytes, bytearray)):
            raise TypeError("unsupported StreamReader buffer representation")
        size = len(buffer)
        STREAM_BUFFER_BYTES.labels(self.harness, source).inc(size - self._buffers.get(source, 0))
        self._buffers[source] = size
        self._peaks[source] = max(size, self._peaks.get(source, 0))

    def close_buffers(self) -> None:
        for source, size in self._buffers.items():
            STREAM_BUFFER_BYTES.labels(self.harness, source).dec(size)
            STREAM_BUFFER_PEAK_BYTES.labels(self.harness, source).observe(self._peaks[source])
        self._buffers.clear()
        self._peaks.clear()

    def cancel_observed(self, requested_at: object) -> None:
        if self.cancel_observed_at is not None:
            return
        self.cancel_observed_at = time.monotonic()
        if not isinstance(requested_at, str):
            return
        try:
            requested = datetime.fromisoformat(requested_at)
            if requested.tzinfo is None:
                return
            elapsed = time.time() - requested.timestamp()
        except ValueError:
            return
        if elapsed < 0:
            INVALID_TIMING_TOTAL.inc()
        else:
            CANCELLATION_SECONDS.labels(self.harness, "request_to_observation").observe(elapsed)

    def signal_sent(self) -> None:
        if self.signal_sent_at is None:
            self.signal_sent_at = time.monotonic()

    def exit_observed(self) -> None:
        if self.exit_observed_at is not None:
            return
        self.exit_observed_at = time.monotonic()
        for phase, started in (
            ("observation_to_exit", self.cancel_observed_at),
            ("signal_to_exit", self.signal_sent_at),
        ):
            if started is not None:
                CANCELLATION_SECONDS.labels(self.harness, phase).observe(
                    self.exit_observed_at - started,
                )


def resident_memory_bytes() -> int:
    """Current RSS, never peak RSS; no optional native dependency is needed."""
    if sys.platform.startswith("linux"):
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=True,
        timeout=0.5,
    )
    return int(result.stdout.strip()) * 1024


class RuntimeProcessCollector:
    """Sample the exporting process on scrape, including macOS Python workers."""

    def collect(self) -> Iterator[CounterMetricFamily | GaugeMetricFamily]:
        yield CounterMetricFamily(
            "local_agent_process_cpu_seconds",
            "CPU seconds used by this process.",
            value=time.process_time(),
        )
        available = GaugeMetricFamily(
            "local_agent_process_memory_sample_available",
            "One if current RSS was sampled.",
        )
        try:
            rss = resident_memory_bytes()
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            available.add_metric([], 0)
        else:
            available.add_metric([], 1)
            yield GaugeMetricFamily(
                "local_agent_process_resident_memory_bytes",
                "Current RSS of this process.",
                value=rss,
            )
        yield available

    def describe(self) -> list[CounterMetricFamily | GaugeMetricFamily]:
        return [
            CounterMetricFamily("local_agent_process_cpu_seconds", "CPU seconds."),
            GaugeMetricFamily("local_agent_process_resident_memory_bytes", "Current RSS."),
            GaugeMetricFamily("local_agent_process_memory_sample_available", "RSS availability."),
        ]


REGISTRY.register(RuntimeProcessCollector())


@contextmanager
def dispatcher_metrics_server(port: int, tier: str | None = None) -> Iterator[None]:
    """Expose the process that executes agents; Pi's registry cannot see its counters."""
    if port == 0:
        yield
        return
    offsets = {None: 0, "junior": 1, "senior": 2, "staff": 3}
    if tier not in offsets or not 1 <= port <= 65532:
        raise ValueError("invalid dispatcher metrics port or tier")
    try:
        server, thread = start_http_server(port + offsets[tier], addr="127.0.0.1")
    except OSError as exc:
        # An unavailable telemetry port must not change execution/recovery semantics.
        logger.warning("dispatcher metrics unavailable: %s", exc)
        yield
        return
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

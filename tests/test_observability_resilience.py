# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace.export import SpanExportResult

from local_first_agent_os import observability


class _RecordingExporter:
    def __init__(self) -> None:
        self.exports: list[Any] = []
        self.shutdown_called = False
        self.flush_timeout: int | None = None

    def export(self, spans: Any) -> SpanExportResult:
        self.exports.append(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_timeout = timeout_millis
        return True


def test_trace_export_drops_batch_when_collector_disappears(monkeypatch) -> None:
    delegate = _RecordingExporter()
    exporter = observability._ReachabilityGuardedSpanExporter(
        delegate,
        "http://127.0.0.1:4318/v1/traces",
    )
    monkeypatch.setattr(observability, "_endpoint_reachable", lambda *_args, **_kwargs: False)

    result = exporter.export(["span"])

    assert result is SpanExportResult.FAILURE
    assert delegate.exports == []


def test_trace_export_delegates_while_collector_is_live(monkeypatch) -> None:
    delegate = _RecordingExporter()
    exporter = observability._ReachabilityGuardedSpanExporter(
        delegate,
        "http://127.0.0.1:4318/v1/traces",
    )
    monkeypatch.setattr(observability, "_endpoint_reachable", lambda *_args, **_kwargs: True)

    assert exporter.export(["span"]) is SpanExportResult.SUCCESS
    assert delegate.exports == [["span"]]
    assert exporter.force_flush(250) is True
    assert delegate.flush_timeout == 250
    exporter.shutdown()
    assert delegate.shutdown_called is True

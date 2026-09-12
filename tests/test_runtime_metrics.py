# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio

import pytest
from host_test_scope import require_uncontained_scope
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest

from local_first_agent_os import runtime_metrics as metrics
from local_first_agent_os.coordination import store


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_commit_timing_excludes_the_rest_of_the_transaction(monkeypatch) -> None:
    class Connection:
        committed = False
        closed = False

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(store, "connect", lambda: connection)
    clock = iter([0.0, 1.0, 4.0, 7.0])
    monkeypatch.setattr(metrics.time, "monotonic", lambda: next(clock))
    labels = {"operation": "raw_event", "outcome": "committed"}
    commit_before = sample("local_agent_execution_event_commit_seconds_sum", **labels)
    total_before = sample("local_agent_execution_event_transaction_seconds_sum", **labels)
    with metrics.event_transaction(metrics.EventTransaction.RAW_EVENT), store.tx():
        pass
    assert sample("local_agent_execution_event_commit_seconds_sum", **labels) - commit_before == 3
    assert (
        sample("local_agent_execution_event_transaction_seconds_sum", **labels) - total_before == 7
    )
    assert connection.committed and connection.closed


def test_commit_failure_is_recorded_as_failure_and_preserves_rollback(monkeypatch) -> None:
    class Connection:
        rolled_back = False
        closed = False

        def commit(self):
            raise OSError("commit unavailable")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(store, "connect", lambda: connection)
    labels = {"operation": "raw_event", "outcome": "failed"}
    before = sample("local_agent_execution_event_commit_seconds_count", **labels)
    with (
        pytest.raises(OSError, match="commit unavailable"),
        metrics.event_transaction(metrics.EventTransaction.RAW_EVENT),
        store.tx(),
    ):
        pass
    assert sample("local_agent_execution_event_commit_seconds_count", **labels) == before + 1
    assert connection.rolled_back and connection.closed


def test_concurrent_stream_samples_keep_each_invocations_bytes() -> None:
    async def exercise():
        first, second = metrics.SupervisionMetrics("codex"), metrics.SupervisionMetrics("codex")
        left, right = asyncio.StreamReader(), asyncio.StreamReader()
        labels = {"harness": "codex", "source": "stdout"}
        baseline = sample("local_agent_execution_stream_buffer_bytes", **labels)
        left.feed_data(b"abc\n")
        right.feed_data(b"123456\n")
        first.sample_buffer("stdout", left)
        second.sample_buffer("stdout", right)
        assert sample("local_agent_execution_stream_buffer_bytes", **labels) == baseline + 11
        await left.readline()
        first.sample_buffer("stdout", left)
        first.close_buffers()
        assert sample("local_agent_execution_stream_buffer_bytes", **labels) == baseline + 7
        second.close_buffers()
        assert sample("local_agent_execution_stream_buffer_bytes", **labels) == baseline

    asyncio.run(exercise())


def test_pending_event_failure_releases_only_its_own_gauge() -> None:
    labels = {"harness": "other", "source": "stdout"}
    before = sample("local_agent_execution_events_pending", **labels)
    first = metrics.SupervisionMetrics("untrusted-model-name")
    with first.pending("stdout"):
        with pytest.raises(OSError), first.pending("stdout"):
            raise OSError("append unavailable")
        assert sample("local_agent_execution_events_pending", **labels) == before + 1
    assert sample("local_agent_execution_events_pending", **labels) == before


def test_exit_timing_is_observed_once_and_excludes_later_stream_drain(monkeypatch) -> None:
    clock = iter([1.0, 2.0, 5.0])
    monkeypatch.setattr(metrics.time, "monotonic", lambda: next(clock))
    labels = {"harness": "codex", "phase": "signal_to_exit"}
    before = sample("local_agent_execution_cancellation_seconds_sum", **labels)
    invocation = metrics.SupervisionMetrics("codex")
    invocation.cancel_observed(None)
    invocation.signal_sent()
    invocation.exit_observed()
    invocation.exit_observed()
    assert sample("local_agent_execution_cancellation_seconds_sum", **labels) - before == 3


def test_current_process_cpu_and_rss_can_be_scraped() -> None:
    require_uncontained_scope(
        reason="actual process RSS sampling requires the host process observer",
        required_flag="AIDASHOS_REQUIRE_HOST_PROCESS_OBSERVER",
    )
    registry = CollectorRegistry()
    registry.register(metrics.RuntimeProcessCollector())
    data = generate_latest(registry).decode()
    assert "local_agent_process_cpu_seconds_total" in data
    assert "local_agent_process_memory_sample_available 1.0" in data
    rss = registry.get_sample_value("local_agent_process_resident_memory_bytes")
    assert rss is not None and rss > 0


def test_unavailable_memory_is_not_reported_as_zero(monkeypatch) -> None:
    def unavailable():
        raise OSError("unavailable")

    monkeypatch.setattr(metrics, "resident_memory_bytes", unavailable)
    registry = CollectorRegistry()
    registry.register(metrics.RuntimeProcessCollector())
    assert registry.get_sample_value("local_agent_process_memory_sample_available") == 0
    assert registry.get_sample_value("local_agent_process_resident_memory_bytes") is None


def test_failed_exporter_binding_does_not_stop_execution(monkeypatch) -> None:
    def occupied(*args, **kwargs):
        raise OSError("port in use")

    monkeypatch.setattr(metrics, "start_http_server", occupied)
    with metrics.dispatcher_metrics_server(8767):
        executed = True
    assert executed


def test_real_subprocess_backlog_is_visible_while_event_commit_is_blocked(tmp_path) -> None:
    import sys
    import threading

    from test_agent_execution_supervisor import _Artifacts, _coord, _lease

    from local_first_agent_os.agent_execution_supervisor import StreamingCommandSupervisor
    from local_first_agent_os.coordination.contracts import AppendExecutionEvent

    entered, release = threading.Event(), threading.Event()

    def coordinate(command):
        if (
            isinstance(command, AppendExecutionEvent)
            and command.source == "stdout"
            and not entered.is_set()
        ):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release append")
        return _coord(command)

    lease = _lease(tmp_path)
    supervisor = StreamingCommandSupervisor(
        coordination_command=coordinate,
        artifact_writer=_Artifacts(),
        heartbeat_seconds=0.1,
    )
    labels = {"harness": "codex", "source": "stdout"}
    before = sample("local_agent_execution_stream_buffer_bytes", **labels)
    code = (
        "import json; "
        "[print(json.dumps({'type':'test','text':'x'*100}),flush=True) for _ in range(100)]"
    )

    async def exercise():
        running = asyncio.create_task(
            supervisor.run(
                [sys.executable, "-u", "-c", code],
                tmp_path,
                lease=lease,
                harness="codex",
                timeout_seconds=5,
            )
        )
        try:
            async with asyncio.timeout(3):
                while (
                    not entered.is_set()
                    or sample(
                        "local_agent_execution_stream_buffer_bytes",
                        **labels,
                    )
                    <= before
                ):
                    await asyncio.sleep(0.01)
            assert sample("local_agent_execution_events_pending", **labels) >= 1
        finally:
            release.set()
            result = await running
        assert result.capture.exit_code == 0

    asyncio.run(exercise())
    assert sample("local_agent_execution_stream_buffer_bytes", **labels) == before
    assert sample("local_agent_execution_events_pending", **labels) == 0

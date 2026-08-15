# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest

from local_first_agent_os.pi_runtime import PiRuntime


class _FakePiDaemonClient:
    events: list[dict[str, Any]] = []

    def stream_query(self, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        yield from self.events


def _runtime() -> PiRuntime:
    return PiRuntime(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )


async def _collect_stream(runtime: PiRuntime, prompt: str) -> list[str]:
    return [chunk async for chunk in runtime.stream(prompt)]


@pytest.fixture(autouse=True)
def _fake_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_first_agent_os.pi_daemon.ensure_pi_daemon", lambda: None)
    monkeypatch.setattr("local_first_agent_os.pi_daemon.PiDaemonClient", _FakePiDaemonClient)
    _FakePiDaemonClient.events = []


def test_pi_runtime_stream_suppresses_duplicate_final_render() -> None:
    _FakePiDaemonClient.events = [
        {"type": "delta", "text": "OK"},
        {"type": "result", "rendered": "OK"},
        {"type": "done"},
    ]

    chunks = asyncio.run(_collect_stream(_runtime(), "prompt"))

    assert chunks == ["OK"]


def test_pi_runtime_stream_yields_result_without_deltas() -> None:
    _FakePiDaemonClient.events = [
        {"type": "result", "rendered": "timer failed"},
        {"type": "done"},
    ]

    chunks = asyncio.run(_collect_stream(_runtime(), "/timer 0"))

    assert chunks == ["timer failed"]


def test_pi_runtime_stream_preserves_distinct_render_after_deltas() -> None:
    _FakePiDaemonClient.events = [
        {"type": "delta", "text": "partial"},
        {"type": "result", "rendered": "summary"},
        {"type": "done"},
    ]

    chunks = asyncio.run(_collect_stream(_runtime(), "prompt"))

    assert chunks == ["partial", "summary"]

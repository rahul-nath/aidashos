# SPDX-License-Identifier: AGPL-3.0-or-later
"""Installed, unauthenticated Codex client/worker lifetime conformance."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest

from local_first_agent_os.capabilities import Capability
from local_first_agent_os.codex_review_client import LocalFixtureModel, run_read_only_review
from local_first_agent_os.codex_tool_worker import CodexToolWorker
from local_first_agent_os.process_containment import ProcessContainmentUnavailable
from local_first_agent_os.sandbox_runtime import ReadOnlyToolWorker, SandboxRuntimeInstallation
from local_first_agent_os.spawn_authority import SpawnAuthority


def _process_parents() -> dict[int, int]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid="],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return {
        int(pid): int(parent)
        for pid, parent in (line.split() for line in result.stdout.splitlines())
    }


def _owned_process_tree(roots: set[int]) -> set[int]:
    parents = _process_parents()
    assert roots <= parents.keys(), "a native process exited before the fixture boundary"
    owned = set(roots)
    while descendants := {pid for pid, parent in parents.items() if parent in owned} - owned:
        owned |= descendants
    return owned


async def _assert_processes_gone(pids: set[int]) -> None:
    deadline = time.monotonic() + 3
    while remaining := pids & _process_parents().keys():
        if time.monotonic() >= deadline:
            pytest.fail(f"native review processes survived context cleanup: {sorted(remaining)}")
        await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    "ending",
    [
        "completed",
        "cancelled",
        "worker_failed",
        "worker_failed_at_report",
        "interpreter_failed",
        "interpreter_failed_at_report",
    ],
)
def test_native_review_lifetime_has_no_retained_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    source = os.environ.get("LOCAL_AGENT_SRT_PROBE_ROOT")
    node = os.environ.get("LOCAL_AGENT_SRT_PROBE_NODE")
    codex = shutil.which("codex")
    if platform.system() != "Darwin" or not source or not node or not codex:
        pytest.skip("explicit installed-Codex/SRT fixture not configured")
    installation = SandboxRuntimeInstallation.inspect(Path(source), Path(node))
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "canary.txt").write_text("lifetime read through the native worker\n")
    events: list[dict] = []
    launched: list[asyncio.subprocess.Process] = []
    owned: set[int] = set()
    original_spawn = asyncio.create_subprocess_exec

    async def tracked_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked_spawn)

    async def scenario() -> None:
        final_requested = asyncio.Event()
        release_final = asyncio.Event()
        provider_tasks: set[asyncio.Task] = set()
        request_count = 0

        async def provider(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal request_count
            task = asyncio.current_task()
            assert task is not None
            provider_tasks.add(task)
            try:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
                length = next(
                    int(line.split(":", 1)[1])
                    for line in headers
                    if line.lower().startswith("content-length:")
                )
                await reader.readexactly(length)
                request_count += 1
                ordinal = request_count
                if ordinal == 1:
                    item = {
                        "type": "custom_tool_call",
                        "id": "read-item",
                        "call_id": "read-call",
                        "name": "exec",
                        "input": "text(await tools.read_repository("
                        '{operation:"read_file",path:"canary.txt"}));',
                    }
                else:
                    final_requested.set()
                    await release_final.wait()
                    item = {
                        "type": "message",
                        "id": "final-item",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "APPROVE\nLifetime fixture.",
                                "annotations": [],
                            }
                        ],
                    }
                response = {
                    "id": f"response-{ordinal}",
                    "object": "response",
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                }
                stream = (
                    {
                        "type": "response.created",
                        "response": dict(response, status="in_progress", output=[]),
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                )
                data = "".join("data: " + json.dumps(event) + "\n\n" for event in stream).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                    + str(len(data)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + data
                )
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await writer.wait_closed()
                provider_tasks.discard(task)

        boundary = ReadOnlyToolWorker(installation, repository)
        authority = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL))
        try:
            async with (
                await asyncio.start_server(provider, "127.0.0.1", 0) as server,
                CodexToolWorker(boundary, codex, authority) as worker,
            ):
                port = server.sockets[0].getsockname()[1]

                def record_event(event: dict) -> None:
                    events.append(event)
                    if ending == "interpreter_failed_at_report" and event.get("item", {}).get(
                        "text", ""
                    ).startswith("APPROVE"):
                        launched[1].kill()
                    if (
                        ending == "worker_failed_at_report"
                        and event.get("item", {}).get("type") == "agent_message"
                    ):
                        assert worker._process is not None
                        worker._process.kill()

                review = asyncio.create_task(
                    run_read_only_review(
                        worker=worker,
                        codex_bin=codex,
                        repository=repository,
                        model=LocalFixtureModel("gpt-5.4", f"http://127.0.0.1:{port}/v1"),
                        prompt="Read canary.txt with read_repository, then review it.",
                        emit=lambda event: record_event(dict(event)),
                    )
                )
                try:
                    await asyncio.wait_for(final_requested.wait(), 20)
                    assert len(launched) == 3, (
                        "expected the model client, native worker, and contained Code Mode host"
                    )
                    owned.update(_owned_process_tree({process.pid for process in launched}))
                    assert len(owned) >= 5, (
                        "both native services must be observed beneath their SRT hosts"
                    )
                    if ending == "completed":
                        release_final.set()
                        report = await asyncio.wait_for(review, 15)
                        assert report.startswith("APPROVE")
                    elif ending == "cancelled":
                        review.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(review, 15)
                    elif ending in {"worker_failed_at_report", "interpreter_failed_at_report"}:
                        release_final.set()
                        with pytest.raises(ProcessContainmentUnavailable):
                            await asyncio.wait_for(review, 15)
                        assert any(
                            event.get("item", {}).get("text", "").startswith("APPROVE")
                            for event in events
                        ), "the failure must occur after the report, before successful completion"
                    else:
                        assert worker._process is not None
                        if ending == "interpreter_failed":
                            launched[1].kill()
                        else:
                            worker._process.kill()
                        with pytest.raises(ProcessContainmentUnavailable):
                            await asyncio.wait_for(review, 15)
                    assert sum(event.get("type") == "turn.completed" for event in events) == (
                        1 if ending == "completed" else 0
                    )
                finally:
                    release_final.set()
                    if not review.done():
                        review.cancel()
                    await asyncio.gather(review, return_exceptions=True)
            assert all(process.returncode is not None for process in launched)
            await _assert_processes_gone(owned)
        finally:
            release_final.set()
            for task in tuple(provider_tasks):
                task.cancel()
            await asyncio.gather(*provider_tasks, return_exceptions=True)

    try:
        asyncio.run(asyncio.wait_for(scenario(), 45))
    finally:
        # Failed conformance must not leave its own fixture processes resident.
        for pid in owned & _process_parents().keys():
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

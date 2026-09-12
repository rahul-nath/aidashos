# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from host_test_scope import require_uncontained_scope
from websockets.asyncio.client import connect
from websockets.asyncio.server import Server
from websockets.exceptions import ConnectionClosed

from local_first_agent_os.capabilities import Capability
from local_first_agent_os.codex_tool_worker import (
    CodexToolWorker,
    InspectionRpcPolicy,
    WorkerOperationDenied,
)
from local_first_agent_os.process_containment import ContainedProcess, ProcessContainmentUnavailable
from local_first_agent_os.sandbox_runtime import ReadOnlyToolWorker, SandboxRuntimeInstallation
from local_first_agent_os.spawn_authority import SpawnAuthority

_AUTHORITY = SpawnAuthority.of((Capability.READ_REPOSITORY,))


def test_close_survives_repeated_caller_cancellation(tmp_path):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        finalized = []

        class HeldServer:
            def close(self):
                pass

            async def wait_closed(self):
                entered.set()
                await release.wait()

        class Stdin:
            closed = False

            def close(self):
                self.closed = True

        class Process:
            stdin = Stdin()
            returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        worker = CodexToolWorker(
            cast(ReadOnlyToolWorker, SimpleNamespace(repository=tmp_path)), "/bin/false", _AUTHORITY
        )
        worker._server = cast(Server, HeldServer())
        process = Process()
        worker._process = cast(asyncio.subprocess.Process, process)
        worker._stack.callback(lambda: finalized.append(True))
        closing = asyncio.create_task(worker.aclose())
        await entered.wait()
        try:
            for _ in range(3):
                closing.cancel()
                await asyncio.sleep(0)
            assert not closing.done()
            assert not worker._closed
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
        assert closing.cancelled()
        assert process.stdin.closed
        assert process.returncode == 0
        assert worker._closed
        assert finalized == [True]
        await worker.aclose()
        assert finalized == [True]

    asyncio.run(exercise())


def test_close_finalizes_other_resources_after_relay_timeout(tmp_path, monkeypatch):
    async def exercise():
        finalized = []

        class HeldServer:
            def close(self):
                pass

            async def wait_closed(self):
                await asyncio.Future()

        class Process:
            stdin = SimpleNamespace(close=lambda: finalized.append("stdin"))
            returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        worker = CodexToolWorker(
            cast(ReadOnlyToolWorker, SimpleNamespace(repository=tmp_path)), "/bin/false", _AUTHORITY
        )
        worker._server = cast(Server, HeldServer())
        process = Process()
        worker._process = cast(asyncio.subprocess.Process, process)
        worker._stack.callback(lambda: finalized.append("stack"))
        with pytest.raises(ProcessContainmentUnavailable, match="cleanup failed"):
            await asyncio.wait_for(worker.aclose(), 1)
        assert process.returncode == 0
        assert not worker._closed
        assert finalized == ["stdin", "stack"]
        with pytest.raises(ProcessContainmentUnavailable, match="cleanup failed"):
            await worker.aclose()
        assert finalized == ["stdin", "stack"]

    monkeypatch.setattr("local_first_agent_os.codex_tool_worker._RESOURCE_CLOSE_TIMEOUT", 0.01)
    asyncio.run(exercise())


def test_policy_is_closed_and_rewrites_only_authorized_sandbox(tmp_path):
    policy = InspectionRpcPolicy(tmp_path, _AUTHORITY)
    request = {"path": str(tmp_path / "file.py"), "sandbox": {"untrusted": "nested"}}
    assert policy.authorize("fs/readFile", request) == {
        "path": (tmp_path / "file.py").as_uri(),
        "sandbox": None,
    }
    assert request["sandbox"] == {"untrusted": "nested"}
    for method in ("process/start", "process/spawn", "fs/writeFile", "fs/remove", "unknown"):
        with pytest.raises(WorkerOperationDenied, match="does not grant"):
            policy.authorize(method, request)
    with pytest.raises(WorkerOperationDenied, match="not granted"):
        InspectionRpcPolicy(tmp_path, SpawnAuthority.nothing()).authorize("fs/readFile", request)


@pytest.mark.parametrize(
    "raw",
    [
        "../foreign",
        "https://example.com/file",
        "file://remote/a",
        "file:///etc/passwd",
        "\x00",
        "",
        None,
    ],
)
def test_policy_rejects_foreign_and_invalid_paths(tmp_path, raw):
    with pytest.raises(WorkerOperationDenied):
        InspectionRpcPolicy(tmp_path, _AUTHORITY).repository_path(raw)


def test_policy_resolves_symlink_before_forwarding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link").symlink_to(tmp_path / "foreign")
    with pytest.raises(WorkerOperationDenied, match="outside"):
        InspectionRpcPolicy(repo, _AUTHORITY).authorize("fs/readFile", {"path": "link"})


@pytest.mark.parametrize("replacement", ["foreign_symlink", "new_directory"])
def test_policy_rejects_retargeted_repository_root(tmp_path, replacement):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = InspectionRpcPolicy(repo, _AUTHORITY)
    repo.rename(tmp_path / "original")
    if replacement == "foreign_symlink":
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        repo.symlink_to(foreign, target_is_directory=True)
    else:
        repo.mkdir()
    with pytest.raises(WorkerOperationDenied, match="repository.*changed"):
        policy.authorize("fs/readFile", {"path": "secret"})


@pytest.mark.parametrize(
    "params", [{"path": ".", "shell": "sh"}, {"path": ".", "env": {}}, [], None]
)
def test_policy_rejects_unknown_fields(tmp_path, params):
    with pytest.raises(WorkerOperationDenied):
        InspectionRpcPolicy(tmp_path, _AUTHORITY).authorize("fs/readFile", params)


_FAKE_WORKER = """
import base64, json, os, pathlib, sys
for raw in sys.stdin:
    request = json.loads(raw)
    method, params = request['method'], request.get('params')
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'sessionId': 'fixture-session'}
    elif method == 'environment/info':
        result = {'shell': {'name': 'sh', 'path': '/bin/sh'}, 'cwd': pathlib.Path.cwd().as_uri()}
    elif method == 'environment/status':
        result = {'status': 'ready'}
    else:
        allowed = ('fs/readFile', 'fs/getMetadata', 'fs/readDirectory', 'fs/canonicalize')
        assert method in allowed, method
        assert params['sandbox'] is None
        path = pathlib.Path(params['path'].removeprefix('file://'))
        if path.name == 'terminate-worker':
            os._exit(0)
        if method == 'fs/getMetadata':
            result = {'isFile': path.is_file(), 'isDirectory': path.is_dir(),
                      'size': path.stat().st_size}
        elif method == 'fs/readFile':
            result = {'dataBase64': base64.b64encode(path.read_bytes()).decode()}
        elif method == 'fs/readDirectory':
            result = {'entries': [{'fileName': p.name, 'isFile': p.is_file(),
                                   'isDirectory': p.is_dir()} for p in path.iterdir()]}
        else:
            result = {'path': path.resolve().as_uri()}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
"""


class _FakeBoundary:
    def __init__(self, repository):
        self.repository = repository
        self.script = repository / "fake_worker.py"
        self.script.write_text(_FAKE_WORKER)

    @contextmanager
    def contain_service(self, command):
        assert "exec-server" in command
        yield ContainedProcess(
            command=(sys.executable, str(self.script)),
            environment={},
            scratch_path=self.repository,
            posture="fixture",
        )


def _fixture_worker(repository: Path) -> CodexToolWorker:
    """The native worker is a double; the real host TCP relay remains under test."""
    require_uncontained_scope(
        reason="Codex host relay tests require an uncontained loopback listener",
        required_flag="LOCAL_AGENT_REQUIRE_CODEX_HOST_TESTS",
    )
    return CodexToolWorker(
        cast(ReadOnlyToolWorker, _FakeBoundary(repository)), "/bin/false", _AUTHORITY
    )


def test_worker_native_read_relay_denials_and_lifetime(tmp_path):
    (tmp_path / "sample.txt").write_text("first\nsecond\nthird\n")

    async def exercise():
        async with _fixture_worker(tmp_path) as worker:
            await worker.require_ready()
            result = await worker.read_repository(
                {"operation": "read_file", "path": "sample.txt", "start_line": 2, "max_lines": 1}
            )
            assert result["text"] == "second"
            assert result["truncated"]
            listed = await worker.read_repository({"operation": "list_directory", "path": "."})
            assert "sample.txt" in [entry["fileName"] for entry in listed["entries"]]
            async with connect(worker.url) as socket:

                async def rpc(i, method, params):
                    await socket.send(json.dumps({"id": i, "method": method, "params": params}))
                    return json.loads(await socket.recv())

                assert (
                    await rpc(1, "initialize", {"clientName": "fixture", "resumeSessionId": None})
                )["result"] == {"sessionId": "fixture-session"}
                denied = await rpc(2, "process/start", {"argv": ["/bin/echo", "must-not-run"]})
                assert denied["error"]["code"] == -32003
                assert (await rpc(3, "fs/writeFile", {"path": (tmp_path / "sample.txt").as_uri()}))[
                    "error"
                ]["code"] == -32003
                read = await rpc(
                    4,
                    "fs/readFile",
                    {"path": (tmp_path / "sample.txt").as_uri(), "sandbox": {"nested": True}},
                )
                assert read["result"]["dataBase64"] == "Zmlyc3QKc2Vjb25kCnRoaXJkCg=="
                assert (await rpc(5, "fs/readFile", {"path": "/etc/passwd"}))["error"][
                    "code"
                ] == -32003
                assert worker.is_alive
            await asyncio.wait_for(worker.disconnected.wait(), 1)
            with pytest.raises(ProcessContainmentUnavailable):
                worker.assert_alive()
        assert worker._process is not None
        assert worker._process.returncode == 0
        assert (tmp_path / "sample.txt").read_text() == "first\nsecond\nthird\n"

    asyncio.run(exercise())


@pytest.mark.parametrize("port", ["relay", "dynamic"])
@pytest.mark.parametrize(
    "metadata", [{"isFile": False, "size": 0}, {"isFile": True, "size": 1024 * 1024 + 1}]
)
def test_both_read_ports_reject_unbounded_files_before_native_read(
    tmp_path, monkeypatch, port, metadata
):
    async def exercise():
        async with _fixture_worker(tmp_path) as worker:
            reads = []
            native_request = worker._request

            async def tracked_request(method, params):
                if method == "fs/getMetadata":
                    return metadata
                if method == "fs/readFile":
                    reads.append(params)
                    return {"dataBase64": "eA=="}
                return await native_request(method, params)

            monkeypatch.setattr(worker, "_request", tracked_request)
            if port == "dynamic":
                with pytest.raises(WorkerOperationDenied, match="bounded regular file"):
                    await worker.read_repository({"operation": "read_file", "path": "fixture"})
            else:
                async with connect(worker.url) as socket:
                    await socket.send(json.dumps({"id": 1, "method": "initialize", "params": {}}))
                    assert "result" in json.loads(await socket.recv())
                    await socket.send(
                        json.dumps(
                            {"id": 2, "method": "fs/readFile", "params": {"path": "fixture"}}
                        )
                    )
                    response = json.loads(await socket.recv())
                    assert response.get("error", {}).get("code") == -32003
                    assert "bounded regular file" in response["error"]["message"]
            assert reads == []

    asyncio.run(exercise())


def test_unrecognized_relay_connection_does_not_disrupt_worker(tmp_path):
    async def exercise():
        async with _fixture_worker(tmp_path) as worker:
            async with connect(worker.url.rsplit("/", 1)[0] + "/wrong") as socket:
                with pytest.raises(ConnectionClosed):
                    await socket.recv()
            await worker.require_ready()

    asyncio.run(exercise())


def test_worker_eof_fails_outstanding_read(tmp_path):
    async def exercise():
        async with _fixture_worker(tmp_path) as worker:
            with pytest.raises(ProcessContainmentUnavailable):
                await worker.read_repository({"operation": "read_file", "path": "terminate-worker"})
            assert worker.disconnected.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "write_file", "path": "sample.txt"},
        {"operation": "read_file", "path": "sample.txt", "start_line": 0},
        {"operation": "read_file", "path": "sample.txt", "start_line": True},
        {"operation": "read_file", "path": "sample.txt", "max_lines": 1001},
        {"operation": "read_file", "path": "sample.txt", "command": "cat"},
        {"operation": "list_directory", "path": ".", "max_lines": 1},
    ],
)
def test_read_port_rejects_invalid_states(tmp_path, arguments):
    (tmp_path / "sample.txt").write_text("sample")

    async def exercise():
        async with _fixture_worker(tmp_path) as worker:
            with pytest.raises(WorkerOperationDenied):
                await worker.read_repository(arguments)
            await worker.require_ready()

    asyncio.run(exercise())


def test_non_utf8_file_is_a_tool_denial(tmp_path):
    (tmp_path / "binary").write_bytes(b"\xff")

    async def exercise():
        async with _fixture_worker(tmp_path) as worker:
            with pytest.raises(WorkerOperationDenied, match="UTF-8"):
                await worker.read_repository({"operation": "read_file", "path": "binary"})
            await worker.require_ready()

    asyncio.run(exercise())


def test_real_codex_worker_read_port(tmp_path):
    require_uncontained_scope(
        reason="Native Codex/SRT host integration cannot run inside a verification child",
        required_flag="LOCAL_AGENT_REQUIRE_CODEX_HOST_TESTS",
    )
    root = os.environ.get("LOCAL_AGENT_SRT_PROBE_ROOT")
    node = os.environ.get("LOCAL_AGENT_SRT_PROBE_NODE")
    if platform.system() != "Darwin" or not root or not node:
        pytest.skip("explicit macOS SRT native worker fixture not configured")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "canary.txt").write_text("native worker proof\n")
    boundary = ReadOnlyToolWorker(SandboxRuntimeInstallation.inspect(Path(root), Path(node)), repo)

    async def exercise():
        async with CodexToolWorker(boundary, "/opt/homebrew/bin/codex", _AUTHORITY) as worker:
            assert worker._process is not None
            assert os.getpgid(worker._process.pid) == os.getpgrp()
            read = await worker.read_repository({"operation": "read_file", "path": "canary.txt"})
            assert read["text"] == "native worker proof"
            async with connect(worker.url) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "initialize",
                            "params": {"clientName": "real-fixture", "resumeSessionId": None},
                        }
                    )
                )
                assert "result" in json.loads(await socket.recv())
                await socket.send(
                    json.dumps(
                        {
                            "id": 2,
                            "method": "process/start",
                            "params": {"argv": ["/bin/echo", "forbidden"]},
                        }
                    )
                )
                assert json.loads(await socket.recv())["error"]["code"] == -32003
                await worker.require_ready()
        assert worker._process is not None
        assert worker._process.returncode == 0

    asyncio.run(exercise())

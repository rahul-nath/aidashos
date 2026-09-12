# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable native acceptance, using installed services and synthetic secrets only.

The diagnostic command port is owned by this test, not exposed to reviewers.
It independently tests OS denial beneath the production read-only RPC policy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from test_codex_review_lifetime import _owned_process_tree, _process_parents
from test_codex_srt_compatibility import _reply
from websockets.asyncio.client import connect

from local_first_agent_os import codex_review_client
from local_first_agent_os.capabilities import Capability
from local_first_agent_os.codex_review_client import (
    LocalFixtureModel,
    _client_command,
    run_read_only_review,
)
from local_first_agent_os.codex_tool_worker import CodexToolWorker
from local_first_agent_os.process_containment import ProcessContainmentUnavailable
from local_first_agent_os.sandbox_runtime import ReadOnlyToolWorker, SandboxRuntimeInstallation
from local_first_agent_os.spawn_authority import ReadOnlyInspection, SpawnAuthority

pytestmark = pytest.mark.native_codex_srt
scenarios("features/codex_inspection_boundary.feature")

_READ_TEXT = "native-inspection-repository-canary\n"
_SECRETS = {
    "LOCAL_AGENT_OPERATOR_TOKEN": "synthetic-operator-secret-95220",
    "OPENAI_API_KEY": "synthetic-provider-secret-57203",
    "ANTHROPIC_API_KEY": "synthetic-anthropic-secret-23852",
    "LOCAL_AGENT_COORDINATION_DATABASE_URL": "synthetic-writer-secret-43873",
}
_AUTHORITY = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL))


@pytest.fixture(scope="module")
def installed_native_boundary():
    assert platform.system() == "Darwin", "native acceptance requires macOS"
    source = os.environ.get("LOCAL_AGENT_SRT_PROBE_ROOT")
    node = os.environ.get("LOCAL_AGENT_SRT_PROBE_NODE")
    codex = shutil.which("codex")
    assert source and node and codex, "native profile must supply its installed prerequisites"
    installation = SandboxRuntimeInstallation.inspect(Path(source), Path(node))
    result = subprocess.run(
        [codex, "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        env={"HOME": "/var/empty", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert result.stdout.strip() == "codex-cli 0.153.4"
    return installation, codex


@given("the installed native Codex and pinned SRT profile", target_fixture="boundary_world")
def native_boundary(installed_native_boundary, tmp_path):
    installation, codex = installed_native_boundary
    return {"installation": installation, "codex": codex, "root": tmp_path}


@given("an assigned repository and synthetic host credentials")
def assigned_repository(boundary_world, monkeypatch):
    world = boundary_world
    repo = world["root"] / "repository"
    repo.mkdir()
    (repo / "canary.txt").write_text(_READ_TEXT)
    credentials = world["root"] / "host-credentials.json"
    credentials.write_text(json.dumps(_SECRETS))
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    world.update(
        repo=repo, credentials=credentials, boundary=ReadOnlyToolWorker(world["installation"], repo)
    )


@when("the native inspection worker reads the repository and attempts forbidden effects")
def native_rpc_controls(boundary_world):
    world = boundary_world

    async def exercise():
        async with CodexToolWorker(world["boundary"], world["codex"], _AUTHORITY) as worker:
            await worker.require_ready()
            world["read"] = await worker.read_repository(
                {"operation": "read_file", "path": "canary.txt"}
            )
            async with connect(worker.url) as relay:

                async def rpc(number, method, params):
                    await relay.send(json.dumps({"id": number, "method": method, "params": params}))
                    return json.loads(await relay.recv())

                assert "result" in await rpc(1, "initialize", {})
                world["write"] = await rpc(
                    2,
                    "fs/writeFile",
                    {
                        "path": (world["repo"] / "canary.txt").as_uri(),
                        "dataBase64": base64.b64encode(b"mutated").decode(),
                    },
                )
                world["process"] = await rpc(
                    3,
                    "process/start",
                    {
                        "argv": ["/usr/bin/touch", str(world["repo"] / "command-marker")],
                    },
                )
                world["foreign"] = await rpc(
                    4,
                    "fs/readFile",
                    {
                        "path": world["credentials"].as_uri(),
                        "sandbox": None,
                    },
                )

    asyncio.run(asyncio.wait_for(exercise(), 25))


@then("the assigned repository text is returned")
def repo_read_succeeded(boundary_world):
    assert boundary_world["read"]["text"] == _READ_TEXT.rstrip("\n")


@then("repository writes and process execution are denied without changing files")
def reviewer_effects_denied(boundary_world):
    for operation in ("write", "process"):
        assert boundary_world[operation]["error"]["code"] == -32003
    assert (boundary_world["repo"] / "canary.txt").read_text() == _READ_TEXT
    assert not (boundary_world["repo"] / "command-marker").exists()


@then("a forbidden host file is not returned")
def foreign_read_denied(boundary_world):
    response = boundary_world["foreign"]
    assert response["error"]["code"] == -32003
    assert all(value not in json.dumps(response) for value in _SECRETS.values())


@when("trusted diagnostic probes run inside the production SRT profile")
def os_controls(boundary_world):
    world = boundary_world
    command = [world["codex"], "--strict-config", "-c", "analytics.enabled=false", "app-server"]
    diagnostic_path = world["root"] / "diagnostics.log"
    with (
        world["boundary"].contain_service(command) as prepared,
        diagnostic_path.open("w+") as stderr,
    ):
        process = subprocess.Popen(
            prepared.command,
            env=prepared.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        world["prepared_environment"] = dict(prepared.environment)
        assert process.stdin is not None
        stdin = process.stdin

        def send(value):
            stdin.write(json.dumps(value) + "\n")
            stdin.flush()

        def execute(number, argv):
            send(
                {
                    "id": number,
                    "method": "command/exec",
                    "params": {
                        "command": argv,
                        "cwd": str(world["repo"]),
                        "timeoutMs": 5000,
                        "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
                    },
                }
            )
            return _reply(process, number)

        try:
            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "native_boundary_acceptance", "version": "1"},
                    },
                }
            )
            _reply(process, 1)
            send({"method": "initialized", "params": {}})
            world["worker_env"] = execute(2, ["/usr/bin/env"])
            world["disk_secret"] = execute(3, ["/bin/cat", str(world["credentials"])])
            world["os_write"] = execute(4, ["/bin/sh", "-c", "printf mutated > canary.txt"])
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    accepted, _ = listener.accept()
                    accepted.close()
                world["tcp"] = execute(
                    5,
                    [
                        "/usr/bin/curl",
                        "--noproxy",
                        "*",
                        "--connect-timeout",
                        "1",
                        "--max-time",
                        "2",
                        f"http://127.0.0.1:{port}",
                    ],
                )
                listener.settimeout(0.1)
                with pytest.raises(TimeoutError):
                    listener.accept()
                world["tcp_connections"] = 0
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            stderr.flush()
            stderr.seek(0)
            world["diagnostics"] = stderr.read()


@then("a working localhost TCP canary receives no worker connection")
def network_denied(boundary_world):
    assert boundary_world["tcp"]["exitCode"] == 7
    assert boundary_world["tcp_connections"] == 0


@then("synthetic credentials are absent from worker environment and unreadable on disk")
def credentials_denied(boundary_world):
    world = boundary_world
    assert world["worker_env"]["exitCode"] == 0
    for name, value in _SECRETS.items():
        assert name not in world["prepared_environment"]
        assert name not in world["worker_env"]["stdout"]
        assert value not in world["worker_env"]["stdout"]
    assert world["disk_secret"]["exitCode"] != 0
    assert "permitted" in world["disk_secret"]["stderr"].lower()


@then("synthetic credential values do not appear in captured diagnostics")
def diagnostics_are_clean(boundary_world):
    captured = json.dumps(
        {
            key: boundary_world[key]
            for key in ("diagnostics", "worker_env", "disk_secret", "tcp", "os_write")
        }
    )
    assert all(value not in captured for value in _SECRETS.values())


@then("the operating-system repository write is denied and leaves its bytes unchanged")
def os_write_denied(boundary_world):
    assert boundary_world["os_write"]["exitCode"] != 0
    assert "permitted" in boundary_world["os_write"]["stderr"].lower()
    assert (boundary_world["repo"] / "canary.txt").read_text() == _READ_TEXT


@when("the production launch profile is prepared and inspected")
def inspect_profile(boundary_world):
    world = boundary_world
    argv = [world["codex"], "exec-server", "--listen", "stdio"]
    with world["boundary"].contain_service(argv) as prepared:
        request = json.loads(Path(prepared.command[-1]).read_text())
        world["profile"] = request["config"]
        world["scratch"] = str(prepared.scratch_path)
        world["service_argv"] = request["argv"]
        world["identity"] = prepared.identity
        assert prepared.posture == "read_only_tool_worker"
        assert (
            hashlib.sha256(Path(prepared.command[-1]).read_bytes()).hexdigest()
            == prepared.identity.request_sha256
        )
    world["client_argv"] = _client_command(
        world["codex"],
        LocalFixtureModel("gpt-5.4", "http://127.0.0.1:1/v1"),
    )


@then("its effective authority is exactly read-only repository inspection")
def exact_readonly_profile(boundary_world):
    world = boundary_world
    assert _AUTHORITY.capabilities == {Capability.READ_REPOSITORY, Capability.INVOKE_MODEL}
    assert isinstance(_AUTHORITY.posture(), ReadOnlyInspection)
    filesystem = world["profile"]["filesystem"]
    assert filesystem["denyRead"] == ["/"]
    assert filesystem["allowWrite"] == [world["scratch"]]
    assert filesystem["denyWrite"] == [
        str(world["repo"].resolve()),
        "/tmp/claude",
        "/private/tmp/claude",
        "/dev/tty",
        "/dev/dtracehelper",
        "/dev/autofs_nowait",
    ]
    assert set(filesystem["allowRead"]) == {
        "/System",
        "/usr",
        "/bin",
        "/sbin",
        "/etc",
        "/private/etc",
        "/dev/null",
        "/private/var/db/dyld",
        "/private/var/select/sh",
        str(world["repo"].resolve()),
        world["scratch"],
        world["codex"],
        str(Path(world["codex"]).resolve()),
    }
    assert (
        hashlib.sha256(
            json.dumps(world["profile"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == world["identity"].policy_sha256
    )


@then("no permission bypass or local-network exception is enabled")
def no_broader_grant(boundary_world):
    world = boundary_world
    assert world["profile"]["network"] == {
        "allowedDomains": [],
        "deniedDomains": ["*"],
        "strictAllowlist": True,
        "allowLocalBinding": False,
        "allowAllUnixSockets": False,
    }
    rendered = " ".join([*world["service_argv"], *world["client_argv"]])
    assert "--dangerously" not in rendered
    assert "--yolo" not in rendered
    assert "--strict-config" in world["client_argv"]
    assert 'sandbox_mode="read-only"' in world["client_argv"]
    assert 'approval_policy="never"' in world["client_argv"]
    assert 'web_search="disabled"' in world["client_argv"]
    disabled = {
        world["client_argv"][index + 1]
        for index, value in enumerate(world["client_argv"])
        if value == "--disable"
    }
    assert disabled >= {
        "shell_tool",
        "unified_exec",
        "hooks",
        "plugins",
        "apps",
        "multi_agent",
        "skill_mcp_dependency_install",
    }
    assert "--code-mode-host" not in world["client_argv"]


@when(parsers.parse("a real model-free review ends by {ending}"))
def review_lifetime(boundary_world, monkeypatch, ending):
    world = boundary_world
    launched, owned, events = [], set(), []
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    original_close = codex_review_client._close_client

    async def fail_client_at_reaping(client):
        if ending == "client failure after report":
            assert any(
                event.get("item", {}).get("text", "").startswith("APPROVE") for event in events
            )
            assert client.process.returncode is None
            client.process.kill()
        await original_close(client)

    # Fault timing is controlled at the cleanup boundary; the actual installed
    # process is killed and the production cleanup implementation still runs.
    monkeypatch.setattr(codex_review_client, "_close_client", fail_client_at_reaping)

    async def exercise():
        final_requested, release_final = asyncio.Event(), asyncio.Event()
        provider_tasks = set()
        requests = []

        async def provider(reader, writer):
            task = asyncio.current_task()
            provider_tasks.add(task)
            try:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
                length = next(
                    int(line.split(":", 1)[1])
                    for line in headers
                    if line.lower().startswith("content-length:")
                )
                requests.append(json.loads(await reader.readexactly(length)))
                ordinal = len(requests)
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
                                "text": "APPROVE\nNative BDD fixture.",
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

        try:
            async with (
                await asyncio.start_server(provider, "127.0.0.1", 0) as server,
                CodexToolWorker(world["boundary"], world["codex"], _AUTHORITY) as worker,
            ):
                port = server.sockets[0].getsockname()[1]

                def record_event(event):
                    events.append(event)
                    if ending == "worker failure after report" and event.get("item", {}).get(
                        "text", ""
                    ).startswith("APPROVE"):
                        assert worker._process is not None
                        worker._process.kill()

                review = asyncio.create_task(
                    run_read_only_review(
                        worker=worker,
                        codex_bin=world["codex"],
                        repository=world["repo"],
                        model=LocalFixtureModel("gpt-5.4", f"http://127.0.0.1:{port}/v1"),
                        prompt="Read canary.txt using read_repository, then review it.",
                        emit=record_event,
                    )
                )
                try:
                    await asyncio.wait_for(final_requested.wait(), 20)
                    assert len(launched) == 3
                    owned.update(_owned_process_tree({process.pid for process in launched}))
                    assert len(owned) >= 5
                    assert _READ_TEXT.rstrip() in json.dumps(requests[1])
                    if ending == "success":
                        release_final.set()
                        assert (await asyncio.wait_for(review, 15)).startswith("APPROVE")
                    elif ending == "worker failure":
                        assert worker._process is not None
                        worker._process.kill()
                        with pytest.raises(ProcessContainmentUnavailable):
                            await asyncio.wait_for(review, 15)
                    elif ending == "timeout":
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(review, 0.05)
                    elif ending == "cancellation":
                        review.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(review, 15)
                    elif ending in {"worker failure after report", "client failure after report"}:
                        release_final.set()
                        with pytest.raises(ProcessContainmentUnavailable):
                            await asyncio.wait_for(review, 15)
                        assert any(
                            event.get("item", {}).get("text", "").startswith("APPROVE")
                            for event in events
                        )
                    else:
                        raise AssertionError(f"unsupported lifetime ending: {ending}")
                finally:
                    release_final.set()
                    if not review.done():
                        review.cancel()
                    await asyncio.gather(review, return_exceptions=True)
            deadline = time.monotonic() + 3
            while owned & _process_parents().keys() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            world["remaining"] = owned & _process_parents().keys()
            world["observed"] = owned
            world["returncodes"] = [process.returncode for process in launched]
            world["events"] = events
            world["ending"] = ending
        finally:
            release_final.set()
            for task in tuple(provider_tasks):
                task.cancel()
            await asyncio.gather(*provider_tasks, return_exceptions=True)

    try:
        asyncio.run(asyncio.wait_for(exercise(), 45))
    finally:
        # Fixture cleanup does not substitute for the pre-cleanup observation.
        for pid in owned & _process_parents().keys():
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@then("all observed model-client and tool-worker descendants have exited")
def descendants_exited(boundary_world):
    assert len(boundary_world["observed"]) >= 3
    assert not boundary_world["remaining"], boundary_world["remaining"]
    assert all(code is not None for code in boundary_world["returncodes"])


@then("only successful completion emits a completed review")
def completion_is_truthful(boundary_world):
    completed = sum(
        event.get("type") == "codex.app_server.turn.completed" for event in boundary_world["events"]
    )
    assert completed == (1 if boundary_world["ending"] == "success" else 0)

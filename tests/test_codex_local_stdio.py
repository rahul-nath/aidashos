# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local transport ownership controls; native containment stays in the native lane."""

from __future__ import annotations

import asyncio
import os
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from host_test_scope import require_uncontained_scope

from local_first_agent_os.codex_code_mode import CodexCodeModeHost
from local_first_agent_os.codex_local_stdio import prepare_local_stdio_client
from local_first_agent_os.codex_stdio_relay import (
    CLIENT_NAME,
    FRAME_LIMIT,
    MANIFEST_NAME,
    RELAY_NAME,
    SHIM_NAME,
    CodeModeEndpoint,
    connect_endpoint,
    read_frame,
)
from local_first_agent_os.process_containment import ProcessContainmentUnavailable
from local_first_agent_os.sandbox_runtime import ReadOnlyToolWorker


@pytest.fixture
def host_stdio_scope() -> None:
    require_uncontained_scope(
        reason="Local Code Mode relay controls require the real host Unix listener",
        required_flag="LOCAL_AGENT_REQUIRE_CODEX_HOST_TESTS",
    )


@contextmanager
def _socket_directory(**_host_options):
    """Keep transport fixtures inside TMPDIR and below macOS's Unix path limit."""
    with tempfile.TemporaryDirectory(prefix="ct-", dir=tempfile.gettempdir()) as raw:
        directory = Path(raw).resolve()
        assert len(os.fsencode(directory / "ipc")) < 104
        yield directory


def _package(tmp_path: Path, endpoint: CodeModeEndpoint):
    source = tmp_path / "source-codex"
    source.write_text("fixture executable bytes\n")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return prepare_local_stdio_client(str(source), endpoint=endpoint, home=home)


@pytest.mark.parametrize(
    "changed", [CLIENT_NAME, RELAY_NAME, MANIFEST_NAME, SHIM_NAME, "source-codex"]
)
def test_package_refuses_changed_code_before_launch(tmp_path: Path, changed: str):
    endpoint = CodeModeEndpoint(Path("/tmp/absent-code-mode"), secrets.token_urlsafe(32))
    package = _package(tmp_path, endpoint)
    assert endpoint.token not in repr(endpoint)
    assert endpoint.token not in str(package.provenance())
    target = (
        tmp_path / changed if changed == "source-codex" else package.executable.with_name(changed)
    )
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed|binding"):
        package.verify()


@pytest.mark.parametrize(
    "payload", [b"\x01", (FRAME_LIMIT + 1).to_bytes(4, "little"), b"\x02\0\0\0x"]
)
@pytest.mark.usefixtures("host_stdio_scope")
def test_stdio_relay_refuses_partial_or_oversized_frame(tmp_path: Path, payload: bytes):
    async def scenario():
        with _socket_directory() as directory:
            endpoint = CodeModeEndpoint(directory / "ipc", secrets.token_urlsafe(32))
            disconnected = asyncio.Event()
            tasks: set[asyncio.Task] = set()

            async def peer(reader, writer):
                task = asyncio.current_task()
                assert task is not None
                tasks.add(task)
                try:
                    assert await reader.readexactly(44) == endpoint.token.encode() + b"\n"
                    writer.write(b"ok\n")
                    await writer.drain()
                    assert await reader.read() == b""
                finally:
                    writer.close()
                    await writer.wait_closed()
                    tasks.discard(task)
                    disconnected.set()

            async with await asyncio.start_unix_server(peer, endpoint.path):
                package = _package(tmp_path, endpoint)
                process = await asyncio.create_subprocess_exec(
                    str(package.executable.with_name(SHIM_NAME)),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await asyncio.wait_for(process.communicate(payload), 10)
                await asyncio.wait_for(disconnected.wait(), 3)
                assert process.returncode == 125
                assert output == b""
                assert endpoint.token.encode() not in error
                assert b"Code Mode stdio relay failed:" in error
                assert not tasks

    asyncio.run(scenario())


def _ambient_python_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    sentinel = tmp_path / "ambient-import-ran"
    (tmp_path / "sitecustomize.py").write_text(f"open({str(sentinel)!r}, 'w').close()")
    return (
        dict(
            os.environ,
            PYTHONPATH=str(tmp_path),
            PYTHONSTARTUP=str(tmp_path / "sitecustomize.py"),
        ),
        sentinel,
    )


def test_fixed_shim_rejects_arguments_before_connecting(tmp_path: Path):
    async def scenario():
        with _socket_directory() as directory:
            endpoint = CodeModeEndpoint(directory / "ipc", secrets.token_urlsafe(32))
            package = _package(tmp_path, endpoint)
            environment, sentinel = _ambient_python_environment(tmp_path)
            process = await asyncio.create_subprocess_exec(
                str(package.executable.with_name(SHIM_NAME)),
                "--listen",
                "stdio",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert await asyncio.wait_for(process.communicate(), 5) == (b"", b"")
            assert process.returncode == 64
            assert not sentinel.exists()

    asyncio.run(scenario())


@pytest.mark.usefixtures("host_stdio_scope")
def test_fixed_shim_roundtrips_frames_and_ignores_python_environment(tmp_path: Path):
    async def scenario():
        with _socket_directory() as directory:
            endpoint = CodeModeEndpoint(directory / "ipc", secrets.token_urlsafe(32))
            package = _package(tmp_path, endpoint)
            environment, sentinel = _ambient_python_environment(tmp_path)
            closed = asyncio.Event()
            frames = []

            async def peer(reader, writer):
                try:
                    assert await reader.readexactly(44) == endpoint.token.encode() + b"\n"
                    writer.write(b"ok\n")
                    await writer.drain()
                    while (frame := await read_frame(reader)) is not None:
                        frames.append(frame)
                        # Deliberately split the native header across reads.
                        for block in (frame[:1], frame[1:3], frame[3:]):
                            writer.write(block)
                            await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    closed.set()

            async with await asyncio.start_unix_server(peer, endpoint.path):
                process = await asyncio.create_subprocess_exec(
                    str(package.executable.with_name(SHIM_NAME)),
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdin and process.stdout and process.stderr
                frame = (256 * 1024).to_bytes(4, "little") + b"a" * (256 * 1024)
                process.stdin.write(frame)
                await process.stdin.drain()
                assert await asyncio.wait_for(process.stdout.readexactly(len(frame)), 10) == frame
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 5)
                await asyncio.wait_for(closed.wait(), 3)
                assert process.returncode == 0
                assert await process.stderr.read() == b""
                assert frames == [frame]
                assert not sentinel.exists()
                package.verify()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["eof", "cancelled", "malformed"])
@pytest.mark.usefixtures("host_stdio_scope")
def test_host_rejects_wrong_or_replayed_capability_and_owns_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
):
    from local_first_agent_os import codex_code_mode

    # Deliberate transport-only fixture. Real Seatbelt controls run separately.
    monkeypatch.setattr(
        codex_code_mode, "tempfile", SimpleNamespace(TemporaryDirectory=_socket_directory)
    )
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    interpreter = executable.with_name(SHIM_NAME)
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "while header := sys.stdin.buffer.read(4):\n"
        "    payload = sys.stdin.buffer.read(int.from_bytes(header, 'little'))\n"
        "    sys.stdout.buffer.write(header + payload)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    interpreter.chmod(0o700)

    @contextmanager
    def contain_service(command):
        yield SimpleNamespace(command=command, environment={"PATH": "/usr/bin:/bin"})

    boundary = cast(
        ReadOnlyToolWorker, SimpleNamespace(repository=tmp_path, contain_service=contain_service)
    )

    async def scenario():
        host = CodexCodeModeHost(boundary, str(executable))
        await host.__aenter__()
        endpoint = host.endpoint
        process = host._process
        assert process is not None
        try:
            assert stat.S_IMODE(endpoint.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(endpoint.path.parent.stat().st_mode) == 0o700
            wrong = CodeModeEndpoint(endpoint.path, secrets.token_urlsafe(32))
            with pytest.raises(asyncio.IncompleteReadError):
                await connect_endpoint(wrong)
            reader, writer = await connect_endpoint(endpoint)
            with pytest.raises(asyncio.IncompleteReadError):
                await connect_endpoint(endpoint)
            frame = b"\x02\0\0\0{}"
            writer.write(frame)
            await writer.drain()
            assert await asyncio.wait_for(read_frame(reader), 3) == frame
            host.require_alive()
            if ending == "malformed":
                writer.write((FRAME_LIMIT + 1).to_bytes(4, "little"))
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 3) == b""
            if ending == "cancelled":
                task = asyncio.create_task(host.aclose())
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                writer.close()
                await writer.wait_closed()
                await host.aclose()
        finally:
            await host.aclose()
        assert process.returncode == 0
        assert not endpoint.path.parent.exists()
        assert not host._handlers
        assert not host._connections
        with pytest.raises(ProcessContainmentUnavailable):
            host.require_alive()
        if ending == "malformed":
            with pytest.raises(ProcessContainmentUnavailable):
                await host.__aexit__(None)

    asyncio.run(scenario())


@pytest.mark.usefixtures("host_stdio_scope")
def test_failed_preflight_closes_its_client_connection(tmp_path: Path, monkeypatch):
    from local_first_agent_os import codex_code_mode

    async def scenario():
        with _socket_directory() as directory:
            endpoint = CodeModeEndpoint(directory / "ipc", secrets.token_urlsafe(32))
            closed = asyncio.Event()
            clients = []

            async def bad_interpreter(reader, writer):
                try:
                    assert await reader.readexactly(44) == endpoint.token.encode() + b"\n"
                    writer.write(b"ok\n")
                    await writer.drain()
                    assert await read_frame(reader) is not None
                    writer.write(b"\x02\0\0\0{}")
                    await writer.drain()
                    assert await reader.read() == b""
                finally:
                    writer.close()
                    await writer.wait_closed()
                    closed.set()

            class FixtureHost:
                def __init__(self, *args):
                    self.endpoint = endpoint

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

            async def observe_connection(value):
                reader, writer = await connect_endpoint(value)
                clients.append(writer)
                return reader, writer

            monkeypatch.setattr(codex_code_mode, "CodexCodeModeHost", FixtureHost)
            monkeypatch.setattr(codex_code_mode, "connect_endpoint", observe_connection)
            async with await asyncio.start_unix_server(bad_interpreter, endpoint.path):
                with pytest.raises(ProcessContainmentUnavailable, match="preflight failed"):
                    await codex_code_mode.preflight_code_mode_runtime(
                        cast(ReadOnlyToolWorker, object()), "unused"
                    )
                assert len(clients) == 1 and clients[0].is_closing()
                await asyncio.wait_for(closed.wait(), 3)

    asyncio.run(scenario())

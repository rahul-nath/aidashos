# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wire-contract tests; native ownership is qualified by the installed helper."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import pytest

from local_first_agent_os import uid_verifier_client as client_module
from local_first_agent_os.native_verification_broker import (
    BROKER_ENV,
    authenticated_contained_client,
)
from local_first_agent_os.uid_verifier_client import UidVerifierClient, UidVerifierUnavailable


@pytest.fixture(autouse=True)
def trusted_host_transport_authority():
    if authenticated_contained_client():
        pytest.skip(
            "requires trusted host helper transport; current UID is an authenticated gate client"
        )


@contextmanager
def _owner(tmp_path, monkeypatch, *, fault=None, delay=False):
    socket_directory = tempfile.TemporaryDirectory(prefix="uvc-")
    path = Path(socket_directory.name) / "owner.sock"
    staging = tmp_path / "g55000"
    staging.mkdir()
    for name in ("source", "toolchain"):
        (staging / name).mkdir()
    errors, received = [], []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    listener.settimeout(5)
    # This fixture tests framing, not kernel peer authentication or UID cleanup.
    monkeypatch.setattr(client_module, "_root_peer", lambda _socket: None)

    def serve():
        try:
            connection, _ = listener.accept()
            with connection, connection.makefile("rb") as stream:
                binding, launches = None, {}

                def send(value):
                    connection.sendall(json.dumps(value).encode() + b"\n")

                def receipt(uid):
                    return {
                        "kind": "cleaned",
                        "uid": uid,
                        "source_binding": binding,
                        "lease_digest": "b" * 64,
                    }

                def complete(handle, code):
                    send(
                        {
                            "kind": "stdout",
                            "handle": handle,
                            "data": base64.b64encode(b"worker evidence\n").decode(),
                        }
                    )
                    send(
                        {
                            "kind": "stderr",
                            "handle": handle,
                            "data": base64.b64encode(b"worker diagnostic\n").decode(),
                        }
                    )
                    send(
                        {
                            "kind": "exit",
                            "handle": handle,
                            "code": code,
                            "receipt": receipt(launches[handle]),
                        }
                    )

                for raw in stream:
                    request = json.loads(raw)
                    received.append(request)
                    kind = request["kind"]
                    if kind == "open":
                        binding = request["source_binding"]
                        send(
                            {
                                "kind": "opened",
                                "gate_handle": "gate",
                                "gid": 55000,
                                "staging": str(staging),
                                "source": str(staging / "source"),
                                "toolchain": str(staging / "toolchain"),
                            }
                        )
                    elif kind == "prepare":
                        uid = 55001 + len(launches)
                        handle = str(uid)
                        launches[handle] = uid
                        launch = staging / ("l" + handle)
                        for name in ("home", "scratch"):
                            (launch / name).mkdir(parents=True)
                        send(
                            {
                                "kind": "prepared",
                                "handle": handle,
                                "uid": uid,
                                "gid": 55000,
                                "home": str(launch / "home"),
                                "scratch": str(launch / "scratch"),
                            }
                        )
                    elif kind == "launch":
                        handle = request["handle"]
                        declaration = {
                            key: request[key]
                            for key in ("argv", "cwd", "environment", "profile", "source_binding")
                        }
                        declaration["environment"] = sorted(declaration["environment"].items())
                        digest = hashlib.sha256(
                            json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest()
                        send(
                            {
                                "kind": "launched",
                                "handle": handle,
                                "uid": launches[handle],
                                "pid": 12345,
                                "pgid": 12345,
                                "digest": "c" * 64,
                                "requested_digest": "d" * 64 if fault == "binding" else digest,
                            }
                        )
                        if fault == "disconnect":
                            return
                        if not delay and fault != "binding":
                            complete(handle, 0)
                    elif kind in ("terminate", "cancel"):
                        complete(
                            request["handle"],
                            -signal.SIGTERM if kind == "terminate" else -signal.SIGKILL,
                        )
                    elif kind == "close":
                        children = (
                            []
                            if fault == "missing-child"
                            else [receipt(uid) for uid in launches.values()]
                        )
                        send(
                            {
                                "kind": "gate_closed",
                                "receipt": receipt(55000),
                                "launch_receipts": children,
                            }
                        )
                        return
        except (BrokenPipeError, ConnectionResetError):
            pass
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path, received
    finally:
        listener.close()
        thread.join(5)
        socket_directory.cleanup()
        assert not thread.is_alive()
        assert not errors


def _launch(owner):
    prepared = owner.prepare(parent=None, scratch=None)
    return owner.launch(
        prepared,
        ("/usr/bin/true",),
        owner.staging.source,
        {"HOME": str(prepared.home), "TMPDIR": str(prepared.scratch)},
        "(version 1)(allow default)",
    )


def test_exact_launch_output_and_complete_uid_set(tmp_path: Path, monkeypatch) -> None:
    with (
        _owner(tmp_path, monkeypatch) as (path, _requests),
        UidVerifierClient(
            source_binding="a" * 64, deadline=monotonic() + 10, process_limit=8, socket_path=path
        ) as owner,
    ):
        process = _launch(owner)
        assert process.communicate(timeout=2) == (b"worker evidence\n", b"worker diagnostic\n")
        assert process.returncode == 0
        assert process.receipt is not None
        assert process.identity is not None
        assert process.identity.requested_digest == process.requested_digest
        with pytest.raises(UidVerifierUnavailable, match="unproven UID ownership"):
            owner.require_closed_staging(owner.staging.toolchain)
        owner.close()
        owner.require_closed_staging(owner.staging.toolchain)
        with pytest.raises(UidVerifierUnavailable, match="does not select"):
            owner.require_closed_staging(owner.staging.source)


@pytest.mark.parametrize("value,kind", [(signal.SIGTERM, "terminate"), (signal.SIGKILL, "cancel")])
def test_termination_and_forced_cancel_are_distinct(
    tmp_path: Path, monkeypatch, value, kind
) -> None:
    with (
        _owner(tmp_path, monkeypatch, delay=True) as (path, requests),
        UidVerifierClient(
            source_binding="a" * 64, deadline=monotonic() + 10, process_limit=8, socket_path=path
        ) as owner,
    ):
        process = _launch(owner)
        process.send_signal(value)
        assert process.wait(timeout=2) == -value
        assert requests[-1] == {"kind": kind, "handle": process.prepared.handle}


@pytest.mark.parametrize("fault", ["binding", "disconnect", "missing-child"])
def test_no_success_without_exact_launch_and_aggregate_cleanup(
    tmp_path: Path, monkeypatch, fault
) -> None:
    with (
        _owner(tmp_path, monkeypatch, fault=fault) as (path, _requests),
        pytest.raises(UidVerifierUnavailable),
        UidVerifierClient(
            source_binding="a" * 64,
            deadline=monotonic() + 10,
            process_limit=8,
            socket_path=path,
        ) as owner,
    ):
        process = _launch(owner)
        process.communicate(timeout=2)


@pytest.mark.skipif(sys.platform != "darwin" or os.geteuid() == 0, reason="unprivileged macOS peer")
def test_kernel_peer_must_be_root() -> None:
    first, second = socket.socketpair()
    with first, second, pytest.raises(UidVerifierUnavailable, match="not authenticated root"):
        client_module._root_peer(first)


def test_environment_and_same_uid_socket_cannot_fake_contained_context(tmp_path, monkeypatch):
    with _owner(tmp_path, monkeypatch) as (path, _requests):
        monkeypatch.setenv(
            BROKER_ENV,
            json.dumps(
                {
                    "socket": str(path),
                    "proxy": "irrelevant",
                    "nonce": "claimed-authority",
                }
            ),
        )
        assert not authenticated_contained_client()

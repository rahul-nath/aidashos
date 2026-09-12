# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Synthetic resources exercise real containment without acquiring a Neon login."""

from __future__ import annotations

import inspect
import json
import socket
import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
import test_host_verification_receipts
import test_local_verification_resources
from host_verifier_capability import require_host_uid_verifier
from test_host_verification_receipts import GateFixture
from test_host_verification_toolchain import _commit

from local_first_agent_os import host_verification as host
from local_first_agent_os import verification_resources as resources
from local_first_agent_os.coordination.store import tx

gate_fixture = test_host_verification_receipts.gate_fixture
host_local_resource_owner = test_local_verification_resources.host_local_resource_owner


def test_real_local_resource_gate_cannot_read_owner_or_use_known_owner_login(
    host_local_resource_owner, gate_fixture, monkeypatch
) -> None:
    directory = gate_fixture.repository.parent / "protected-resources"
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    subject = host.verification_subject_for_dispatch(gate_fixture.intent_id)
    assert subject is not None
    resources.initialize_local_verification_resources(
        subject.target_project_id,
        gate_fixture.repository,
        "postgresql://postgres:postgres@127.0.0.1:5433/local_agent",
    )
    monkeypatch.setattr(
        host, "acquire_verification_resources", resources.acquire_verification_resources
    )
    (gate_fixture.repository / "local_resource_gate.py").write_text(
        "from pathlib import Path\nimport os, socket, psycopg\n"
        "from psycopg import sql\n"
        "url = os.environ['LOCAL_AGENT_TEST_DATABASE_URL']\n"
        "with psycopg.connect(url, autocommit=True) as connection:\n"
        "    role = connection.execute('SELECT current_user').fetchone()[0]\n"
        "    assert role.startswith('aidashos_verify_')\n"
        "    schema = sql.Identifier('leased_' + role)\n"
        "    connection.execute(sql.SQL('CREATE SCHEMA {}').format(schema))\n"
        "    try: connection.execute('SET ROLE postgres')\n"
        "    except psycopg.errors.InsufficientPrivilege: pass\n"
        "    else: raise AssertionError('owner role allowed')\n"
        "try: psycopg.connect(url, user='postgres', password='postgres', connect_timeout=2)\n"
        "except psycopg.OperationalError: pass\n"
        "else: raise AssertionError('owner login allowed')\n"
        "for action in [lambda: socket.create_connection(('127.0.0.1', 5433), timeout=2), "
        f"lambda: Path({str(directory / 'database-url')!r}).read_bytes()]:\n"
        "    try: action()\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('protected owner authority allowed')\n"
        "print('leased role works; owner login, direct database and credential read denied')\n"
    )
    commit = _commit(gate_fixture.repository)
    center = host.load_project_center()
    project = replace(
        center.projects[0],
        verification_commands=[
            "uv run --offline --no-sync python local_resource_gate.py",
            "uv run --offline --no-sync python gate.py",
        ],
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    result = replace(gate_fixture, commit=commit).run()
    assert isinstance(result, host.VerificationPassed), result
    assert "owner login, direct database and credential read denied" in result.captures[0].stdout
    assert "source write and outside read denied" in result.captures[1].stdout
    verified = host.resolve_verification_receipt(result.receipt_id, subject)
    assert isinstance(verified, host.VerifiedReceiptReference)
    assert isinstance(verified.receipt.resources, host.ClosedVerificationResource)
    assert isinstance(
        verified.receipt.resources.identity, resources.LocalPostgresVerificationResourceIdentity
    )


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.settimeout(2)
        connection.sendall(connection.recv(1024))


@pytest.fixture
def pinned_relay() -> Iterator[resources._PinnedLoopbackRelay]:
    # This relay belongs to the host gate owner, not to an already contained child.
    # Check the same gate prerequisite before creating its listening sockets.
    require_host_uid_verifier()
    with socketserver.TCPServer(("127.0.0.1", 0), _EchoHandler) as server:
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        relay = resources._PinnedLoopbackRelay(
            ("127.0.0.1", server.server_address[1]), startup=resources.OpaquePostgresTls()
        )
        try:
            yield relay
        finally:
            relay_closed = relay.close()
            server.shutdown()
            serving.join(timeout=2)
            assert relay_closed
            assert not serving.is_alive()


def test_relay_checks_host_authority_before_opening_listeners(monkeypatch) -> None:
    def contained_client_refusal() -> None:
        pytest.skip("authenticated contained client cannot provision a host relay")

    def forbidden_listener(*_args, **_kwargs):
        raise AssertionError("listener creation preceded host authority")

    monkeypatch.setattr(f"{__name__}.require_host_uid_verifier", contained_client_refusal)
    monkeypatch.setattr(socketserver, "TCPServer", forbidden_listener)
    with pytest.raises(pytest.skip.Exception, match="cannot provision a host relay"):
        next(inspect.unwrap(pinned_relay)())


@dataclass(frozen=True)
class ResourceFixture:
    lease: resources.VerificationResourceLease
    worker_secret: str
    owner_secret: str
    close_calls: list[str]
    certificate: Path


def _resource_fixture(
    gate: GateFixture,
    monkeypatch: pytest.MonkeyPatch,
    relay: resources._PinnedLoopbackRelay,
    *,
    cleanup: Literal["closed", "pending", "raise"],
) -> ResourceFixture:
    certificate = gate.repository.parent / "fixture-ca.pem"
    certificate.write_text("synthetic-public-ca")
    common = (
        host._git(gate.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode()
        .strip()
    )
    subject = host.verification_subject_for_dispatch(gate.intent_id)
    assert subject is not None
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id=subject.target_project_id,
        neon_project_id="fixture-neon",
        lease_id="a" * 32,
        role="aidashos_verify_" + "a" * 32,
        source_common_directory=common,
        host="ep-fixture.us-east-2.aws.neon.tech",
        # A shape-valid synthetic endpoint; this fixture never connects to it.
        hostaddr="8.8.8.8",
        relay_port=relay.port,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    owner_secret, worker_secret = "synthetic-owner-secret", "synthetic-worker-secret"
    owner = resources._NeonConnection(
        identity.host, identity.hostaddr, "synthetic_owner", owner_secret, certificate
    )
    worker = resources._NeonConnection(
        identity.host, identity.hostaddr, identity.role, worker_secret, certificate
    )
    lease = resources.VerificationResourceLease(
        identity, owner, worker, gate.repository.parent / "lease.json", relay
    )
    close_calls: list[str] = []

    def close(actual: resources.VerificationResourceLease) -> resources.VerificationResourceCleanup:
        close_calls.append(actual.identity.lease_id)
        assert relay.close()
        if cleanup == "raise":
            raise OSError(owner_secret)
        if cleanup == "pending":
            return resources.VerificationResourceCleanupPending(actual.identity.lease_id)
        return resources.VerificationResourceClosed(actual.identity.lease_id)

    def acquire(project_id: str, repository: Path) -> resources.VerificationResourcesPresent:
        assert project_id == identity.target_project_id
        assert repository == gate.repository
        return resources.VerificationResourcesPresent(lease)

    monkeypatch.setattr(host, "acquire_verification_resources", acquire)
    monkeypatch.setattr(resources.VerificationResourceLease, "close", close)
    return ResourceFixture(lease, worker_secret, owner_secret, close_calls, certificate)


@pytest.mark.parametrize("cleanup", ["closed", "pending", "raise"])
def test_actual_resource_gate_preserves_process_truth_and_separate_cleanup(
    gate_fixture: GateFixture,
    monkeypatch: pytest.MonkeyPatch,
    pinned_relay: resources._PinnedLoopbackRelay,
    cleanup: Literal["closed", "pending", "raise"],
) -> None:
    fixture = _resource_fixture(gate_fixture, monkeypatch, pinned_relay, cleanup=cleanup)
    (gate_fixture.repository / "resource_gate.py").write_text(
        "import os\nimport socket\nimport sys\nfrom pathlib import Path\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "assert 'LOCAL_AGENT_COORDINATION_DATABASE_URL' not in os.environ\n"
        "print(os.environ['LOCAL_AGENT_TEST_DATABASE_URL'])\n"
        "print(os.environ['LOCAL_AGENT_TEST_DATABASE_URL'], file=sys.stderr)\n"
        "url = urlsplit(os.environ['LOCAL_AGENT_TEST_DATABASE_URL'])\n"
        "ca = Path(parse_qs(url.query)['sslrootcert'][0])\n"
        f"assert ca != Path({str(fixture.certificate)!r})\n"
        "assert ca.read_text() == 'synthetic-public-ca'\n"
        f"for action in [lambda: Path({str(fixture.certificate)!r}).read_bytes(), "
        "lambda: ca.write_text('replace trust')]:\n"
        "    try: action()\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('private CA read or staged CA write allowed')\n"
        "print('staged CA readable; original private CA read and staged CA write denied')\n"
        "assert url.hostname == 'ep-fixture.us-east-2.aws.neon.tech'\n"
        "assert parse_qs(url.query)['sslmode'] == ['verify-full']\n"
        "assert parse_qs(url.query)['hostaddr'] == ['127.0.0.1']\n"
        "with socket.create_connection(('127.0.0.1', url.port), timeout=2) as sock:\n"
        "    sock.sendall(b'declared relay reachable')\n"
        "    assert sock.recv(1024) == b'declared relay reachable'\n"
        "print('declared relay reachable')\n"
        "with socket.socket() as sock:\n"
        "    sock.settimeout(0.2)\n"
        "    try: sock.connect(('127.0.0.1', 31337))\n"
        "    except PermissionError: print('undeclared network denied')\n"
        "    else: raise AssertionError('undeclared network was allowed')\n"
    )
    commit = _commit(gate_fixture.repository)
    center = host.load_project_center()
    project = replace(
        center.projects[0],
        verification_commands=[
            "uv run --offline --no-sync python resource_gate.py",
            "uv run --offline --no-sync python gate.py",
        ],
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    observed = replace(gate_fixture, commit=commit).run()
    if cleanup == "closed":
        assert isinstance(observed, host.VerificationPassed), observed
        passed = observed
    else:
        assert isinstance(observed, host.VerificationCleanupPending), observed
        assert isinstance(observed.verification, host.VerificationPassed)
        assert observed.resource.identity == fixture.lease.identity
        passed = observed.verification
    assert fixture.close_calls == [fixture.lease.identity.lease_id]
    assert fixture.certificate.read_text() == "synthetic-public-ca"
    assert "staged CA readable; original private CA read and staged CA write denied" in (
        passed.captures[0].stdout
    )
    assert "declared relay reachable" in passed.captures[0].stdout
    assert "undeclared network denied" in passed.captures[0].stdout
    assert "source write and outside read denied" in passed.captures[1].stdout
    with tx() as connection:
        row = connection.execute(
            "SELECT payload_json, output_json FROM host_verification_receipts WHERE receipt_id=?",
            (passed.receipt_id,),
        ).fetchone()
    assert row is not None
    retained = row["payload_json"] + row["output_json"]
    for secret in (fixture.owner_secret, fixture.worker_secret):
        assert secret not in retained
        assert all(secret not in capture.stdout + capture.stderr for capture in passed.captures)
    payload = json.loads(row["payload_json"])
    assert payload["process_outcome"] == "passed"
    assert payload["resources"]["kind"] == ("closed" if cleanup == "closed" else "cleanup_pending")
    subject = host.verification_subject_for_dispatch(gate_fixture.intent_id)
    assert subject is not None
    evidence = host.resolve_verification_receipt(passed.receipt_id, subject)
    if cleanup == "closed":
        assert isinstance(evidence, host.VerifiedReceiptReference), evidence
    else:
        assert isinstance(evidence, host.RetainedLegacyObservation), evidence
        replay = host.resolve_retained_verification_outcome(
            passed.receipt_id, subject, gate_fixture.lease_id, commit
        )
        assert isinstance(replay, host.VerificationCleanupPending), replay


def test_setup_failure_still_closes_the_resource_and_redacts_the_error(
    gate_fixture: GateFixture,
    monkeypatch: pytest.MonkeyPatch,
    pinned_relay: resources._PinnedLoopbackRelay,
) -> None:
    fixture = _resource_fixture(gate_fixture, monkeypatch, pinned_relay, cleanup="closed")

    def fail_toolchain(
        _project: Path, *, source_root: Path | None = None
    ) -> host._InstalledToolchain:
        raise ValueError("toolchain unavailable: " + fixture.worker_secret)

    monkeypatch.setattr(host, "_installed_toolchain", fail_toolchain)
    observed = gate_fixture.run()
    assert isinstance(observed, host.VerificationUnavailable), observed
    assert fixture.worker_secret not in observed.reason
    assert fixture.close_calls == [fixture.lease.identity.lease_id]


def test_resource_cannot_override_the_command_environment(
    gate_fixture: GateFixture,
    monkeypatch: pytest.MonkeyPatch,
    pinned_relay: resources._PinnedLoopbackRelay,
) -> None:
    fixture = _resource_fixture(gate_fixture, monkeypatch, pinned_relay, cleanup="closed")
    monkeypatch.setattr(
        resources.VerificationResourceLease,
        "environment",
        lambda _self, **_kwargs: {"PATH": "/"},
    )
    observed = gate_fixture.run()
    assert isinstance(observed, host.VerificationUnavailable), observed
    assert "unrelated process authority" in observed.reason
    assert fixture.close_calls == [fixture.lease.identity.lease_id]

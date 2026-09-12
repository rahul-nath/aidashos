# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import socket
import socketserver
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from conftest import suite_postgres_source
from host_test_scope import require_uncontained_scope
from postgres_server import ManagedPostgres
from psycopg import sql

from local_first_agent_os import verification_resources as resources
from local_first_agent_os.macho_dependencies import PinnedRuntimeFile
from local_first_agent_os.verification_toolchain_staging import (
    PublicVerificationCa,
    StagedVerificationCa,
)

_HOST = "ep-example-leaf-ab123.us-east-1.aws.neon.tech"


@pytest.fixture(autouse=True)
def isolated_resource_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "_resource_directory", lambda: tmp_path / "resource-default")


def _connection(tmp_path: Path) -> resources._NeonConnection:
    ca = tmp_path / "ca.pem"
    ca.write_text("fixture CA")
    return resources._NeonConnection(_HOST, "8.8.8.8", "fixture_owner", "canary-credential", ca)


def test_other_projects_never_open_owner_credentials(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "protected"
    _protected_configuration(directory)
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    monkeypatch.setattr(resources, "_load_owner", lambda *_args: pytest.fail("resource touched"))
    assert isinstance(
        resources.acquire_verification_resources("other-project", Path("/untrusted")),
        resources.VerificationResourcesAbsent,
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://owner:secret@localhost/neondb",
        "postgresql://owner:secret@ep-example-pooler.us-east-1.aws.neon.tech/neondb",
        f"postgresql://owner:secret@{_HOST}:5433/neondb",
        f"postgresql://owner:secret@{_HOST}/production",
        f"postgresql://owner:secret@{_HOST}/neondb?hostaddr=127.0.0.1",
        f"postgresql://owner:secret@{_HOST}/neondb?options=-csearch_path=public",
        f"postgresql://owner:secret@{_HOST}/neondb?sslmode=require&sslmode=disable",
    ],
)
def test_undeclared_resource_endpoints_refuse_before_dns(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "_resolve_host", lambda _: pytest.fail("DNS touched"))
    with pytest.raises(resources._ResourceRefusal):
        resources._connection_from_secret(url)


def test_host_resolves_ip_and_keeps_original_tls_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "_resolve_host", lambda _: "8.8.8.8")
    connection = resources._connection_from_secret(
        f"postgresql://owner:secret@{_HOST}/neondb?sslmode=require"
    )
    parsed = urlsplit(connection.url())
    query = parse_qs(parsed.query)
    assert parsed.hostname == _HOST
    assert query["hostaddr"] == ["8.8.8.8"]
    assert query["sslmode"] == ["verify-full"]
    assert query["sslrootcert"] == [str(Path(resources.certifi.where()).resolve())]
    assert query["channel_binding"] == ["require"]


def test_dns_rebinding_to_local_address_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5432))],
    )
    with pytest.raises(resources._ResourceRefusal, match="public IPv4"):
        resources._resolve_host(_HOST)


@pytest.mark.parametrize("mode", (0o644, 0o640, 0o666))
def test_readable_secret_files_refuse(tmp_path: Path, mode: int) -> None:
    secret = tmp_path / "credential"
    secret.write_text("fixture credential")
    secret.chmod(mode)
    with pytest.raises(resources._ResourceRefusal, match="0600"):
        resources._read_protected(secret)


def test_symlink_secret_is_never_followed(tmp_path: Path) -> None:
    secret = tmp_path / "credential"
    secret.write_text("fixture credential")
    secret.chmod(0o600)
    link = tmp_path / "alias"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        resources._read_protected(link)


def test_resource_identity_manifest_and_repr_exclude_credentials(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="fixture_project",
        neon_project_id="fixture-neon",
        source_common_directory="/fixture/.git",
        lease_id="a" * 32,
        role="aidashos_verify_" + "a" * 32,
        host=_HOST,
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2026-09-08T12:00:00+00:00",
    )
    manifest = tmp_path / "lease.json"
    # Identity rendering has no transport side effects. Actual relay lifetime is
    # covered by the host forwarding/cleanup tests, not by this serialization test.
    relay = Mock(spec=resources._PinnedLoopbackRelay)
    lease = resources.VerificationResourceLease(identity, connection, connection, manifest, relay)
    resources._write_manifest(manifest, identity, resources.VerificationResourceState.READY)
    assert "canary-credential" not in repr(lease)
    assert "canary-credential" not in manifest.read_text()
    assert manifest.stat().st_mode & 0o777 == 0o600
    staged_file = tmp_path / "staged-ca.pem"
    staged_file.write_bytes(connection.ca_file.read_bytes())
    staged_ca = StagedVerificationCa(
        PublicVerificationCa(connection.ca_file), PinnedRuntimeFile.capture(staged_file)
    )
    environment = lease.environment(staged_ca=staged_ca)
    assert set(environment) == {"LOCAL_AGENT_TEST_DATABASE_URL"}
    assert lease.sandbox_rules() == (
        f'(allow network-outbound (remote tcp "localhost:{identity.relay_port}"))',
    )
    child_url = urlsplit(environment["LOCAL_AGENT_TEST_DATABASE_URL"])
    assert child_url.hostname == _HOST
    assert child_url.port == identity.relay_port
    assert parse_qs(child_url.query)["hostaddr"] == ["127.0.0.1"]
    assert parse_qs(child_url.query)["sslrootcert"] == [str(staged_file)]
    assert lease._worker is connection and lease._owner is connection
    assert connection.ca_file == tmp_path / "ca.pem"
    assert lease.identity is identity
    assert lease.redact(connection.url() + " canary-credential") == (
        "[REDACTED_VERIFICATION_DATABASE_URL] [REDACTED_VERIFICATION_CREDENTIAL]"
    )
    assert json.loads(manifest.read_text())["identity"]["authority"] == "connect_create_own_schemas"
    assert relay.mock_calls == []


def test_provisioning_exception_never_echoes_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(
        configuration, *, source_repository: Path | None = None
    ) -> tuple[resources._NeonConnection, Path, Path]:
        raise RuntimeError("postgresql://secret:must-not-leak@host/neondb")

    monkeypatch.setattr(resources, "_load_owner", fail)
    monkeypatch.setattr(
        resources,
        "_load_configuration",
        lambda: resources.NeonConfiguration(
            target_project_id="fixture_project",
            target_repository="/fixture",
            connection_file="/protected/database-url",
            project_id="fixture-neon",
        ),
    )
    result = resources.acquire_verification_resources("fixture_project", Path("/fixture"))
    assert isinstance(result, resources.VerificationResourcesRefused)
    assert "must-not-leak" not in repr(result)


def test_cleanup_recovery_rejects_arbitrary_identity_before_resource_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "_load_owner", lambda: pytest.fail("resource touched"))
    with pytest.raises(ValueError, match="generated hexadecimal UUID"):
        resources.cleanup_verification_resource_lease("../../owner")


def _protected_configuration(directory: Path) -> Path:
    directory.mkdir(mode=0o700)
    path = directory / "verification.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "neon_verification_setup.v2",
                "project_id": "fixture-neon",
                "target_project_id": "fixture_project",
                "project_purpose": "isolated_verification_only",
                "expected_postgres_major": 18,
                "connection_file": str(directory / "database-url"),
                "target_repository": "/trusted/repository",
            }
        )
    )
    path.chmod(0o600)
    return path


def test_same_project_id_wrong_repository_refuses_before_secret_or_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "protected"
    _protected_configuration(directory)
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    monkeypatch.setattr(resources, "_assert_protected_directory", lambda _: None)
    monkeypatch.setattr(resources, "_git_common_directory", lambda repo: repo / ".git")
    monkeypatch.setattr(resources, "_resolve_host", lambda _: pytest.fail("DNS touched"))
    result = resources.acquire_verification_resources("fixture_project", Path("/fixture"))
    assert isinstance(result, resources.VerificationResourcesRefused)
    assert result.code is resources.VerificationResourceFailure.CONFIGURATION_REFUSED
    assert "differs from the protected resource owner" in result.reason
    assert not (directory / "database-url").exists()


def test_registered_worktree_common_directory_reaches_credential_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "protected"
    _protected_configuration(directory)
    credential = directory / "database-url"
    credential.write_text("fixture credential")
    credential.chmod(0o600)
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    monkeypatch.setattr(resources, "_assert_protected_directory", lambda _: None)
    seen: list[Path] = []

    def common_directory(repository: Path) -> Path:
        seen.append(repository)
        return Path("/trusted/repository/.git")

    connection = _connection(tmp_path)
    monkeypatch.setattr(resources, "_git_common_directory", common_directory)
    monkeypatch.setattr(resources, "_connection_from_secret", lambda secret: connection)
    configuration = resources._load_configuration()
    assert configuration is not None
    owner, resource_directory, common = resources._load_owner(
        configuration, source_repository=Path("/worktree")
    )
    assert owner is connection
    assert resource_directory == directory
    assert common == Path("/trusted/repository/.git")
    assert seen == [Path("/trusted/repository"), Path("/worktree")]


def test_duplicate_configuration_field_refuses_before_repository_or_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "protected"
    config = _protected_configuration(directory)
    config.write_text(config.read_text()[:-1] + ', "target_repository": "/other"}')
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    monkeypatch.setattr(resources, "_assert_protected_directory", lambda _: None)
    monkeypatch.setattr(resources, "_git_common_directory", lambda _: pytest.fail("Git touched"))
    result = resources.acquire_verification_resources("fixture_project", Path("/fixture"))
    assert isinstance(result, resources.VerificationResourcesRefused)


@pytest.mark.parametrize("hostaddr", ["127.0.0.1", "10.0.0.1", "not-an-ip", "8.8.8.8:443"])
def test_lease_identity_cannot_authorize_undeclared_sandbox_endpoint(hostaddr: str) -> None:
    with pytest.raises(ValueError):
        resources.NeonVerificationResourceIdentity(
            target_project_id="fixture_project",
            neon_project_id="fixture-neon",
            source_common_directory="/trusted/.git",
            lease_id="a" * 32,
            role="aidashos_verify_" + "a" * 32,
            host=_HOST,
            hostaddr=hostaddr,
            relay_port=12345,
            expires_at="2026-09-08T12:00:00+00:00",
        )


@pytest.mark.parametrize("violation_column", [None, 0, 1, 2, 3, 4, 5, 6, 8, 9, 10])
def test_worker_authority_allows_own_schema_progress_and_refuses_widening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, violation_column: int | None
) -> None:
    row: list[bool | int] = [False] * 7 + [True, 0, 0, 0]
    if violation_column is not None:
        row[violation_column] = True

    class Cursor:
        def fetchone(self) -> list[bool | int]:
            return row

    class Database:
        def execute(self, query: str, parameters: tuple[str, ...]) -> Cursor:
            assert "pg_database" in query and "pg_namespace" in query
            assert parameters == ("fixture_owner",)
            return Cursor()

    @contextmanager
    def database(_connection: resources._NeonConnection) -> Iterator[Database]:
        yield Database()

    monkeypatch.setattr(resources._NeonConnection, "open", database)
    if violation_column is None:
        resources._verify_worker(_connection(tmp_path), "fixture_owner")
    else:
        with pytest.raises(resources._ResourceRefusal, match="own-schema authority"):
            resources._verify_worker(_connection(tmp_path), "fixture_owner")


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while data := self.request.recv(65536):
            self.request.sendall(data)


@pytest.fixture
def loopback_echo() -> Iterator[int]:
    require_uncontained_scope(
        reason="real pinned-relay integration requires a host TCP listener",
        required_flag="LOCAL_AGENT_REQUIRE_HOST_NETWORK_TESTS",
    )

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), _EchoHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield int(server.server_address[1])
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_pinned_relay_forwards_opaque_bytes_and_closes_active_connections(
    loopback_echo: int,
) -> None:
    relay = resources._PinnedLoopbackRelay(
        ("127.0.0.1", loopback_echo), startup=resources.OpaquePostgresTls()
    )
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as client:
            # CONNECT is opaque application data, never a destination instruction.
            message = b"CONNECT other-endpoint.invalid:443 HTTP/1.1\r\n\r\n"
            client.sendall(message)
            assert client.recv(len(message)) == message
            assert relay.close()
            assert client.recv(1) == b""
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", relay.port), timeout=1)
        assert relay.close()
    finally:
        relay.close()


def test_native_seatbelt_allows_only_pinned_loopback_relay(loopback_echo: int) -> None:
    relay = resources._PinnedLoopbackRelay(
        ("127.0.0.1", loopback_echo), startup=resources.OpaquePostgresTls()
    )
    profile = (
        "(version 1) (allow default) (deny network*) "
        f'(allow network-outbound (remote tcp "localhost:{relay.port}"))'
    )
    probe = """
import errno,json,socket,sys
allowed,denied = map(int, sys.argv[1:])
with socket.create_connection(('127.0.0.1',allowed),timeout=3) as client:
    client.sendall(b'contained-echo')
    positive=client.recv(14)==b'contained-echo'
try:
    socket.create_connection(('127.0.0.1',denied),timeout=1)
except OSError as exc:
    negative=exc.errno in (errno.EPERM,errno.EACCES)
else:
    negative=False
print(json.dumps({'allowed':positive,'other_port_denied':negative}))
"""
    try:
        process = subprocess.run(
            (
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                sys.executable,
                "-c",
                probe,
                str(relay.port),
                str(loopback_echo),
            ),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout) == {"allowed": True, "other_port_denied": True}
    finally:
        assert relay.close()


def test_relay_bounds_live_connections_and_releases_capacity(loopback_echo: int) -> None:
    relay = resources._PinnedLoopbackRelay(
        ("127.0.0.1", loopback_echo), startup=resources.OpaquePostgresTls()
    )
    clients: list[socket.socket] = []
    try:
        for _ in range(resources._CONNECTION_LIMIT):
            client = socket.create_connection(("127.0.0.1", relay.port), timeout=3)
            clients.append(client)
            client.sendall(b"x")
            assert client.recv(1) == b"x"
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as overflow:
            assert overflow.recv(1) == b""
        assert relay.close()
        for client in clients:
            assert client.recv(1) == b""
    finally:
        relay.close()
        for client in clients:
            client.close()


def test_cleanup_terminates_generated_role_sessions_across_databases() -> None:
    source = suite_postgres_source()
    if not isinstance(source, ManagedPostgres):
        pytest.skip("role lifecycle probe requires the disposable local PostgreSQL test server")
    lease_id = uuid.uuid4().hex
    role = "aidashos_verify_" + lease_id
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="fixture_project",
        neon_project_id="fixture-neon",
        source_common_directory="/fixture/.git",
        lease_id=lease_id,
        role=role,
        host=_HOST,
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2026-09-08T12:00:00+00:00",
    )
    worker: psycopg.Connection | None = None
    with psycopg.connect(source.url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal("disposable-local-test-only"),
            )
        )
        try:
            worker = psycopg.connect(
                source.url,
                user=role,
                password="disposable-local-test-only",
                dbname="postgres",
                autocommit=True,
            )
            worker.execute("CREATE TEMP TABLE role_cleanup_canary(value int)")
            backend = worker.execute("SELECT pg_backend_pid()").fetchone()
            assert backend is not None
            worker_pid = backend[0]

            resources._terminate_role_connections(owner, identity)

            surviving = owner.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid=%s", (worker_pid,)
            ).fetchone()
            assert surviving == (0,), "lease role session in another database survived cleanup"
            assert owner.execute("SELECT 1").fetchone() == (1,)
            with pytest.raises(psycopg.OperationalError):
                worker.execute("SELECT * FROM role_cleanup_canary")
        finally:
            owner.execute(
                "SELECT pg_terminate_backend(pid, 1000) FROM pg_stat_activity WHERE usename=%s",
                (role,),
            ).fetchall()
            if worker is not None:
                worker.close()
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_nonsuperuser_creator_can_clean_owned_schemas_without_worker_escalation() -> None:
    source = suite_postgres_source()
    if not isinstance(source, ManagedPostgres):
        pytest.skip("role lifecycle probe requires the disposable local PostgreSQL test server")
    lease_id = uuid.uuid4().hex
    worker_role = "aidashos_verify_" + lease_id
    provisioner = "canary_provisioner_" + lease_id
    owned_schema = "canary_owned_" + lease_id
    control_schema = "canary_control_" + lease_id
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="fixture_project",
        neon_project_id="fixture-neon",
        source_common_directory="/fixture/.git",
        lease_id=lease_id,
        role=worker_role,
        host=_HOST,
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2026-09-08T12:00:00+00:00",
    )
    with psycopg.connect(source.url, autocommit=True) as admin:
        database_row = admin.execute("SELECT current_database()").fetchone()
        assert database_row is not None
        database = database_row[0]
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER CREATEROLE NOINHERIT PASSWORD {}").format(
                sql.Identifier(provisioner),
                sql.Literal("disposable-local-test-only"),
            )
        )
        admin.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {} WITH GRANT OPTION").format(
                sql.Identifier(database),
                sql.Identifier(provisioner),
            )
        )
        try:
            with psycopg.connect(
                source.url, user=provisioner, password="disposable-local-test-only", autocommit=True
            ) as owner:
                owner.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(worker_role)
                    )
                )
                # Reproduce the actual PG18 creator membership, irrespective of
                # the local server's createrole_self_grant configuration.
                admin.execute(
                    sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE").format(
                        sql.Identifier(worker_role),
                        sql.Identifier(provisioner),
                    )
                )
                role_row = admin.execute(
                    "SELECT oid FROM pg_roles WHERE rolname=%s", (worker_role,)
                ).fetchone()
                assert role_row is not None
                worker_oid = role_row[0]
                admin.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                        sql.Identifier(owned_schema), sql.Identifier(worker_role)
                    )
                )
                owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(control_schema)))
                flags = owner.execute(
                    "SELECT pg_has_role(current_user,%s,'USAGE'), "
                    "pg_has_role(current_user,%s,'SET')",
                    (worker_role, worker_role),
                ).fetchone()
                assert flags == (False, False)
                with (
                    pytest.raises(psycopg.errors.InsufficientPrivilege) as failure,
                    owner.transaction(),
                ):
                    owner.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(owned_schema))
                    )
                assert failure.value.sqlstate == "42501"

                resources._drop_owned_schemas(owner, identity, worker_oid, (owned_schema,))

                assert owner.execute("SELECT current_user").fetchone() == (provisioner,)
                assert owner.execute(
                    "SELECT count(*) FROM pg_namespace WHERE nspname=%s", (owned_schema,)
                ).fetchone() == (0,)
                assert owner.execute(
                    "SELECT count(*) FROM pg_namespace WHERE nspname=%s", (control_schema,)
                ).fetchone() == (1,)
                assert owner.execute(
                    "SELECT pg_has_role(%s,%s,'MEMBER')", (worker_role, provisioner)
                ).fetchone() == (False,)
                # Missing retained schemas are safe on retry; SET LOCAL again
                # returns to the provisioner before DROP ROLE.
                resources._drop_owned_schemas(owner, identity, worker_oid, (owned_schema,))
                assert owner.execute("SELECT current_user").fetchone() == (provisioner,)
                with pytest.raises(ValueError, match="ownership changed"):
                    resources._drop_owned_schemas(owner, identity, worker_oid, (control_schema,))
                assert owner.execute("SELECT current_user").fetchone() == (provisioner,)
                assert owner.execute(
                    "SELECT count(*) FROM pg_namespace WHERE nspname=%s", (control_schema,)
                ).fetchone() == (1,)
                owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(worker_role)))
        finally:
            for schema in (owned_schema, control_schema):
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(worker_role)))
            admin.execute(
                sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                    sql.Identifier(database), sql.Identifier(provisioner)
                )
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(provisioner)))


def test_cleanup_pending_retains_safe_stage_and_sqlstate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _connection(tmp_path)
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="fixture_project",
        neon_project_id="fixture-neon",
        source_common_directory="/fixture/.git",
        lease_id="a" * 32,
        role="aidashos_verify_" + "a" * 32,
        host=_HOST,
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2026-09-08T12:00:00+00:00",
    )

    def refused(_connection: resources._NeonConnection) -> psycopg.Connection:
        raise psycopg.errors.InsufficientPrivilege("postgresql://secret:must-not-leak@host/neondb")

    monkeypatch.setattr(resources._NeonConnection, "open", refused)
    outcome = resources._cleanup(identity, connection, tmp_path / "manifest.json")
    assert isinstance(outcome, resources.VerificationResourceCleanupPending)
    assert outcome.stage is resources.VerificationCleanupStage.VALIDATE_RESOURCE
    assert outcome.sqlstate == "42501"
    assert "must-not-leak" not in repr(outcome)

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The real local verifier can use only its leased role, even with known owner credentials."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from conftest import suite_postgres_url
from psycopg import sql
from work_unit_support import write_test_project_registry

from local_first_agent_os import verification_resources as resources
from local_first_agent_os.verification_postgres_protocol import LocalPostgresStartupGuard


def _packet(fields: list[tuple[bytes, bytes]], *, protocol: int = 3 << 16) -> bytes:
    body = struct.pack("!I", protocol) + b"".join(k + b"\0" + v + b"\0" for k, v in fields) + b"\0"
    return struct.pack("!I", len(body) + 4) + body


@pytest.fixture
def host_local_resource_owner() -> None:
    """Provisioner tests require the dedicated host endpoint, outside a leased gate."""

    url = suite_postgres_url()
    endpoint = urlsplit(url)
    if endpoint.hostname != "127.0.0.1" or endpoint.port != resources.LOCAL_PORT:
        pytest.skip("host provisioning canary requires the dedicated local test server")
    with psycopg.connect(url) as connection:
        authority = connection.execute(
            "SELECT rolcreaterole FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
    if authority != (True,):
        pytest.skip("host provisioning canary requires the test server provisioning role")


@pytest.fixture
def local_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    directory = tmp_path / "protected"
    directory.mkdir(mode=0o700)
    secret = directory / "database-url"
    secret.write_text("postgresql://postgres:postgres@127.0.0.1:5433/local_agent\n")
    secret.chmod(0o600)
    configuration = resources.LocalPostgresConfiguration(
        target_project_id="public_self",
        target_repository=str(repository),
        connection_file=str(secret),
    )
    path = directory / "verification.json"
    path.write_text(configuration.model_dump_json())
    path.chmod(0o600)
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    return configuration.target_project_id, repository


def test_real_psycopg_cannot_reauthenticate_as_the_known_superuser(
    host_local_resource_owner, local_configuration
) -> None:
    result = resources.acquire_verification_resources(*local_configuration)
    assert isinstance(result, resources.VerificationResourcesPresent), result
    lease = result.lease
    schema = "leased_" + lease.identity.lease_id
    try:
        assert isinstance(lease.identity, resources.LocalPostgresVerificationResourceIdentity)
        url = lease.environment()["LOCAL_AGENT_TEST_DATABASE_URL"]
        with psycopg.connect(url, autocommit=True) as connection:
            assert connection.execute("SELECT current_user").fetchone() == (lease.identity.role,)
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            for statement in (
                "SET ROLE postgres",
                "SET SESSION AUTHORIZATION postgres",
                "CREATE ROLE forbidden_role",
                "CREATE DATABASE forbidden_database",
                "CREATE TABLE public.forbidden_table (id int)",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement)
        # The runtime supplies a per-test schema as a startup option.
        with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
            assert connection.execute("SELECT current_schema()").fetchone() == (schema,)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(url, user="postgres", password="postgres", connect_timeout=2)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(url, dbname="postgres", connect_timeout=2)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(url, options="-c session_authorization=postgres", connect_timeout=2)
        # Every alternate startup shape is refused before the relay opens upstream.
        for protocol in (80877102, 80877103, 80877104, 2 << 16, (3 << 16) + 1):
            with socket.create_connection(
                ("127.0.0.1", lease.identity.relay_port), timeout=2
            ) as client:
                client.sendall(_packet([], protocol=protocol))
                with suppress(ConnectionResetError):
                    assert client.recv(1) == b""
    finally:
        assert isinstance(lease.close(), resources.VerificationResourceClosed)
    with psycopg.connect("postgresql://postgres:postgres@127.0.0.1:5433/local_agent") as owner:
        assert (
            owner.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (lease.identity.role,)
            ).fetchone()
            is None
        )
        assert (
            owner.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_user",
        "missing_user",
        "duplicate_database",
        "trailing_data",
        "unknown_key",
        "oversized",
        "wrong_user",
    ],
)
def test_startup_guard_refuses_ambiguous_or_widened_identity(mutation: str) -> None:
    role = "aidashos_verify_" + "a" * 32
    guard = LocalPostgresStartupGuard(role, "local_agent")
    fields = [(b"user", role.encode()), (b"database", b"local_agent")]
    if mutation == "duplicate_user":
        fields += [(b"user", b"postgres")]
    elif mutation == "missing_user":
        fields = fields[1:]
    elif mutation == "duplicate_database":
        fields += [(b"database", b"postgres")]
    elif mutation == "unknown_key":
        fields += [(b"replication", b"true")]
    elif mutation == "wrong_user":
        fields[0] = (b"user", b"postgres")
    packet = _packet(fields)
    if mutation == "trailing_data":
        packet += b"ignored"
    if mutation == "oversized":
        packet = _packet(fields + [(b"application_name", b"a" * 10000)])
    with pytest.raises(ValueError):
        guard.validate(packet)


def test_public_identity_serialization_contains_no_private_neon_binding(
    local_configuration,
) -> None:
    target_id, _ = local_configuration
    identity = resources.LocalPostgresVerificationResourceIdentity(
        target_project_id=target_id,
        lease_id="a" * 32,
        role="aidashos_verify_" + "a" * 32,
        source_common_directory="/fixture/.git",
        relay_port=12345,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    payload = json.loads(identity.model_dump_json())
    assert payload["target_project_id"] == target_id
    assert "neon_project_id" not in payload
    assert payload["transport"] == "pinned_role_loopback_tcp"


def test_actual_setup_script_is_repeatable_without_pulling_or_exposing_credentials(
    host_local_resource_owner,
    tmp_path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "public-checkout"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = tmp_path / "configs"
    write_test_project_registry(config, "public_self", repository)
    commands = tmp_path / "commands"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        '#!/bin/sh\n[ "$*" = "info" ] && exit 0\n'
        '[ "$*" = "compose up --pull never --no-build '
        '--wait --wait-timeout 120 -d postgres-test" ] || exit 97\n'
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "LOCAL_AGENT_CONFIG_DIR": str(config),
        "PYTHONPATH": str(root / "src"),
        "UV_PROJECT_ENVIRONMENT": str(Path(sys.executable).parent.parent),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    command = ["bash", str(root / "scripts/initialize-verification-resources.sh")]
    first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
    assert first.returncode == 0, first.stderr
    assert "created" in first.stdout
    assert "postgres:postgres" not in first.stdout + first.stderr
    directory = home / ".local-agent/verification"
    config_path = directory / "verification.json"
    secret_path = directory / "database-url"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert secret_path.stat().st_mode & 0o777 == 0o600
    before = (config_path.read_bytes(), secret_path.read_bytes())
    repeated = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
    assert repeated.returncode == 0, repeated.stderr
    assert "preserved" in repeated.stdout
    assert before == (config_path.read_bytes(), secret_path.read_bytes())

    legacy = directory / "neon-verification.json"
    legacy.write_text("retained legacy setup must not be superseded")
    legacy.chmod(0o600)
    refused_legacy = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
    assert refused_legacy.returncode != 0
    assert before == (config_path.read_bytes(), secret_path.read_bytes())
    assert legacy.read_text() == "retained legacy setup must not be superseded"
    checked = subprocess.run(
        [sys.executable, "-m", "local_first_agent_os.local_verification_setup", "check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert "ready" in checked.stdout
    refused = subprocess.run(
        [*command, "--target-project-id", "unregistered"],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert refused.returncode != 0
    assert before == (config_path.read_bytes(), secret_path.read_bytes())


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:postgres@127.0.0.1:5432/local_agent",
        "postgresql://postgres:postgres@localhost:5433/local_agent",
        "postgresql://postgres:postgres@127.0.0.1:5433/postgres",
        "postgresql://postgres:postgres@127.0.0.1:5433/local_agent?hostaddr=8.8.8.8",
    ],
)
def test_local_setup_has_no_production_or_remote_endpoint_fallback(url: str) -> None:
    with pytest.raises(ValueError):
        resources._local_connection_from_secret(url)


def test_protected_resource_fifo_is_refused_without_waiting_for_a_writer(tmp_path) -> None:
    fifo = tmp_path / "database-url"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(ValueError, match="mode 0600 files"):
        resources._read_protected(fifo)


def test_startup_read_cannot_wait_beyond_its_total_deadline() -> None:
    guard = LocalPostgresStartupGuard("aidashos_verify_" + "a" * 32, "local_agent")
    client, peer = socket.socketpair()
    try:
        peer.sendall(b"\x00")
        with pytest.raises(TimeoutError, match="total read deadline"):
            guard.read(client, deadline=0)
    finally:
        client.close()
        peer.close()


def test_retained_v1_neon_manifest_keeps_its_closed_cleanup_binding(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "protected"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(resources, "_resource_directory", lambda: directory)
    common = Path("/legacy/.git")
    monkeypatch.setattr(resources, "_git_common_directory", lambda _: common)
    secret = directory / "neon-database-url"
    secret.write_text("retained-secret")
    secret.chmod(0o600)
    configuration = resources.LegacyNeonConfiguration(
        schema_version="neon_verification_setup.v1",
        project_purpose="isolated_verification_only",
        expected_postgres_major=18,
        project_id="retained-fixture",
        target_repository="/legacy",
        connection_file=str(secret),
    )
    config_path = directory / "neon-verification.json"
    config_path.write_text(configuration.model_dump_json())
    config_path.chmod(0o600)
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="legacy_project",
        neon_project_id="retained-fixture",
        lease_id="c" * 32,
        role="aidashos_verify_" + "c" * 32,
        source_common_directory=str(common),
        host="ep-fixture.us-east-2.aws.neon.tech",
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    manifest = directory / f"lease-{identity.lease_id}.json"
    resources._write_manifest(
        manifest, identity, resources.VerificationResourceState.CLEANUP_PENDING
    )
    owner = resources._NeonConnection(
        identity.host, identity.hostaddr, "owner", "secret", Path("/ca")
    )
    monkeypatch.setattr(resources, "_connection_from_secret", lambda _: owner)
    cleaned = []

    def cleanup(actual, actual_owner, actual_manifest):
        cleaned.append((actual, actual_owner, actual_manifest))
        return resources.VerificationResourceClosed(actual.lease_id)

    monkeypatch.setattr(resources, "_cleanup", cleanup)
    assert isinstance(
        resources.cleanup_verification_resource_lease(identity.lease_id),
        resources.VerificationResourceClosed,
    )
    assert cleaned == [(identity, owner, manifest)]
    config_path.write_text(
        configuration.model_copy(update={"project_id": "other-fixture"}).model_dump_json()
    )
    assert isinstance(
        resources.cleanup_verification_resource_lease(identity.lease_id),
        resources.VerificationResourceCleanupPending,
    )
    assert len(cleaned) == 1


@dataclass(frozen=True)
class _LegacyMigrationFixture:
    repository: Path
    directory: Path
    environment: dict[str, str]
    manifest: Path

    def run(self, operation: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "local_first_agent_os.local_verification_setup", operation],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def snapshot(self) -> dict[str, tuple[bytes, int, int, int]]:
        return {
            path.name: (
                path.read_bytes(),
                path.stat().st_mode,
                path.stat().st_ino,
                path.stat().st_mtime_ns,
            )
            for path in self.directory.iterdir()
        }


@pytest.fixture
def legacy_migration(tmp_path: Path) -> _LegacyMigrationFixture:
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    directory = home / ".local-agent/verification"
    directory.mkdir(mode=0o700, parents=True)
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = tmp_path / "configs"
    write_test_project_registry(config, "retained_project", repository)
    secret = directory / "neon-database-url"
    # Migration must preserve this opaque credential without using it or contacting Neon.
    secret.write_text("retained-cleanup-credential\n")
    secret.chmod(0o600)
    metadata = resources.LegacyNeonConfiguration(
        schema_version="neon_verification_setup.v1",
        project_id="retained-fixture",
        project_purpose="isolated_verification_only",
        expected_postgres_major=18,
        connection_file=str(secret),
        target_repository=str(repository),
    )
    legacy = directory / "neon-verification.json"
    legacy.write_text(metadata.model_dump_json() + "\n")
    legacy.chmod(0o600)
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="retained_project",
        neon_project_id=metadata.project_id,
        lease_id="d" * 32,
        role="aidashos_verify_" + "d" * 32,
        source_common_directory=str(repository / ".git"),
        host="ep-fixture.us-east-2.aws.neon.tech",
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    manifest = directory / f"lease-{identity.lease_id}.json"
    resources._write_manifest(manifest, identity, resources.VerificationResourceState.CLOSED)
    environment = {
        **os.environ,
        "HOME": str(home),
        "LOCAL_AGENT_CONFIG_DIR": str(config),
        "PYTHONPATH": str(root / "src"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    return _LegacyMigrationFixture(repository, directory, environment, manifest)


@pytest.mark.parametrize("legacy_checkout", ["same", "linked_worktree"])
def test_explicit_legacy_migration_preserves_cleanup_files_and_is_repeatable(
    host_local_resource_owner, legacy_migration: _LegacyMigrationFixture, legacy_checkout: str
) -> None:
    fixture = legacy_migration
    if legacy_checkout == "linked_worktree":
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.repository),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "Fixture source",
            ],
            check=True,
        )
        linked = fixture.repository.parent / "linked-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.repository),
                "worktree",
                "add",
                "--detach",
                str(linked),
            ],
            check=True,
            capture_output=True,
        )
        legacy = fixture.directory / "neon-verification.json"
        metadata = json.loads(legacy.read_text())
        metadata["target_repository"] = str(linked)
        legacy.write_text(json.dumps(metadata))
    before = fixture.snapshot()
    refused = fixture.run("initialize")
    assert refused.returncode != 0
    assert fixture.snapshot() == before

    migrated = fixture.run("migrate-legacy-neon-to-local")
    assert migrated.returncode == 0, migrated.stderr
    assert "created" in migrated.stdout
    after = fixture.snapshot()
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) == {"verification.json", "database-url"}
    configuration = resources.LocalPostgresConfiguration.model_validate_json(
        (fixture.directory / "verification.json").read_bytes()
    )
    assert configuration.target_project_id == "retained_project"
    assert configuration.target_repository == str(fixture.repository)
    assert configuration.connection_file == str(fixture.directory / "database-url")
    for name in ("verification.json", "database-url"):
        assert (fixture.directory / name).stat().st_mode & 0o777 == 0o600

    for operation in ("migrate-legacy-neon-to-local", "initialize", "check"):
        repeated = fixture.run(operation)
        assert repeated.returncode == 0, repeated.stderr
        assert ("ready" if operation == "check" else "preserved") in repeated.stdout
        assert fixture.snapshot() == after
        assert "postgres:postgres" not in repeated.stdout + repeated.stderr
        assert "retained-cleanup-credential" not in repeated.stdout + repeated.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_repository",
        "wrong_lease_repository",
        "wrong_target",
        "wrong_neon_project",
        "provisioning",
        "ready",
        "cleanup_pending",
        "owned_schemas",
        "malformed_manifest",
        "wrong_manifest_name",
        "duplicate_setup_field",
        "wrong_secret_path",
    ],
)
def test_legacy_migration_refuses_unproven_binding_without_writes(
    legacy_migration: _LegacyMigrationFixture, tmp_path: Path, mutation: str
) -> None:
    fixture = legacy_migration
    legacy = fixture.directory / "neon-verification.json"
    metadata = json.loads(legacy.read_text())
    payload = json.loads(fixture.manifest.read_text())
    if mutation == "wrong_repository":
        other = tmp_path / "other-repository"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        metadata["target_repository"] = str(other)
    elif mutation == "wrong_lease_repository":
        payload["identity"]["source_common_directory"] = str(tmp_path / "other/.git")
    elif mutation == "wrong_target":
        payload["identity"]["target_project_id"] = "other_project"
    elif mutation == "wrong_neon_project":
        payload["identity"]["neon_project_id"] = "other-neon-project"
    elif mutation in {"provisioning", "ready", "cleanup_pending"}:
        payload["state"] = mutation
    elif mutation == "owned_schemas":
        payload["owned_schemas"] = ["not_proven_clean"]
    elif mutation == "wrong_secret_path":
        metadata["connection_file"] = str(tmp_path / "other-secret")
    legacy.write_text(json.dumps(metadata))
    fixture.manifest.write_text(json.dumps(payload))
    if mutation == "malformed_manifest":
        fixture.manifest.write_text("{invalid}")
    elif mutation == "wrong_manifest_name":
        fixture.manifest.rename(fixture.directory / ("lease-" + "a" * 32 + ".json"))
    elif mutation == "duplicate_setup_field":
        legacy.write_text(legacy.read_text()[:-1] + ', "project_id": "duplicate"}')
    before = fixture.snapshot()
    refused = fixture.run("migrate-legacy-neon-to-local")
    assert refused.returncode != 0
    assert "Local verification setup refused" in refused.stderr
    assert fixture.snapshot() == before

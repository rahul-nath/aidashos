# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Host-owned, temporary database resources for one registered verification gate.

The protected owner credential never enters the tested process. A leased login
can create its own test schemas in the dedicated verification database, but has
no database/role creation authority, owner membership, or public-schema writes.
Local protected manifests make interrupted cleanup discoverable; they are not
an implementation of the durable execution event bus.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import select
import socket
import stat
import subprocess
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Annotated, ClassVar, Literal, TypedDict
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import certifi
import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .verification_postgres_protocol import (
    LocalPostgresStartupGuard,
    OpaquePostgresTls,
    PostgresStartupPolicy,
)

_NEON_DATABASE = "neondb"
_NEON_PORT = 5432
_NEON_POSTGRES_MAJOR = 18
LOCAL_DATABASE = "local_agent"
LOCAL_PORT = 5433
LOCAL_POSTGRES_MAJOR = 16
_LEASE_SECONDS = 7200
_CONNECTION_LIMIT = 32
_LOOPBACK_ADDRESS = "127.0.0.1"
_RELAY_POLL_SECONDS = 0.2
_RELAY_CONNECT_SECONDS = 5.0
_RELAY_CLOSE_SECONDS = 6.0
_RELAY_BUFFER_BYTES = 65536
_TERMINATE_SESSION_TIMEOUT_MS = 1000
_MAX_CONFIG_BYTES = 16384
_DIRECT_NEON_HOST = re.compile(r"ep-[a-z0-9-]+(?:\.[a-z0-9-]+)+\.neon\.tech")
_ROLE_PREFIX = "aidashos_verify_"
_URL_PATTERN = re.compile(r"postgres(?:ql)?://[^\s\"'<>]+", re.IGNORECASE)


class VerificationResourceState(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    CLEANUP_PENDING = "cleanup_pending"
    CLOSED = "closed"


class VerificationSetupDisposition(StrEnum):
    CREATED = "created"
    PRESERVED = "preserved"


class VerificationSetupOperation(StrEnum):
    INITIALIZE = "initialize"
    MIGRATE_LEGACY_NEON_TO_LOCAL = "migrate-legacy-neon-to-local"


class VerificationResourceFailure(StrEnum):
    CONFIGURATION_REFUSED = "VERIFICATION_RESOURCE_CONFIGURATION_REFUSED"
    ENDPOINT_UNAVAILABLE = "VERIFICATION_RESOURCE_ENDPOINT_UNAVAILABLE"
    AUTHORITY_REFUSED = "VERIFICATION_RESOURCE_AUTHORITY_REFUSED"
    PROVISIONING_FAILED = "VERIFICATION_RESOURCE_PROVISIONING_FAILED"
    CLEANUP_PENDING = "VERIFICATION_RESOURCE_CLEANUP_PENDING"


class LegacyNeonConfiguration(BaseModel):
    """Retained setup can clean existing leases, but cannot authorize a new target."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    schema_version: Literal["neon_verification_setup.v1"]
    project_id: str = Field(min_length=1)
    project_purpose: Literal["isolated_verification_only"]
    expected_postgres_major: Literal[18]
    connection_file: str
    target_repository: str


class _ConfigurationBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    target_project_id: str = Field(min_length=1)
    target_repository: str = Field(min_length=1)
    connection_file: str = Field(min_length=1)
    project_purpose: Literal["isolated_verification_only"] = "isolated_verification_only"


class NeonConfiguration(_ConfigurationBinding):
    schema_version: Literal["neon_verification_setup.v2"] = "neon_verification_setup.v2"
    project_id: str = Field(min_length=1)
    expected_postgres_major: Literal[18] = 18


class LocalPostgresConfiguration(_ConfigurationBinding):
    schema_version: Literal["local_postgres_verification_setup.v1"] = (
        "local_postgres_verification_setup.v1"
    )
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[5433] = 5433
    database: Literal["local_agent"] = "local_agent"
    expected_postgres_major: Literal[16] = 16


type ProtectedConfiguration = Annotated[
    NeonConfiguration | LocalPostgresConfiguration, Field(discriminator="schema_version")
]
_CONFIGURATION = TypeAdapter(ProtectedConfiguration)


class _Identity(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    target_project_id: str = Field(min_length=1)
    lease_id: str
    role: str
    source_common_directory: str
    relay_hostaddr: Literal["127.0.0.1"] = "127.0.0.1"
    relay_port: int
    expires_at: str
    authority: Literal["connect_create_own_schemas"] = "connect_create_own_schemas"

    @model_validator(mode="after")
    def _validate_identity(self) -> _Identity:
        if (
            re.fullmatch(r"[0-9a-f]{32}", self.lease_id) is None
            or self.role != f"{_ROLE_PREFIX}{self.lease_id}"
            or not Path(self.source_common_directory).is_absolute()
            or not 1 <= self.relay_port <= 65535
            or datetime.fromisoformat(self.expires_at).tzinfo is None
        ):
            raise ValueError("verification lease identity violates its declared resource contract")
        return self


class NeonVerificationResourceIdentity(_Identity):
    """The closed v1 receipt shape remains readable for retained Neon cleanup."""

    schema_version: Literal["verification_resource_lease.v1"] = "verification_resource_lease.v1"
    neon_project_id: str = Field(min_length=1)
    database: Literal["neondb"] = "neondb"
    postgres_major: Literal[18] = 18
    host: str
    hostaddr: str
    port: Literal[5432] = 5432
    sslmode: Literal["verify-full"] = "verify-full"
    transport: Literal["pinned_loopback_tcp"] = "pinned_loopback_tcp"

    @model_validator(mode="after")
    def _validate_endpoint(self) -> NeonVerificationResourceIdentity:
        if (
            _DIRECT_NEON_HOST.fullmatch(self.host) is None
            or "-pooler." in self.host
            or not ipaddress.IPv4Address(self.hostaddr).is_global
        ):
            raise ValueError("Neon verification requires a direct, public IPv4 TLS endpoint")
        return self


class LocalPostgresVerificationResourceIdentity(_Identity):
    schema_version: Literal["local_postgres_verification_lease.v1"] = (
        "local_postgres_verification_lease.v1"
    )
    database: Literal["local_agent"] = "local_agent"
    postgres_major: Literal[16] = 16
    host: Literal["127.0.0.1"] = "127.0.0.1"
    hostaddr: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[5433] = 5433
    sslmode: Literal["disable"] = "disable"
    transport: Literal["pinned_role_loopback_tcp"] = "pinned_role_loopback_tcp"


type VerificationResourceIdentity = Annotated[
    NeonVerificationResourceIdentity | LocalPostgresVerificationResourceIdentity,
    Field(discriminator="schema_version"),
]


class _IdentityFields(TypedDict):
    target_project_id: str
    lease_id: str
    role: str
    source_common_directory: str
    relay_port: int
    expires_at: str


class _LeaseManifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    identity: VerificationResourceIdentity
    state: VerificationResourceState
    owned_schemas: tuple[str, ...]


@dataclass(frozen=True)
class VerificationResourcesAbsent:
    """This project has no declared external verification resource."""


@dataclass(frozen=True)
class VerificationResourcesRefused:
    code: VerificationResourceFailure
    reason: str


@dataclass(frozen=True)
class VerificationResourceClosed:
    lease_id: str


class VerificationCleanupStage(StrEnum):
    VALIDATE_RESOURCE = "validate_resource"
    REVOKE_LOGIN = "revoke_login"
    TERMINATE_SESSIONS = "terminate_sessions"
    OWNED_SCHEMA_CLEANUP = "owned_schema_cleanup"
    REVOKE_DATABASE_PRIVILEGES = "revoke_database_privileges"
    DROP_ROLE = "drop_role"
    WRITE_CLOSED_MANIFEST = "write_closed_manifest"


@dataclass(frozen=True)
class VerificationResourceCleanupPending:
    lease_id: str
    code: VerificationResourceFailure = VerificationResourceFailure.CLEANUP_PENDING
    reason: str = "temporary verification resource requires protected-manifest cleanup"
    stage: VerificationCleanupStage | None = None
    sqlstate: str | None = None

    def __post_init__(self) -> None:
        if self.stage is not None and not isinstance(self.stage, VerificationCleanupStage):
            raise TypeError("verification cleanup stage must be a declared variant")
        if self.sqlstate is not None and re.fullmatch(r"[A-Z0-9]{5}", self.sqlstate) is None:
            raise ValueError("verification cleanup SQLSTATE must be a five-character server code")


type VerificationResourceCleanup = VerificationResourceClosed | VerificationResourceCleanupPending


@dataclass(frozen=True)
class _NeonConnection:
    host: str
    hostaddr: str
    username: str
    password: str = field(repr=False)
    ca_file: Path
    port: ClassVar[int] = _NEON_PORT
    database: ClassVar[str] = _NEON_DATABASE
    postgres_major: ClassVar[int] = _NEON_POSTGRES_MAJOR

    def url(self, *, hostaddr: str | None = None, port: int | None = None) -> str:
        query = urlencode(
            {
                "hostaddr": self.hostaddr if hostaddr is None else hostaddr,
                "sslmode": "verify-full",
                "sslrootcert": str(self.ca_file),
                "channel_binding": "require",
                "connect_timeout": "10",
            }
        )
        return urlunsplit(
            (
                "postgresql",
                f"{quote(self.username, safe='')}:{quote(self.password, safe='')}"
                f"@{self.host}:{self.port if port is None else port}",
                f"/{self.database}",
                query,
                "",
            )
        )

    def open(self) -> psycopg.Connection:
        return psycopg.connect(self.url(), autocommit=True)


@dataclass(frozen=True)
class _LocalPostgresConnection:
    username: str
    password: str = field(repr=False)
    host: ClassVar[str] = _LOOPBACK_ADDRESS
    hostaddr: ClassVar[str] = _LOOPBACK_ADDRESS
    port: ClassVar[int] = LOCAL_PORT
    database: ClassVar[str] = LOCAL_DATABASE
    postgres_major: ClassVar[int] = LOCAL_POSTGRES_MAJOR

    def url(self, *, hostaddr: str | None = None, port: int | None = None) -> str:
        if hostaddr not in {None, self.hostaddr}:
            raise ValueError("local verification transport cannot leave loopback")
        return urlunsplit(
            (
                "postgresql",
                f"{quote(self.username, safe='')}:{quote(self.password, safe='')}"
                f"@{self.host}:{self.port if port is None else port}",
                f"/{self.database}",
                urlencode({"sslmode": "disable", "gssencmode": "disable", "connect_timeout": "10"}),
                "",
            )
        )

    def open(self) -> psycopg.Connection:
        return psycopg.connect(self.url(), autocommit=True)


type _Connection = _NeonConnection | _LocalPostgresConnection


class _PinnedLoopbackRelay:
    """Forward to one host-selected address under its declared PostgreSQL protocol.

    Local startup selects only the leased role and dedicated test database before
    opening upstream. Remote TLS remains opaque and authenticated end to end.
    Neither variant permits client-selected destination addresses or ports.
    """

    def __init__(self, destination: tuple[str, int], *, startup: PostgresStartupPolicy) -> None:
        if not isinstance(startup, (OpaquePostgresTls, LocalPostgresStartupGuard)):
            raise TypeError("verification relay requires a declared startup policy")
        address, port = destination
        ipaddress.IPv4Address(address)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("verification relay requires one concrete destination")
        self._destination = destination
        self._startup = startup
        self._deadline = monotonic() + _LEASE_SECONDS
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(_CONNECTION_LIMIT)
        self._sockets: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.bind((_LOOPBACK_ADDRESS, 0))
            self._listener.listen(_CONNECTION_LIMIT)
            self._listener.settimeout(_RELAY_POLL_SECONDS)
            self.port = int(self._listener.getsockname()[1])
            self._acceptor = threading.Thread(
                target=self._accept, name="verification-pinned-relay", daemon=True
            )
            self._acceptor.start()
        except BaseException:
            self._listener.close()
            raise

    def _register(self, connection: socket.socket) -> bool:
        with self._lock:
            if self._stopped.is_set():
                connection.close()
                return False
            self._sockets.add(connection)
            return True

    def _accept(self) -> None:
        while not self._stopped.is_set() and monotonic() < self._deadline:
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._capacity.acquire(blocking=False):
                client.close()
                continue
            if not self._register(client):
                self._capacity.release()
                return
            worker = threading.Thread(target=self._forward, args=(client,), daemon=True)
            with self._lock:
                self._workers.add(worker)
                worker.start()
        self._listener.close()

    def _forward(self, client: socket.socket) -> None:
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(_RELAY_CONNECT_SECONDS)
            startup = (
                self._startup.read(
                    client, deadline=min(self._deadline, monotonic() + _RELAY_CONNECT_SECONDS)
                )
                if isinstance(self._startup, LocalPostgresStartupGuard)
                else b""
            )
            if not self._register(upstream):
                return
            upstream.settimeout(_RELAY_CONNECT_SECONDS)
            upstream.connect(self._destination)
            client.setblocking(False)
            upstream.setblocking(False)
            peers = {client: upstream, upstream: client}
            pending = {client: bytearray(), upstream: bytearray(startup)}
            reading = {client, upstream}
            writing = {client, upstream}
            while not self._stopped.is_set() and monotonic() < self._deadline:
                for source, destination in peers.items():
                    if (
                        source not in reading
                        and not pending[destination]
                        and destination in writing
                    ):
                        destination.shutdown(socket.SHUT_WR)
                        writing.remove(destination)
                if not reading and not any(pending.values()):
                    return
                readable, writable, _ = select.select(
                    [
                        source
                        for source in reading
                        if len(pending[peers[source]]) < _RELAY_BUFFER_BYTES
                    ],
                    [destination for destination in writing if pending[destination]],
                    [],
                    _RELAY_POLL_SECONDS,
                )
                for source in readable:
                    destination = peers[source]
                    try:
                        data = source.recv(_RELAY_BUFFER_BYTES - len(pending[destination]))
                    except BlockingIOError:
                        continue
                    if data:
                        pending[destination].extend(data)
                    else:
                        reading.remove(source)
                for destination in writable:
                    try:
                        written = destination.send(pending[destination])
                    except BlockingIOError:
                        continue
                    if written == 0:
                        return
                    del pending[destination][:written]
        except (OSError, ValueError):
            # Transport failure becomes the child's ordinary connection error;
            # forwarding never invents a database outcome or logs protocol data.
            return
        finally:
            client.close()
            upstream.close()
            with self._lock:
                self._sockets.discard(client)
                self._sockets.discard(upstream)
                self._workers.discard(threading.current_thread())
            self._capacity.release()

    def close(self) -> bool:
        self._stopped.set()
        self._listener.close()
        deadline = monotonic() + _RELAY_CLOSE_SECONDS
        self._acceptor.join(timeout=max(0.0, deadline - monotonic()))
        with self._lock:
            connections = tuple(self._sockets)
            workers = tuple(self._workers)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - monotonic()))
        return not any(worker.is_alive() for worker in (self._acceptor, *workers))


@dataclass(frozen=True)
class VerificationResourceLease:
    identity: VerificationResourceIdentity
    _owner: _Connection = field(repr=False)
    _worker: _Connection = field(repr=False)
    _manifest: Path = field(repr=False)
    _relay: _PinnedLoopbackRelay = field(repr=False)

    @property
    def readable_paths(self) -> tuple[Path, ...]:
        return (self._worker.ca_file,) if isinstance(self._worker, _NeonConnection) else ()

    def environment(self) -> dict[str, str]:
        return {
            "LOCAL_AGENT_TEST_DATABASE_URL": self._worker.url(
                hostaddr=self.identity.relay_hostaddr, port=self.identity.relay_port
            )
        }

    def sandbox_rules(self) -> tuple[str, ...]:
        endpoint = f"localhost:{self.identity.relay_port}"
        return (f"(allow network-outbound (remote tcp {json.dumps(endpoint)}))",)

    def redact(self, text: str) -> str:
        return _redact(text, (self._owner, self._worker))

    def close(self) -> VerificationResourceCleanup:
        relay_closed = self._relay.close()
        result = _cleanup(self.identity, self._owner, self._manifest)
        if not relay_closed:
            _write_manifest(
                self._manifest, self.identity, VerificationResourceState.CLEANUP_PENDING
            )
            return VerificationResourceCleanupPending(self.identity.lease_id)
        return result


@dataclass(frozen=True)
class VerificationResourcesPresent:
    lease: VerificationResourceLease


type VerificationResources = (
    VerificationResourcesAbsent | VerificationResourcesPresent | VerificationResourcesRefused
)


class _ResourceRefusal(ValueError):
    def __init__(self, code: VerificationResourceFailure, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


def _resource_directory() -> Path:
    return Path.home() / ".local-agent" / "verification"


def _assert_protected_directory(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise _ResourceRefusal(
                VerificationResourceFailure.CONFIGURATION_REFUSED,
                "verification resource path contains a symlink",
            )
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification resource directory must be owned by the host user with mode 0700",
        )


def _read_protected(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > _MAX_CONFIG_BYTES
        ):
            raise _ResourceRefusal(
                VerificationResourceFailure.CONFIGURATION_REFUSED,
                "verification configuration and credentials require host-owned mode 0600 files",
            )
        content = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
        if len(content) > _MAX_CONFIG_BYTES:
            raise ValueError("verification resource file exceeds its size contract")
        return content
    finally:
        os.close(descriptor)


def _read_protected_json(path: Path) -> bytes:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("verification resource JSON contains duplicate fields")
            result[key] = value
        return result

    content = _read_protected(path)
    if not isinstance(json.loads(content, object_pairs_hook=unique_object), dict):
        raise ValueError("verification resource JSON must be an object")
    return content


def _resolve_host(host: str) -> str:
    addresses = {
        ipaddress.IPv4Address(row[4][0])
        for row in socket.getaddrinfo(host, _NEON_PORT, socket.AF_INET, socket.SOCK_STREAM)
    }
    if not addresses or any(not address.is_global for address in addresses):
        raise _ResourceRefusal(
            VerificationResourceFailure.ENDPOINT_UNAVAILABLE,
            "direct verification endpoint did not resolve exclusively to public IPv4 addresses",
        )
    return str(sorted(addresses)[0])


def _connection_from_secret(secret: str) -> _NeonConnection:
    parsed = urlsplit(secret.strip())
    host = parsed.hostname or ""
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or _DIRECT_NEON_HOST.fullmatch(host) is None
        or "-pooler." in host
        or parsed.port not in {None, _NEON_PORT}
        or parsed.path != f"/{_NEON_DATABASE}"
        or parsed.fragment
        or not parsed.username
        or not parsed.password
    ):
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification credential must name the direct Neon endpoint and dedicated neondb",
        )
    query = parse_qsl(parsed.query, strict_parsing=True)
    if len({name for name, _ in query}) != len(query) or any(
        name not in {"sslmode", "sslrootcert", "channel_binding"} for name, _ in query
    ):
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification credential contains undeclared connection options",
        )
    ca_file = Path(certifi.where()).resolve(strict=True)
    return _NeonConnection(
        host, _resolve_host(host), unquote(parsed.username), unquote(parsed.password), ca_file
    )


def _local_connection_from_secret(secret: str) -> _LocalPostgresConnection:
    parsed = urlsplit(secret.strip())
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != _LOOPBACK_ADDRESS
        or parsed.port != LOCAL_PORT
        or parsed.path != f"/{LOCAL_DATABASE}"
        or parsed.fragment
        or not parsed.username
        or not parsed.password
        or parsed.query
    ):
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "local verification credential must name the dedicated test server "
            "127.0.0.1:5433/local_agent without additional connection options",
        )
    return _LocalPostgresConnection(unquote(parsed.username), unquote(parsed.password))


def _git_common_directory(repository: Path) -> Path:
    if not repository.is_absolute():
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification repository identity must be absolute",
        )
    result = subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            str(repository),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    if result.returncode != 0:
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification repository has no trusted Git common-directory identity",
        )
    return Path(result.stdout.strip()).resolve(strict=True)


def _load_configuration() -> ProtectedConfiguration | None:
    directory = _resource_directory()
    if not directory.exists() and not directory.is_symlink():
        return None
    _assert_protected_directory(directory)
    path = directory / "verification.json"
    if not path.exists() and not path.is_symlink():
        return None
    return _CONFIGURATION.validate_json(_read_protected_json(path))


def _load_owner(
    metadata: ProtectedConfiguration,
    *,
    source_repository: Path | None = None,
) -> tuple[_Connection, Path, Path]:
    directory = _resource_directory()
    _assert_protected_directory(directory)
    common_directory = _git_common_directory(Path(metadata.target_repository))
    if source_repository is not None and (
        _git_common_directory(source_repository) != common_directory
    ):
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification repository differs from the protected resource owner",
        )
    connection_file = directory / "database-url"
    if metadata.connection_file != str(connection_file):
        raise _ResourceRefusal(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "verification configuration names an undeclared credential file",
        )
    secret = _read_protected(connection_file).decode()
    connection = (
        _local_connection_from_secret(secret)
        if isinstance(metadata, LocalPostgresConfiguration)
        else _connection_from_secret(secret)
    )
    return connection, directory, common_directory


def _load_legacy_owner(
    identity: NeonVerificationResourceIdentity,
) -> tuple[_NeonConnection, Path, Path]:
    directory = _resource_directory()
    _assert_protected_directory(directory)
    metadata = LegacyNeonConfiguration.model_validate_json(
        _read_protected_json(directory / "neon-verification.json")
    )
    common = _git_common_directory(Path(metadata.target_repository))
    secret_file = directory / "neon-database-url"
    if (
        metadata.project_id != identity.neon_project_id
        or str(common) != identity.source_common_directory
        or metadata.connection_file != str(secret_file)
    ):
        raise ValueError("retained Neon lease differs from its protected setup")
    return _connection_from_secret(_read_protected(secret_file).decode()), directory, common


def _validate_closed_legacy_setup(
    directory: Path,
    configuration: LocalPostgresConfiguration,
    common_directory: Path,
    existing: ProtectedConfiguration | None,
) -> None:
    """Retained receipts authorize migration only after their legacy authority is closed.

    The operator must quiesce all resource owners before this read/write sequence.
    This check neither contacts Neon nor substitutes for a live cleanup receipt.
    """

    _assert_protected_directory(directory)
    metadata = LegacyNeonConfiguration.model_validate_json(
        _read_protected_json(directory / "neon-verification.json")
    )
    secret_file = directory / "neon-database-url"
    if _git_common_directory(
        Path(metadata.target_repository)
    ) != common_directory or metadata.connection_file != str(secret_file):
        raise ValueError("retained Neon setup differs from the requested repository binding")
    # Keep the opaque cleanup credential and its existing protected-file contract intact.
    _read_protected(secret_file)
    for path in sorted(directory.glob("lease-*.json")):
        payload = _LeaseManifest.model_validate_json(_read_protected_json(path))
        identity = payload.identity
        if (
            path.name != f"lease-{identity.lease_id}.json"
            or identity.target_project_id != configuration.target_project_id
            or identity.source_common_directory != str(common_directory)
        ):
            raise ValueError("retained lease differs from the requested repository binding")
        if isinstance(identity, NeonVerificationResourceIdentity):
            if (
                identity.neon_project_id != metadata.project_id
                or payload.state is not VerificationResourceState.CLOSED
                or payload.owned_schemas
            ):
                raise ValueError("retained Neon lease has no matching closed cleanup receipt")
        elif not isinstance(existing, LocalPostgresConfiguration):
            raise ValueError("local lease exists without an established local binding")


def initialize_local_verification_resources(
    target_project_id: str,
    source_repository: Path,
    owner_secret: str,
    *,
    operation: VerificationSetupOperation = VerificationSetupOperation.INITIALIZE,
) -> VerificationSetupDisposition:
    """Pin the existing test server; only explicit migration can supersede closed legacy setup.

    Migration requires quiescent resource authority and retains every legacy file.
    An established matching local binding is preserved by either operation.
    """

    owner = _local_connection_from_secret(owner_secret)
    common_directory = _git_common_directory(source_repository)
    directory = _resource_directory()
    secret_file = directory / "database-url"
    configuration = LocalPostgresConfiguration(
        target_project_id=target_project_id,
        target_repository=str(source_repository),
        connection_file=str(secret_file),
    )
    existing = _load_configuration()
    if existing is not None and existing != configuration:
        raise ValueError("existing verification setup names a different protected binding")
    legacy_file = directory / "neon-verification.json"
    has_legacy_setup = legacy_file.exists() or legacy_file.is_symlink()
    match operation:
        case VerificationSetupOperation.INITIALIZE:
            if has_legacy_setup and existing is None:
                raise ValueError(
                    "retained Neon setup requires an explicit protected binding migration"
                )
        case VerificationSetupOperation.MIGRATE_LEGACY_NEON_TO_LOCAL:
            if not has_legacy_setup:
                raise ValueError("explicit Neon migration requires retained protected v1 setup")
        case _:
            raise ValueError("unknown verification setup operation")
    if has_legacy_setup:
        _validate_closed_legacy_setup(directory, configuration, common_directory, existing)
    with owner.open() as connection:
        _verify_owner(connection, owner)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_protected_directory(directory)
    normalized_secret = owner_secret.strip().encode() + b"\n"
    try:
        descriptor = os.open(
            secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        if _read_protected(secret_file).strip() != normalized_secret.strip():
            raise ValueError(
                "existing verification credential differs; replacement must be explicit"
            ) from None
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(normalized_secret)
            stream.flush()
            os.fsync(stream.fileno())
    path = directory / "verification.json"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _load_configuration() != configuration:
            raise ValueError("concurrent verification setup differs from this binding") from None
        return VerificationSetupDisposition.PRESERVED
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(configuration.model_dump_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return VerificationSetupDisposition.CREATED


def check_configured_verification_resources(
    target_project_id: str, source_repository: Path
) -> None:
    """Read the exact protected binding and database authority without allocating a lease."""

    configuration = _load_configuration()
    if configuration is None or configuration.target_project_id != target_project_id:
        raise ValueError("registered project has no protected verification binding")
    owner, _, _ = _load_owner(configuration, source_repository=source_repository)
    with owner.open() as connection:
        _verify_owner(connection, owner)


def _manifest_payload(
    identity: VerificationResourceIdentity,
    state: VerificationResourceState,
    schemas: tuple[str, ...],
) -> bytes:
    return json.dumps(
        {"identity": identity.model_dump(), "state": state, "owned_schemas": schemas},
        sort_keys=True,
    ).encode()


def _write_manifest(
    path: Path,
    identity: VerificationResourceIdentity,
    state: VerificationResourceState,
    schemas: tuple[str, ...] = (),
) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        data = _manifest_payload(identity, state, schemas)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("verification lease manifest cannot be a symlink")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _redact(text: str, connections: tuple[_Connection, ...]) -> str:
    result = _URL_PATTERN.sub("[REDACTED_VERIFICATION_DATABASE_URL]", text)
    for connection in connections:
        for secret in (connection.url(), connection.password, quote(connection.password, safe="")):
            if secret:
                result = result.replace(secret, "[REDACTED_VERIFICATION_CREDENTIAL]")
    return result


def _verify_owner(connection: psycopg.Connection, owner: _Connection) -> None:
    row = connection.execute(
        "SELECT current_database(), current_setting('server_version_num')::int, "
        "rolcreaterole FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if (
        row is None
        or row[0] != owner.database
        or row[1] // 10000 != owner.postgres_major
        or not row[2]
    ):
        raise _ResourceRefusal(
            VerificationResourceFailure.AUTHORITY_REFUSED,
            "verification resource requires its declared dedicated PostgreSQL provisioning role",
        )


def _verify_worker(worker: _Connection, owner_role: str) -> None:
    """Refuse application write authority outside owned schemas in the declared database.

    PUBLIC may still grant CONNECT, TEMP, or catalog visibility. Those privileges
    are distinct from creating persistent application state and are not revoked
    as a side effect of provisioning this resource.
    """

    with worker.open() as connection:
        row = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
            "pg_has_role(current_user, %s, 'MEMBER'), "
            "has_schema_privilege(current_user, 'public', 'CREATE'), "
            "has_database_privilege(current_user, current_database(), 'CREATE'), "
            "(SELECT count(*) FROM pg_auth_members WHERE member=r.oid), "
            "(SELECT count(*) FROM pg_database d WHERE d.datname<>current_database() "
            "AND has_database_privilege(current_user, d.oid, 'CREATE')), "
            "(SELECT count(*) FROM pg_namespace n WHERE n.nspowner<>r.oid "
            "AND has_schema_privilege(current_user, n.oid, 'CREATE')) "
            "FROM pg_roles r WHERE rolname=current_user",
            (owner_role,),
        ).fetchone()
        if row is None or any(row[:7]) or not row[7] or any(row[8:]):
            raise _ResourceRefusal(
                VerificationResourceFailure.AUTHORITY_REFUSED,
                "temporary verification role exceeded its own-schema authority",
            )


def _terminate_role_connections(
    connection: psycopg.Connection, identity: VerificationResourceIdentity
) -> None:
    # PUBLIC CONNECT/TEMP can exist on sibling databases. Closing every session
    # of this unique leased role also removes its session-local temporary state.
    terminated = connection.execute(
        "SELECT pg_terminate_backend(pid, %s) FROM pg_stat_activity "
        "WHERE usename=%s AND pid<>pg_backend_pid()",
        (_TERMINATE_SESSION_TIMEOUT_MS, identity.role),
    ).fetchall()
    if any(not row[0] for row in terminated):
        raise ValueError("temporary verification role session did not terminate")


def _drop_owned_schemas(
    connection: psycopg.Connection,
    identity: VerificationResourceIdentity,
    role_oid: int,
    schemas: tuple[str, ...],
) -> None:
    if not schemas:
        return
    # CREATEROLE's ADMIN membership does not imply SET or inherited ownership.
    # Only the provisioner gains access to this disposable role; the worker
    # receives no membership in the provisioner or any other privileged role.
    connection.execute(
        sql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT FALSE, SET TRUE").format(
            sql.Identifier(identity.role)
        )
    )
    with connection.transaction():
        connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(identity.role)))
        for schema in schemas:
            observed = connection.execute(
                "SELECT nspowner FROM pg_namespace WHERE nspname=%s", (schema,)
            ).fetchone()
            if observed is None:
                continue
            if observed[0] != role_oid:
                raise ValueError("verification cleanup schema ownership changed")
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    # SET LOCAL is restored on commit and rollback. Database grant revocation
    # and DROP ROLE must run as the original provisioner, never the leased role.


def _cleanup(
    identity: VerificationResourceIdentity, owner: _Connection, manifest: Path
) -> VerificationResourceCleanup:
    stage = VerificationCleanupStage.VALIDATE_RESOURCE
    try:
        with owner.open() as connection:
            _verify_owner(connection, owner)
            role = connection.execute(
                "SELECT oid FROM pg_roles WHERE rolname=%s", (identity.role,)
            ).fetchone()
            if role is not None:
                stage = VerificationCleanupStage.REVOKE_LOGIN
                connection.execute(
                    sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(identity.role))
                )
                stage = VerificationCleanupStage.TERMINATE_SESSIONS
                _terminate_role_connections(connection, identity)
                schemas = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT nspname FROM pg_namespace WHERE nspowner=%s ORDER BY nspname",
                        (role[0],),
                    ).fetchall()
                )
                _write_manifest(
                    manifest, identity, VerificationResourceState.CLEANUP_PENDING, schemas
                )
                stage = VerificationCleanupStage.OWNED_SCHEMA_CLEANUP
                _drop_owned_schemas(connection, identity, role[0], schemas)
                stage = VerificationCleanupStage.REVOKE_DATABASE_PRIVILEGES
                connection.execute(
                    sql.SQL("REVOKE CONNECT, CREATE ON DATABASE {} FROM {}").format(
                        sql.Identifier(identity.database), sql.Identifier(identity.role)
                    )
                )
                stage = VerificationCleanupStage.DROP_ROLE
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(identity.role)))
        stage = VerificationCleanupStage.WRITE_CLOSED_MANIFEST
        _write_manifest(manifest, identity, VerificationResourceState.CLOSED)
        return VerificationResourceClosed(identity.lease_id)
    except Exception as exc:
        sqlstate = exc.sqlstate if isinstance(exc, psycopg.Error) else None
        return VerificationResourceCleanupPending(identity.lease_id, stage=stage, sqlstate=sqlstate)


def acquire_verification_resources(
    target_project_id: str, source_repository: Path
) -> VerificationResources:
    """Provision only the host-declared resource, never an endpoint from task data."""

    identity: VerificationResourceIdentity | None = None
    owner: _Connection | None = None
    manifest: Path | None = None
    relay: _PinnedLoopbackRelay | None = None
    try:
        configuration = _load_configuration()
        if configuration is None:
            legacy_file = _resource_directory() / "neon-verification.json"
            if legacy_file.exists():
                legacy = LegacyNeonConfiguration.model_validate_json(
                    _read_protected_json(legacy_file)
                )
                if _git_common_directory(Path(legacy.target_repository)) == _git_common_directory(
                    source_repository
                ):
                    raise _ResourceRefusal(
                        VerificationResourceFailure.CONFIGURATION_REFUSED,
                        "legacy Neon setup needs an explicit registered target binding "
                        "before new verification",
                    )
            return VerificationResourcesAbsent()
        if configuration.target_project_id != target_project_id:
            return VerificationResourcesAbsent()
        owner, directory, common_directory = _load_owner(
            configuration, source_repository=source_repository
        )
        lease_id = uuid.uuid4().hex
        role = f"{_ROLE_PREFIX}{lease_id}"
        startup = (
            LocalPostgresStartupGuard(role, owner.database)
            if isinstance(owner, _LocalPostgresConnection)
            else OpaquePostgresTls()
        )
        relay = _PinnedLoopbackRelay((owner.hostaddr, owner.port), startup=startup)
        identity_fields: _IdentityFields = {
            "target_project_id": target_project_id,
            "lease_id": lease_id,
            "role": role,
            "source_common_directory": str(common_directory),
            "relay_port": relay.port,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS)).isoformat(),
        }
        identity = (
            LocalPostgresVerificationResourceIdentity(**identity_fields)
            if isinstance(configuration, LocalPostgresConfiguration)
            else NeonVerificationResourceIdentity(
                **identity_fields,
                neon_project_id=configuration.project_id,
                host=owner.host,
                hostaddr=owner.hostaddr,
            )
        )
        manifest = directory / f"lease-{lease_id}.json"
        _write_manifest(manifest, identity, VerificationResourceState.PROVISIONING)
        password = secrets.token_urlsafe(32)
        with owner.open() as connection:
            _verify_owner(connection, owner)
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {} "
                    "PASSWORD {} VALID UNTIL {}"
                ).format(
                    sql.Identifier(identity.role),
                    sql.Literal(_CONNECTION_LIMIT),
                    sql.Literal(password),
                    sql.Literal(identity.expires_at),
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(owner.database), sql.Identifier(identity.role)
                )
            )
        worker = replace(owner, username=identity.role, password=password)
        _verify_worker(worker, owner.username)
        _write_manifest(manifest, identity, VerificationResourceState.READY)
        return VerificationResourcesPresent(
            VerificationResourceLease(identity, owner, worker, manifest, relay)
        )
    except FileNotFoundError:
        refusal = VerificationResourcesRefused(
            VerificationResourceFailure.CONFIGURATION_REFUSED,
            "protected verification resource configuration or credential is absent",
        )
    except _ResourceRefusal as exc:
        refusal = VerificationResourcesRefused(exc.code, exc.reason)
    except Exception:
        refusal = VerificationResourcesRefused(
            VerificationResourceFailure.PROVISIONING_FAILED,
            "verification resource provisioning failed; no owner credential was delegated",
        )
    if relay is not None:
        relay.close()
    if (
        identity is not None
        and owner is not None
        and manifest is not None
        and isinstance(_cleanup(identity, owner, manifest), VerificationResourceCleanupPending)
    ):
        return VerificationResourcesRefused(
            VerificationResourceFailure.CLEANUP_PENDING,
            f"verification provisioning failed; lease {identity.lease_id} needs protected cleanup",
        )
    return refusal


def cleanup_verification_resource_lease(lease_id: str) -> VerificationResourceCleanup:
    """Retry one protected manifest; never discover or drop unrelated resources."""

    if re.fullmatch(r"[0-9a-f]{32}", lease_id) is None:
        raise ValueError("verification lease identity must be a generated hexadecimal UUID")
    try:
        directory = _resource_directory()
        _assert_protected_directory(directory)
        manifest = directory / f"lease-{lease_id}.json"
        payload = _LeaseManifest.model_validate_json(_read_protected_json(manifest))
        identity = payload.identity
        configuration = _load_configuration()
        if (
            configuration is not None
            and configuration.target_project_id == identity.target_project_id
        ):
            if isinstance(identity, NeonVerificationResourceIdentity) and (
                not isinstance(configuration, NeonConfiguration)
                or configuration.project_id != identity.neon_project_id
            ):
                owner, directory, common_directory = _load_legacy_owner(identity)
            else:
                owner, directory, common_directory = _load_owner(configuration)
        elif isinstance(identity, NeonVerificationResourceIdentity):
            owner, directory, common_directory = _load_legacy_owner(identity)
        else:
            raise ValueError("local lease no longer has its protected target binding")
        if (
            identity.lease_id != lease_id
            or identity.role != f"{_ROLE_PREFIX}{lease_id}"
            or identity.host != owner.host
            or identity.port != owner.port
            or identity.database != owner.database
            or identity.source_common_directory != str(common_directory)
        ):
            raise ValueError("verification cleanup manifest identity does not match its resource")
        return _cleanup(identity, owner, manifest)
    except Exception:
        return VerificationResourceCleanupPending(lease_id)

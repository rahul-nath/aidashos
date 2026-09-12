# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pin a local PostgreSQL connection's identity before opening the upstream socket."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from time import monotonic

_PROTOCOL_3 = 3 << 16
_MAX_STARTUP_BYTES = 10000
_STARTUP_FIELDS = frozenset(
    {b"user", b"database", b"client_encoding", b"application_name", b"options"}
)
_SEARCH_PATH = re.compile(rb"-c search_path=[a-zA-Z_][a-zA-Z0-9_]*")


@dataclass(frozen=True)
class OpaquePostgresTls:
    """Remote PostgreSQL retains its end-to-end authenticated TLS stream."""


@dataclass(frozen=True)
class LocalPostgresStartupGuard:
    role: str
    database: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"aidashos_verify_[0-9a-f]{32}", self.role) is None:
            raise ValueError("local relay must name its generated leased role")
        if self.database != "local_agent":
            raise ValueError("local relay must name the dedicated verification database")

    def validate(self, packet: bytes) -> None:
        """Only protocol 3 startup can select this role and database; no negotiation/cancel."""

        if (
            not 8 <= len(packet) <= _MAX_STARTUP_BYTES
            or int.from_bytes(packet[:4], "big") != len(packet)
            or int.from_bytes(packet[4:8], "big") != _PROTOCOL_3
        ):
            raise ValueError("undeclared PostgreSQL startup protocol")
        fields = packet[8:].split(b"\0")
        if fields[-2:] != [b"", b""] or len(fields[:-2]) % 2:
            raise ValueError("malformed PostgreSQL startup fields")
        parameters: dict[bytes, bytes] = {}
        for key, value in zip(fields[:-2:2], fields[1:-2:2], strict=True):
            if key not in _STARTUP_FIELDS or key in parameters:
                raise ValueError("undeclared or repeated PostgreSQL startup field")
            parameters[key] = value
        if parameters.get(b"user") != self.role.encode("ascii") or parameters.get(
            b"database"
        ) != self.database.encode("ascii"):
            raise ValueError("PostgreSQL startup identity differs from its lease")
        options = parameters.get(b"options")
        if options is not None and _SEARCH_PATH.fullmatch(options) is None:
            raise ValueError("only the test schema search path may be set at startup")

    def read(self, client: socket.socket, *, deadline: float) -> bytes:
        header = _read_exact(client, 4, deadline)
        size = int.from_bytes(header, "big")
        if not 8 <= size <= _MAX_STARTUP_BYTES:
            raise ValueError("PostgreSQL startup exceeds its bounded packet contract")
        packet = header + _read_exact(client, size - len(header), deadline)
        self.validate(packet)
        return packet


def _read_exact(client: socket.socket, count: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("PostgreSQL startup exceeded its total read deadline")
        client.settimeout(remaining)
        data = client.recv(count - len(chunks))
        if not data:
            raise ValueError("PostgreSQL startup ended before its declared length")
        chunks.extend(data)
    return bytes(chunks)


type PostgresStartupPolicy = OpaquePostgresTls | LocalPostgresStartupGuard

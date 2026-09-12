# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated stdio transport for a host-owned, contained Code Mode connection.

This file also runs as a copied standalone program under Python -I -S, using
only the standard library. Model bytes are forwarded as bounded frames; they
are never evaluated in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

FRAME_LIMIT = 2 * 1024 * 1024
MANIFEST_NAME = "relay.json"
RELAY_NAME = "relay.py"
CLIENT_NAME = "codex"
SHIM_NAME = "codex-code-mode-host"


@dataclass(frozen=True)
class CodeModeEndpoint:
    path: Path
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.path.is_absolute()
            or ".." in self.path.parts
            or len(os.fsencode(self.path)) >= 104
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.token)
        ):
            raise ValueError("invalid private Code Mode endpoint")

    def payload(self) -> dict[str, str]:
        return {"path": str(self.path), "token": self.token}

    @classmethod
    def from_payload(cls, value: object) -> CodeModeEndpoint:
        if not isinstance(value, dict) or set(value) != {"path", "token"}:
            raise ValueError("invalid Code Mode endpoint shape")
        if not isinstance(value["path"], str) or not isinstance(value["token"], str):
            raise ValueError("invalid Code Mode endpoint values")
        return cls(Path(value["path"]), value["token"])


def file_digest(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("relay binding is not a regular file")
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_package(root: Path, expected_manifest: str) -> dict:
    """Validate all copied code before admitting the private connection."""
    if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest):
        raise ValueError("invalid relay manifest binding")
    descriptor = os.open(root / MANIFEST_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("relay manifest is not a regular file")
        raw = source.read(8193)
    if len(raw) > 8192 or hashlib.sha256(raw).hexdigest() != expected_manifest:
        raise ValueError("relay manifest changed")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "endpoint", "python", "files"}
        or value["schema"] != "aidashos.codex-local-stdio.v1"
        or not isinstance(value["python"], dict)
        or set(value["python"]) != {"path", "sha256"}
        or not isinstance(value["files"], dict)
        or set(value["files"]) != {CLIENT_NAME, RELAY_NAME}
    ):
        raise ValueError("invalid relay manifest shape")
    executable = Path(sys.executable).resolve(strict=True)
    if (
        str(executable) != value["python"]["path"]
        or file_digest(executable) != value["python"]["sha256"]
    ):
        raise ValueError("relay Python differs from its prepared binding")
    for name, expected in value["files"].items():
        if file_digest(root / name) != expected:
            raise ValueError("relay package code changed")
    CodeModeEndpoint.from_payload(value["endpoint"])
    return value


def checked_frame(raw: bytes) -> bytes:
    if len(raw) < 4 or len(raw) > FRAME_LIMIT or int.from_bytes(raw[:4], "little") != len(raw) - 4:
        raise ValueError("invalid Code Mode stdio frame")
    return raw


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as failure:
        if failure.partial:
            raise ValueError("truncated Code Mode stdio header") from failure
        return None
    length = int.from_bytes(header, "little")
    if length > FRAME_LIMIT - 4:
        raise ValueError("Code Mode stdio frame exceeded its bound")
    return checked_frame(header + await reader.readexactly(length))


async def connect_endpoint(
    endpoint: CodeModeEndpoint,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(endpoint.path, limit=FRAME_LIMIT)
    try:
        async with asyncio.timeout(5):
            writer.write(endpoint.token.encode("ascii") + b"\n")
            await writer.drain()
            if await reader.readexactly(3) != b"ok\n":
                raise ValueError("Code Mode endpoint refused authentication")
        return reader, writer
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


async def relay(endpoint: CodeModeEndpoint) -> None:

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=FRAME_LIMIT)
    input_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    output_transport = None
    peer = None
    try:
        output_transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer
        )
        writer = asyncio.StreamWriter(output_transport, protocol, None, loop)
        incoming, peer = await connect_endpoint(endpoint)

        async def transfer(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
            while (frame := await read_frame(source)) is not None:
                destination.write(frame)
                await destination.drain()

        tasks = [
            asyncio.create_task(transfer(reader, peer)),
            asyncio.create_task(transfer(incoming, writer)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if peer is not None:
            peer.close()
            await peer.wait_closed()
        input_transport.close()
        if output_transport is not None:
            output_transport.close()


def main() -> None:
    if len(sys.argv) != 2 or not sys.flags.isolated or not sys.flags.no_site:
        raise ValueError("relay requires only its prepared binding and isolated Python")
    root = Path(__file__).resolve(strict=True).parent
    value = verify_package(root, sys.argv[1])
    os.environ.clear()
    asyncio.run(relay(CodeModeEndpoint.from_payload(value["endpoint"])))


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        # Exception text may contain the bearer capability. The parent owns diagnostics.
        print(f"Code Mode stdio relay failed: {type(failure).__name__}", file=sys.stderr)
        raise SystemExit(125) from None

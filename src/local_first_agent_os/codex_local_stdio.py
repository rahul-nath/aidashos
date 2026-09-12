# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private packaging for Codex's process-owned Code Mode stdio provider.

The installed client chooses its sibling interpreter by real executable path.
Only a byte-identical private client copy and a fixed relay occupy that layout;
no installed executable, model capability or interpreter permission changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import codex_stdio_relay
from .codex_stdio_relay import (
    CLIENT_NAME,
    MANIFEST_NAME,
    RELAY_NAME,
    SHIM_NAME,
    CodeModeEndpoint,
    file_digest,
    verify_package,
)


def _copy_regular(source: Path, destination: Path, mode: int) -> str:
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as reader, destination.open("xb") as writer:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 512 * 1024 * 1024:
            raise ValueError("Codex package source is not a bounded regular file")
        digest = hashlib.sha256()
        copied = 0
        while block := reader.read(1024 * 1024):
            copied += len(block)
            if copied > info.st_size:
                raise ValueError("Codex package source grew while copying")
            digest.update(block)
            writer.write(block)
        if copied != info.st_size:
            raise ValueError("Codex package source shrank while copying")
    destination.chmod(mode)
    expected = digest.hexdigest()
    if file_digest(source) != expected or file_digest(destination) != expected:
        raise ValueError("Codex package source changed while copying")
    return expected


@dataclass(frozen=True)
class PreparedLocalStdioClient:
    executable: Path
    manifest_sha256: str
    _launcher_sha256: str = field(repr=False)
    _source_bindings: tuple[tuple[Path, str], ...] = field(repr=False)

    def provenance(self) -> dict[str, str]:
        root = self.executable.parent
        return {
            "source_codex": str(self._source_bindings[0][0]),
            "source_codex_sha256": self._source_bindings[0][1],
            "copied_codex_sha256": file_digest(self.executable),
            "relay_sha256": file_digest(root / RELAY_NAME),
            "launcher_sha256": self._launcher_sha256,
            "manifest_sha256": self.manifest_sha256,
            "python": str(Path(sys.executable).resolve(strict=True)),
            "python_sha256": file_digest(Path(sys.executable).resolve(strict=True)),
        }

    def verify(self) -> None:
        verify_package(self.executable.parent, self.manifest_sha256)
        if file_digest(self.executable.with_name(SHIM_NAME)) != self._launcher_sha256:
            raise ValueError("Codex stdio launcher changed")
        for source, expected in self._source_bindings:
            if file_digest(source) != expected:
                raise ValueError("Codex package source binding changed")


def prepare_local_stdio_client(
    codex_bin: str,
    *,
    endpoint: CodeModeEndpoint,
    home: Path,
) -> PreparedLocalStdioClient:
    """Prepare one exact client layout inside an already-owned temporary home."""
    root = home / "local-stdio"
    root.mkdir(mode=0o700)
    original = Path(codex_bin).resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    relay_source = Path(codex_stdio_relay.__file__).resolve(strict=True)
    source_bindings = []
    for source, name, mode in ((original, CLIENT_NAME, 0o500), (relay_source, RELAY_NAME, 0o400)):
        source_bindings.append((source, _copy_regular(source, root / name, mode)))

    manifest = {
        "schema": "aidashos.codex-local-stdio.v1",
        "endpoint": endpoint.payload(),
        "python": {"path": str(python), "sha256": file_digest(python)},
        "files": {name: file_digest(root / name) for name in (CLIENT_NAME, RELAY_NAME)},
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    expected_manifest = hashlib.sha256(raw).hexdigest()
    # The shell cannot choose arguments or load a mutable argument file. The
    # owner separately binds its bytes, avoiding a self-referential manifest.
    launcher = (
        "#!/bin/sh\n"
        'if [ "$#" -ne 0 ]; then exit 64; fi\n'
        "exec "
        + shlex.join(
            (
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
                f"HOME={home}",
                f"TMPDIR={home}",
                str(python),
                "-I",
                "-S",
                str(root / RELAY_NAME),
                expected_manifest,
            )
        )
        + "\n"
    )
    (root / SHIM_NAME).write_text(launcher)
    (root / SHIM_NAME).chmod(0o500)
    (root / MANIFEST_NAME).write_bytes(raw)
    (root / MANIFEST_NAME).chmod(0o400)
    result = PreparedLocalStdioClient(
        root / CLIENT_NAME, expected_manifest, file_digest(root / SHIM_NAME), tuple(source_bindings)
    )
    result.verify()
    return result

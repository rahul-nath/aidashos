# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicit installer provisioning and read-only validation of the host credential."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from .operator_identity import operator_token_file

_MAX_CREDENTIAL_BYTES = 4096
_CREDENTIAL_RANDOM_BYTES = 48


class CredentialInitialization(StrEnum):
    CREATED = "created"
    PRESERVED = "preserved"


def read_host_operator_credential(path: Path) -> str:
    """Only the configured host may load its owned, non-symlink, mode 0600 file."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > _MAX_CREDENTIAL_BYTES
        ):
            raise PermissionError("operator credential must be an owned mode 0600 regular file")
        data = os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1)
        if len(data) > _MAX_CREDENTIAL_BYTES:
            raise PermissionError("operator credential exceeds its bounded file contract")
        credential = data.decode().strip()
    finally:
        os.close(descriptor)
    if not credential:
        raise PermissionError("operator credential is empty")
    return credential


def initialize_operator_credential(path: Path) -> CredentialInitialization:
    """Create only when absent; refuse an invalid existing identity without replacing it."""

    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError(
                "operator credential directory must be owned and not writable by others"
            )
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            read_host_operator_credential(path)
            return CredentialInitialization.PRESERVED
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(_CREDENTIAL_RANDOM_BYTES) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        read_host_operator_credential(path)
        return CredentialInitialization.CREATED
    finally:
        os.close(directory)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("initialize", "check"))
    args = parser.parse_args(argv)
    path = operator_token_file()
    try:
        if args.operation == "initialize":
            disposition = initialize_operator_credential(path)
            print(f"Operator credential {disposition.value}: {path}")
        else:
            read_host_operator_credential(path)
            print(f"Operator credential ready: {path}")
    except (OSError, UnicodeError):
        parser.exit(
            1,
            f"Operator credential refused: {path}; "
            "inspect ownership, mode 0600, and nonempty contents.\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

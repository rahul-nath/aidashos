# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OS boundary around every frontier-agent process.

CLI permission flags express intent to the harness.
They do not stop the process itself from reading the dispatcher's credentials or
writing outside its leased worktree, so this module owns the lower boundary.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

from .operator_identity import operator_token_file
from .spawn_authority import (
    ReadOnlyInspection,
    SpawnPosture,
    UnattendedImplementation,
    describe_posture,
)
from .staffing import FrontierHarness
from .toolchains import project_environment

_SANDBOX_EXEC: Final = Path("/usr/bin/sandbox-exec")
_CONTEXT_ENV: Final = frozenset(
    {
        "LOCAL_AGENT_ASSIGNED_WORKTREE",
        "LOCAL_AGENT_CONTEXT_JSON",
        "LOCAL_AGENT_TERMINAL_SESSION_STARTED",
    }
)
_LEDGER_READER_ENV: Final = "LOCAL_AGENT_LEDGER_READER_DATABASE_URL"
_EXACT_ENV: Final = frozenset(
    {
        "CI",
        "CODEX_HOME",
        "COLORTERM",
        "CURL_CA_BUNDLE",
        "DEVELOPER_DIR",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_SYSTEM",
        "GOPATH",
        "GOROOT",
        "HOME",
        "JAVA_HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_COLOR",
        "NVM_DIR",
        "PATH",
        "PNPM_HOME",
        "REQUESTS_CA_BUNDLE",
        "RUSTUP_HOME",
        "SDKROOT",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "USER",
    }
)
_PREFIX_ENV: Final = ("LC_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


class ProcessContainmentUnavailable(RuntimeError):
    """The host cannot supply the mandatory frontier-process boundary."""


@dataclass(frozen=True)
class ContainedProcess:
    command: tuple[str, ...]
    environment: Mapping[str, str]
    scratch_path: Path
    posture: str


class ProcessContainer(Protocol):
    """The host boundary used by the executor, independent of one OS facility."""

    def contain(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        posture: SpawnPosture,
        harness: FrontierHarness,
        overrides: Mapping[str, str] | None = None,
    ) -> AbstractContextManager[ContainedProcess]: ...


def _allowed_environment(
    cwd: Path,
    overrides: Mapping[str, str] | None,
    scratch: Path,
) -> dict[str, str]:
    resolved = project_environment(cwd, overrides)
    allowed = {
        name: value
        for name, value in resolved.items()
        if name in _EXACT_ENV or name in _CONTEXT_ENV or name.startswith(_PREFIX_ENV)
    }
    allowed.update(
        {
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
            "UV_CACHE_DIR": str(scratch / "uv-cache"),
            "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
            "npm_config_cache": str(scratch / "npm-cache"),
        }
    )
    reader_url = resolved.get(_LEDGER_READER_ENV)
    if reader_url:
        # The MCP server reads the historical names.  Deliberately replace them
        # with the reader role rather than forwarding either ambient writer URL.
        allowed[_LEDGER_READER_ENV] = reader_url
        allowed["AGENT_COORDINATION_DATABASE_URL"] = reader_url
        allowed["LOCAL_AGENT_COORDINATION_DATABASE_URL"] = reader_url
    return allowed


def _filter(kind: str, path: Path) -> str:
    return f"({kind} {json.dumps(str(path.expanduser().resolve()))})"


def _git_paths(cwd: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return exact Git read and implementation-write paths for one worktree."""

    probe = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return (), ()
    parts = tuple(Path(line).resolve() for line in probe.stdout.splitlines() if line.strip())
    if len(parts) != 2:
        raise ProcessContainmentUnavailable(f"git returned an invalid path set for {cwd}")
    git_dir, common_dir = parts
    reads = (git_dir, common_dir)
    writes: list[Path] = [git_dir, common_dir / "objects"]
    branch = subprocess.run(
        ["git", "-C", str(cwd), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if branch:
        for base in (common_dir, common_dir / "logs"):
            ref = base / branch
            writes.extend((ref, ref.with_name(f"{ref.name}.lock")))
    return reads, tuple(writes)


def _command_read_paths(command: Sequence[str], environment: Mapping[str, str]) -> tuple[Path, ...]:
    executable = str(command[0]) if command else ""
    resolved = shutil.which(executable, path=environment.get("PATH"))
    roots: list[Path] = []
    if resolved:
        binary = Path(resolved)
        roots.extend((binary, binary.resolve()))
    elif executable:
        binary = Path(executable).expanduser()
        if binary.exists():
            roots.extend((binary, binary.resolve()))
    for entry in environment.get("PATH", "").split(os.pathsep):
        if entry:
            path_entry = Path(entry).expanduser()
            roots.append(path_entry)
            if path_entry.name == "bin":
                roots.append(path_entry.parent)
    return tuple(dict.fromkeys(path.resolve() for path in roots))


def _reader_database_endpoints(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw = environment.get(_LEDGER_READER_ENV)
    if not raw:
        return ()
    parsed = urlsplit(raw.replace("postgresql+psycopg://", "postgresql://", 1))
    if not parsed.hostname:
        raise ProcessContainmentUnavailable("ledger reader URL requires a network host")
    port = parsed.port or 5432
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProcessContainmentUnavailable("ledger reader host could not be resolved") from exc
    resolved = {entry[4][0] for entry in addresses}
    if not resolved or any(not ipaddress.ip_address(address).is_loopback for address in resolved):
        raise ProcessContainmentUnavailable(
            "the macOS containment adapter requires a loopback ledger reader"
        )
    return (f"localhost:{port}",)


def _profile(
    *,
    command: Sequence[str],
    cwd: Path,
    scratch: Path,
    posture: SpawnPosture,
    harness: FrontierHarness,
    environment: Mapping[str, str],
) -> str:
    home = Path(environment.get("HOME") or Path.home()).expanduser().resolve()
    git_reads, git_writes = _git_paths(cwd)
    writable: list[str] = [_filter("subpath", scratch), _filter("literal", Path("/dev/null"))]
    if isinstance(posture, UnattendedImplementation):
        writable.append(_filter("subpath", cwd))
        writable.extend(
            _filter("subpath" if path.is_dir() else "literal", path) for path in git_writes
        )
    if harness is FrontierHarness.CODEX:
        writable.append(_filter("subpath", Path(environment.get("CODEX_HOME") or home / ".codex")))
    else:
        writable.extend(
            (
                _filter("subpath", home / ".claude"),
                _filter("literal", home / ".claude.json"),
            )
        )
    readable: list[Path] = [
        Path("/"),
        cwd,
        scratch,
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/dev/null"),
        Path("/sbin"),
        Path("/private/etc"),
        Path("/private/var/db/dyld"),
        Path("/Library/Apple"),
        home / ".gitconfig",
        home / ".config" / "git",
        *git_reads,
        *_command_read_paths(command, environment),
    ]
    if harness is FrontierHarness.CODEX:
        readable.append(Path(environment.get("CODEX_HOME") or home / ".codex"))
    else:
        readable.extend((home / ".claude", home / ".claude.json"))
    read_rules = tuple(
        _filter(
            "literal" if path == Path("/") else ("subpath" if path.is_dir() else "literal"),
            path,
        )
        for path in dict.fromkeys(path.expanduser().resolve() for path in readable if path.exists())
    )
    database_network = tuple(
        f"(allow network-outbound (remote tcp {json.dumps(endpoint)}))"
        for endpoint in _reader_database_endpoints(environment)
    )
    return " ".join(
        (
            "(version 1)",
            "(allow default)",
            # Metadata lookup stays available because dyld, Python, and the
            # harnesses probe optional paths while starting. File contents are
            # the sensitive boundary and remain allowlisted.
            "(deny file-read-data)",
            *(f"(allow file-read-data {item})" for item in read_rules),
            "(deny file-write*)",
            *(f"(allow file-write* {item})" for item in writable),
            f"(deny file-read-data {_filter('literal', operator_token_file())})",
            "(deny network-outbound)",
            '(allow network-outbound (literal "/private/var/run/mDNSResponder"))',
            '(allow network-outbound (remote tcp "*:443"))',
            *database_network,
        )
    )


class MacOSSeatbeltContainer:
    """macOS implementation of the frontier-process boundary."""

    @contextmanager
    def contain(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        posture: SpawnPosture,
        harness: FrontierHarness,
        overrides: Mapping[str, str] | None = None,
    ) -> Iterator[ContainedProcess]:
        if platform.system() != "Darwin" or not _SANDBOX_EXEC.is_file():
            raise ProcessContainmentUnavailable(
                "frontier execution requires macOS sandbox-exec on this local runtime"
            )
        with tempfile.TemporaryDirectory(prefix="local-agent-seat-") as raw_scratch:
            scratch = Path(raw_scratch).resolve()
            environment = _allowed_environment(cwd, overrides, scratch)
            profile = _profile(
                command=command,
                cwd=cwd.resolve(),
                scratch=scratch,
                posture=posture,
                harness=harness,
                environment=environment,
            )
            yield ContainedProcess(
                command=(str(_SANDBOX_EXEC), "-p", profile, *(str(part) for part in command)),
                environment=environment,
                scratch_path=scratch,
                posture=describe_posture(posture),
            )


def process_container_for_host() -> ProcessContainer:
    if platform.system() == "Darwin" and _SANDBOX_EXEC.is_file():
        return MacOSSeatbeltContainer()
    raise ProcessContainmentUnavailable(
        "no frontier process containment adapter exists for this host"
    )


def assert_process_containment_available() -> None:
    """Prove the resident runtime can launch a real process inside its boundary."""

    container = process_container_for_host()
    cwd = Path.cwd().resolve()
    with container.contain(
        ("/usr/bin/true",),
        cwd,
        posture=ReadOnlyInspection(),
        harness=FrontierHarness.CODEX,
    ) as contained:
        probe = subprocess.run(
            contained.command,
            cwd=cwd,
            env=contained.environment,
            capture_output=True,
            text=True,
            check=False,
        )
    if probe.returncode != 0:
        raise ProcessContainmentUnavailable(
            f"frontier containment probe exited {probe.returncode}: {probe.stderr.strip()}"
        )


@contextmanager
def contained_frontier_process(
    command: Sequence[str],
    cwd: Path,
    *,
    posture: SpawnPosture,
    harness: FrontierHarness,
    overrides: Mapping[str, str] | None = None,
) -> Iterator[ContainedProcess]:
    """Compatibility entry point for tests and non-executor callers."""

    with process_container_for_host().contain(
        command,
        cwd,
        posture=posture,
        harness=harness,
        overrides=overrides,
    ) as contained:
        yield contained


__all__ = [
    "ContainedProcess",
    "MacOSSeatbeltContainer",
    "ProcessContainer",
    "ProcessContainmentUnavailable",
    "assert_process_containment_available",
    "contained_frontier_process",
    "process_container_for_host",
]

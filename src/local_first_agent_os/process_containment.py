# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OS boundary around every frontier-agent process.

CLI permission flags express intent to the harness.
They do not stop the process itself from reading the dispatcher's credentials or
writing outside its leased worktree, so this module owns the lower boundary.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlsplit

from .operator_identity import operator_token_file
from .seatbelt_policy import PathGrant, PathScope, SeatbeltPolicy, TcpGrant, UnixGrant
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
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
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
    from .native_verification_broker import BROKER_ENV

    resolved = project_environment(cwd, overrides)
    allowed = {
        name: value
        for name, value in resolved.items()
        if name in _EXACT_ENV or name in _CONTEXT_ENV or name.startswith(_PREFIX_ENV)
    }
    # A caller override cannot select a different owner for the native transport.
    # The receiving broker still authenticates the caller by kernel identity.
    if BROKER_ENV in os.environ:
        allowed[BROKER_ENV] = os.environ[BROKER_ENV]
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


type GitPathProbe = Callable[[Path, tuple[str, ...]], subprocess.CompletedProcess[str]]


def _git_path_probe(cwd: Path, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(cwd), *arguments), capture_output=True, text=True, check=False
    )


def _git_paths(
    cwd: Path, probe_command: GitPathProbe = _git_path_probe
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return exact Git read and implementation-write paths for one worktree."""

    probe = probe_command(
        cwd, ("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    )
    if probe.returncode != 0:
        return (), ()
    parts = tuple(Path(line).resolve() for line in probe.stdout.splitlines() if line.strip())
    if len(parts) != 2:
        raise ProcessContainmentUnavailable(f"git returned an invalid path set for {cwd}")
    git_dir, common_dir = parts
    reads = (git_dir, common_dir)
    writes: list[Path] = [git_dir, common_dir / "objects"]
    branch = probe_command(cwd, ("symbolic-ref", "-q", "HEAD")).stdout.strip()
    if branch:
        for base in (common_dir, common_dir / "logs"):
            ref = base / branch
            writes.extend((ref, ref.with_name(f"{ref.name}.lock")))
    return reads, tuple(writes)


def _runtime_read_paths(executable: Path) -> tuple[Path, ...]:
    resolved = executable.resolve()
    roots = [executable, resolved]
    runtime_library = resolved.parent.parent / "lib"
    if resolved.parent.name == "bin" and runtime_library.is_dir():
        # Relocatable runtimes such as uv-managed Python load both their dylib
        # and standard library beside the real bin directory.
        roots.append(runtime_library)
    return tuple(roots)


type ShebangRead = Callable[[Path], bytes]


def _read_shebang(script: Path) -> bytes:
    with script.open("rb") as stream:
        return stream.readline(4096)


def _shebang_executable(
    script: Path, environment: Mapping[str, str], read: ShebangRead = _read_shebang
) -> Path | None:
    try:
        first_line = read(script)
    except OSError:
        return None
    if not first_line.startswith(b"#!"):
        return None
    try:
        arguments = shlex.split(first_line[2:].decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not arguments:
        return None
    interpreter = arguments[0]
    if Path(interpreter).name == "env":
        candidates = (
            argument
            for argument in arguments[1:]
            if not argument.startswith("-") and "=" not in argument
        )
        interpreter = next(candidates, "")
    resolved = shutil.which(interpreter, path=environment.get("PATH"))
    return Path(resolved) if resolved else None


def _command_read_paths(
    command: Sequence[str],
    environment: Mapping[str, str],
    read_shebang: ShebangRead = _read_shebang,
) -> tuple[Path, ...]:
    executable = str(command[0]) if command else ""
    resolved = shutil.which(executable, path=environment.get("PATH"))
    roots: list[Path] = []
    if resolved:
        binary = Path(resolved)
        roots.extend(_runtime_read_paths(binary))
        interpreter = _shebang_executable(binary, environment, read_shebang)
        if interpreter is not None:
            roots.extend(_runtime_read_paths(interpreter))
    elif executable:
        binary = Path(executable).expanduser()
        if binary.exists():
            roots.extend(_runtime_read_paths(binary))
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
    if parsed.hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError as exc:
            raise ProcessContainmentUnavailable(
                "the ledger reader requires a literal loopback address"
            ) from exc
        if not loopback:
            raise ProcessContainmentUnavailable("the ledger reader requires a loopback address")
    return (f"localhost:{port}",)


def _containment_policy(
    *,
    command: Sequence[str],
    cwd: Path,
    scratch: Path,
    posture: SpawnPosture,
    harness: FrontierHarness,
    environment: Mapping[str, str],
    git_probe: GitPathProbe = _git_path_probe,
    read_shebang: ShebangRead = _read_shebang,
) -> SeatbeltPolicy:
    home = Path(environment.get("HOME") or Path.home()).expanduser().resolve()
    git_reads, git_writes = _git_paths(cwd, git_probe)
    writable = [PathGrant(scratch), PathGrant(Path("/dev/null"), PathScope.EXACT)]
    if isinstance(posture, UnattendedImplementation):
        writable.append(PathGrant(cwd))
        writable.extend(
            PathGrant(path, PathScope.TREE if path.is_dir() else PathScope.EXACT)
            for path in git_writes
        )
    if harness is FrontierHarness.CODEX:
        writable.append(PathGrant(Path(environment.get("CODEX_HOME") or home / ".codex")))
    else:
        writable.extend(
            (
                PathGrant(home / ".claude"),
                PathGrant(home / ".claude.json", PathScope.EXACT),
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
        # Claude Code initializes Foundation before it parses its command line.
        # Foundation reads the system timezone database and terminates the
        # process when Seatbelt hides it, even for `claude --version`.
        Path("/private/var/db/timezone"),
        Path("/Library/Apple"),
        home / ".gitconfig",
        home / ".config" / "git",
        *git_reads,
        *_command_read_paths(command, environment, read_shebang),
    ]
    if harness is FrontierHarness.CODEX:
        readable.append(Path(environment.get("CODEX_HOME") or home / ".codex"))
    else:
        readable.extend(
            (
                home / ".claude",
                home / ".claude.json",
                # Claude Code stores its session in the macOS login keychain.
                # Security.framework cannot discover that item when Seatbelt
                # hides the backing database, so expose this file rather than
                # the whole Keychains directory.
                home / "Library" / "Keychains" / "login.keychain-db",
            )
        )
    read_rules = tuple(
        PathGrant(
            path, PathScope.EXACT if path == Path("/") or not path.is_dir() else PathScope.TREE
        )
        for path in dict.fromkeys(path.expanduser().resolve() for path in readable if path.exists())
    )
    database_network = tuple(
        TcpGrant("localhost", int(endpoint.rsplit(":", 1)[1]))
        for endpoint in _reader_database_endpoints(environment)
    )
    return SeatbeltPolicy(
        reads=(read_rules,),
        writes=(tuple(writable),),
        outbound=(
            (
                UnixGrant(Path("/private/var/run/mDNSResponder")),
                TcpGrant("*", 443),
                *database_network,
            ),
        ),
        forbidden_reads=(PathGrant(operator_token_file(), PathScope.EXACT),),
        deny_other_network=False,
        pty=True,
    )


def _profile(
    *,
    command: Sequence[str],
    cwd: Path,
    scratch: Path,
    posture: SpawnPosture,
    harness: FrontierHarness,
    environment: Mapping[str, str],
) -> str:
    return _containment_policy(
        command=command,
        cwd=cwd,
        scratch=scratch,
        posture=posture,
        harness=harness,
        environment=environment,
    ).render()


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
            from .native_verification_broker import broker_command

            proxy = broker_command(
                command,
                cwd,
                scratch,
                environment,
                posture=describe_posture(posture),
                harness=harness.value,
            )
            if proxy is None:
                profile = _profile(
                    command=command,
                    cwd=cwd.resolve(),
                    scratch=scratch,
                    posture=posture,
                    harness=harness,
                    environment=environment,
                )
                native_command = (
                    str(_SANDBOX_EXEC),
                    "-p",
                    profile,
                    *(str(part) for part in command),
                )
            else:
                native_command = proxy
            yield ContainedProcess(
                command=native_command,
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

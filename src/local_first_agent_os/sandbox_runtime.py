# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pinned native containment for credential-free tool services.

The trusted model connection is not a child of this boundary.
The adapter must authorize each tool effect before sending it to the service;
read-only filesystem containment does not itself grant RUN_COMMAND.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .process_containment import ContainedProcess, ProcessContainmentUnavailable

_SOURCE_REVISION = "40804af269e1616092e9971de12a1f358f58eba9"
_LOCK_SHA256 = "dd606f0b0c87c422b9c2671312e3379e5705d2ff3a64659a32f8afdbe4af6faa"
_RUNTIME_VERSION = "0.0.75"
_NODE_VERSION = "v22.19.0"
# SRT always prepends these shared/device writes to filesystem.allowWrite.
# Stdio and /dev/null remain usable; a headless worker does not need the other
# devices or another agent's shared Claude scratch directory.
_DENIED_DEFAULT_WRITES = (
    "/tmp/claude",
    "/private/tmp/claude",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/autofs_nowait",
)
_PLATFORM_READS = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/private/etc",
    "/dev/null",
    "/private/var/db/dyld",
    "/private/var/select/sh",
)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _run_identity(command: Sequence[str]) -> str:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": "/var/empty",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )
    return result.stdout.strip()


def _runtime_tree_sha256(source: Path) -> str:
    """Measure installed code without unmeasured directory symlink subtrees."""
    digest = hashlib.sha256()
    for directory in (source / "dist", source / "node_modules"):
        if directory.is_symlink():
            raise ValueError("installed runtime directories must not be symlinks")
        for entry in sorted(directory.rglob("*")):
            if entry.is_symlink():
                target = entry.resolve(strict=True)
                if not target.is_file() or not target.is_relative_to(source):
                    raise ValueError("installed runtime symlink is not an internal file")
                digest.update(str(target.relative_to(source)).encode() + b"\0")
            if entry.is_file():
                digest.update(str(entry.relative_to(source)).encode() + b"\0")
                digest.update(_sha256(entry).encode())
            elif not entry.is_dir():
                raise ValueError("installed runtime contains a non-regular entry")
    return digest.hexdigest()


@dataclass(frozen=True)
class SandboxRuntimeInstallation:
    """Identity of installed bytes, never permission to fetch or repair them."""

    source: Path
    node: Path
    identity_sha256: str

    @classmethod
    def inspect(cls, source: Path, node: Path) -> SandboxRuntimeInstallation:
        if platform.system() != "Darwin":
            raise ProcessContainmentUnavailable("SRT worker profile is verified only on macOS")
        source, node = source.resolve(strict=True), node.resolve(strict=True)
        try:
            manifest = json.loads((source / "package.json").read_text())
            if manifest.get("version") != _RUNTIME_VERSION:
                raise ValueError("SRT package version does not match the pin")
            git = ("/usr/bin/git", "-c", "core.fsmonitor=false", "-C", str(source))
            if _run_identity((*git, "rev-parse", "HEAD")) != _SOURCE_REVISION:
                raise ValueError("SRT source revision does not match the pin")
            if _run_identity((*git, "status", "--porcelain")):
                raise ValueError("SRT source has uncommitted changes")
            if _sha256(source / "package-lock.json") != _LOCK_SHA256:
                raise ValueError("SRT transitive lockfile does not match the pin")
            if _run_identity((str(node), "--version")) != _NODE_VERSION:
                raise ValueError("Node version differs from the verified toolchain")
            if not (source / "dist" / "index.js").is_file():
                raise ValueError("SRT is not built")
            if not (source / "node_modules" / "zod" / "package.json").is_file():
                raise ValueError("SRT dependencies are not installed")
            # Include generated code and installed dependencies, not just their
            # source lockfile. Any on-disk drift invalidates a prepared launch.
            digest = hashlib.sha256()
            digest.update(_SOURCE_REVISION.encode())
            digest.update(_sha256(node).encode())
            digest.update(_sha256(Path(__file__).with_name("_sandbox_worker.mjs")).encode())
            digest.update(_runtime_tree_sha256(source).encode())
            return cls(source, node, digest.hexdigest())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ProcessContainmentUnavailable(
                f"pinned SRT installation unavailable: {exc}"
            ) from exc

    def revalidate(self) -> None:
        if self.inspect(self.source, self.node) != self:
            raise ProcessContainmentUnavailable("SRT installation changed after preparation")


@dataclass(frozen=True)
class WorkerLaunchIdentity:
    """The exact runtime, executable, policy and request prepared by the host.

    An identity is not a conformance approval; readiness must bind this value
    to the permitted and denied operations exercised through the adapter.
    """

    runtime_sha256: str
    service_sha256: str
    policy_sha256: str
    request_sha256: str


@dataclass(frozen=True)
class PreparedToolService(ContainedProcess):
    identity: WorkerLaunchIdentity


@dataclass(frozen=True)
class ReadOnlyToolWorker:
    """One repository, private state, no inherited credentials or network.

    This is a native-process boundary, not a second capability vocabulary.
    RPC authorization remains a mandatory adapter responsibility.
    """

    installation: SandboxRuntimeInstallation
    repository: Path

    @contextmanager
    def contain_service(self, command: Sequence[str]) -> Iterator[PreparedToolService]:
        self.installation.revalidate()
        repository = self.repository.resolve(strict=True)
        if not repository.is_dir() or not command or not Path(command[0]).is_absolute():
            raise ValueError("native service requires a repository and absolute executable")
        executable = Path(command[0]).resolve(strict=True)
        # macOS Unix socket paths are limited to 104 bytes. SRT creates its
        # sockets beneath worker TMPDIR; inheriting the driver's nested scratch
        # can truncate distinct socket names into the same bind address.
        # The fixed short parent is public, but each owned child stays mode 0700.
        with tempfile.TemporaryDirectory(prefix="aos-worker-", dir="/private/tmp") as raw:
            root = Path(raw).resolve()
            scratch = root / "worker"
            scratch.mkdir(mode=0o700)
            (scratch / "codex").mkdir(mode=0o700)
            environment = {
                "HOME": str(scratch),
                "CODEX_HOME": str(scratch / "codex"),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "SHELL": "/bin/sh",
                "TMPDIR": str(scratch),
                "CLAUDE_CODE_TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "UV_OFFLINE": "1",
                "UV_CACHE_DIR": str(scratch / "uv-cache"),
                "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
                "npm_config_offline": "true",
                "npm_config_cache": str(scratch / "npm-cache"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            }
            config = {
                "network": {
                    "allowedDomains": [],
                    "deniedDomains": ["*"],
                    "strictAllowlist": True,
                    "allowLocalBinding": False,
                    "allowAllUnixSockets": False,
                },
                "filesystem": {
                    "denyRead": ["/"],
                    "allowRead": list(
                        dict.fromkeys(
                            (
                                *_PLATFORM_READS,
                                str(repository),
                                str(scratch),
                                str(executable),
                                str(command[0]),
                            )
                        )
                    ),
                    "allowWrite": [str(scratch)],
                    "denyWrite": [str(repository), *_DENIED_DEFAULT_WRITES],
                },
            }
            request = root / "launch.json"
            service_sha256 = _sha256(executable)
            request.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "argv": list(command),
                        "serviceExecutable": str(executable),
                        "serviceSha256": service_sha256,
                        "cwd": str(repository),
                        "env": environment,
                        "config": config,
                    }
                )
            )
            request.chmod(0o600)
            bridge = Path(__file__).with_name("_sandbox_worker.mjs")
            identity = WorkerLaunchIdentity(
                runtime_sha256=self.installation.identity_sha256,
                service_sha256=service_sha256,
                policy_sha256=hashlib.sha256(
                    json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                request_sha256=_sha256(request),
            )
            yield PreparedToolService(
                command=(
                    str(self.installation.node),
                    str(bridge),
                    str(self.installation.source),
                    identity.request_sha256,
                    str(request),
                ),
                environment=MappingProxyType(environment),
                scratch_path=scratch,
                posture="read_only_tool_worker",
                identity=identity,
            )

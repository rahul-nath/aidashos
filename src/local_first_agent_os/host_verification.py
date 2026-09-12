# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Protected receipts from registered commands executed over a committed snapshot.

The public result writer can reference a receipt, but cannot mint one. Only this
host runner inserts receipt rows, after observing the real contained processes.
Legacy command/output strings remain observations outside this contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zlib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import Capability
from .constants import PROCESS_TIMEOUT_EXIT_CODE
from .contracts import DispatchIntentStatus, LeaseStatus
from .coordination.store import ConnectionLike, now, rowdict, tx
from .macho_dependencies import linked_runtime_files as _linked_runtime_files
from .native_verification_broker import NativeVerificationBroker
from .pow_wow.types import CommandRunCapture
from .project_center import LinkedProject, load_project_center
from .seatbelt_policy import PathGrant, PathScope, SeatbeltPolicy, TcpGrant
from .toolchains import installed_node_environment, project_environment
from .uid_verifier_client import UidVerifierClient, UidVerifierUnavailable
from .verification_git import verification_git_environment
from .verification_resources import (
    VerificationResourceClosed,
    VerificationResourceFailure,
    VerificationResourceIdentity,
    VerificationResourcesAbsent,
    VerificationResourcesPresent,
    VerificationResourcesRefused,
    acquire_verification_resources,
)
from .verification_toolchain_staging import stage_installed_toolchain
from .work_units.lifecycle import TERMINAL_WORK_UNIT_STATUSES, WorkUnitStatus

RECEIPT_SCHEMA = "host_verification_receipt.v1"
REFERENCE_KIND = "host_verification_receipt_reference"
_SANDBOX = Path("/usr/bin/sandbox-exec")
type _GitObjectId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class VerificationSubject(_Record):
    intent_id: str = Field(min_length=1)
    target_project_id: str = Field(min_length=1)
    work_unit_id: str = Field(min_length=1)
    milestone_key: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    compiled_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CommittedSource(_Record):
    kind: Literal["committed"] = "committed"
    commit: _GitObjectId
    tree: _GitObjectId
    base: _GitObjectId
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class BoundLease(_Record):
    lease_id: str
    worker_id: str
    created_at: float = Field(ge=0)
    permission_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_base_commit: str | None


class ProcessCapture(_Record):
    command: str = Field(min_length=1)
    exit_code: int
    stdout: str
    stderr: str
    cwd: Annotated[str, Field(min_length=1)] | None = None


class AllCommandsCompleted(_Record):
    kind: Literal["all_commands_completed"] = "all_commands_completed"


class GateDeadlineExceeded(_Record):
    kind: Literal["deadline_exceeded"] = "deadline_exceeded"
    started_command_count: int = Field(ge=0)


type GateExecutionEnd = Annotated[
    AllCommandsCompleted | GateDeadlineExceeded, Field(discriminator="kind")
]
type _ProcessOutcome = Literal["passed", "failed", "cancelled"]


class NoExternalVerificationResource(_Record):
    kind: Literal["none"] = "none"


class ClosedVerificationResource(_Record):
    kind: Literal["closed"] = "closed"
    identity: VerificationResourceIdentity


class PendingVerificationResource(_Record):
    kind: Literal["cleanup_pending"] = "cleanup_pending"
    identity: VerificationResourceIdentity
    code: VerificationResourceFailure = VerificationResourceFailure.CLEANUP_PENDING
    reason: str = "verification resource cleanup requires an explicit protected attestation"


type VerificationResourceDisposition = Annotated[
    NoExternalVerificationResource | ClosedVerificationResource | PendingVerificationResource,
    Field(discriminator="kind"),
]


class Receipt(_Record):
    schema_version: Literal["host_verification_receipt.v1"] = RECEIPT_SCHEMA
    receipt_id: str = Field(min_length=1)
    subject: VerificationSubject
    worker_lease_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_created_at: float = Field(ge=0)
    source: CommittedSource
    gate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    commands: tuple[str, ...] = Field(min_length=1)
    runtime_identity: str = Field(min_length=1)
    permission_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_outcome: _ProcessOutcome
    execution_end: GateExecutionEnd = AllCommandsCompleted()
    resources: VerificationResourceDisposition = NoExternalVerificationResource()
    started_at: float = Field(ge=0)
    completed_at: float = Field(ge=0)

    @model_validator(mode="after")
    def chronological_observation(self) -> Self:
        if not self.lease_created_at <= self.started_at <= self.completed_at:
            raise ValueError("verification interval must follow lease creation")
        if isinstance(self.execution_end, GateDeadlineExceeded):
            if self.execution_end.started_command_count > len(self.commands):
                raise ValueError("verification cannot start more commands than its registered gate")
            if self.process_outcome == "passed":
                raise ValueError("an exhausted verification deadline cannot certify success")
        return self


@dataclass(frozen=True)
class VerificationPassed:
    receipt_id: str
    captures: tuple[CommandRunCapture, ...]


@dataclass(frozen=True)
class VerificationFailed:
    receipt_id: str
    captures: tuple[CommandRunCapture, ...]


@dataclass(frozen=True)
class VerificationCancelled:
    receipt_id: str
    captures: tuple[CommandRunCapture, ...]


@dataclass(frozen=True)
class VerificationUnavailable:
    reason: str


type VerificationProcessOutcome = (
    VerificationPassed | VerificationFailed | VerificationCancelled | VerificationUnavailable
)


@dataclass(frozen=True)
class VerificationCleanupPending:
    verification: VerificationProcessOutcome
    resource: PendingVerificationResource


type VerificationOutcome = VerificationProcessOutcome | VerificationCleanupPending


@dataclass(frozen=True)
class VerifiedReceiptReference:
    receipt: Receipt
    captures: tuple[ProcessCapture, ...]

    def evidence_content(self) -> str:
        return _json(
            {
                "receipt": self.receipt.model_dump(mode="json"),
                "processes": [capture.model_dump() for capture in self.captures],
            }
        )


@dataclass(frozen=True)
class _ProtectedReceiptObservation:
    receipt: Receipt
    captures: tuple[ProcessCapture, ...]


@dataclass(frozen=True)
class RetainedLegacyObservation:
    reason: str


@dataclass(frozen=True)
class ContradictoryEvidence:
    reason: str


type VerificationEvidence = (
    VerifiedReceiptReference | RetainedLegacyObservation | ContradictoryEvidence
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _gate_digest(project_id: str, commands: tuple[str, ...]) -> str:
    return _digest(
        _json(
            {
                "project": project_id,
                "commands": commands,
                "result_contract": "all_registered_processes_exit_zero.v1",
            }
        ).encode()
    )


def _subject_and_lease(
    intent_id: str, lease_id: str, worker_id: str
) -> tuple[VerificationSubject, BoundLease]:
    with tx() as connection:
        return _locked_subject_and_lease(connection, intent_id, lease_id, worker_id)


def _locked_subject_and_lease(
    connection: ConnectionLike, intent_id: str, lease_id: str, worker_id: str
) -> tuple[VerificationSubject, BoundLease]:
    rows = connection.execute(
        "SELECT cm.stable_key, m.attempt, m.work_unit_id, w.compiled_plan_hash, "
        "d.target_project_id, d.status AS dispatch_status, d.permitted_capabilities, "
        "d.base_commit_sha, w.status AS work_unit_status "
        "FROM milestone_executions m JOIN work_units w ON w.work_unit_id=m.work_unit_id "
        "JOIN compiled_milestones cm ON cm.milestone_id=m.milestone_id "
        "JOIN dispatch_intents d ON d.intent_id=m.dispatch_intent_id "
        "WHERE d.intent_id=? FOR UPDATE OF m, w, d",
        (intent_id,),
    ).fetchall()
    lease_row = connection.execute(
        "SELECT * FROM agent_execution_leases WHERE lease_id=? FOR UPDATE", (lease_id,)
    ).fetchone()
    return _decode_subject_and_lease(rows, lease_row, intent_id, lease_id, worker_id)


def _decode_subject_and_lease(
    rows: list[Any], lease_row: Any, intent_id: str, lease_id: str, worker_id: str
) -> tuple[VerificationSubject, BoundLease]:
    if len(rows) != 1 or lease_row is None:
        raise ValueError("verification requires one durable milestone and an owned execution lease")
    row, lease = rowdict(rows[0]), rowdict(lease_row)
    if DispatchIntentStatus(row["dispatch_status"]) is not DispatchIntentStatus.CLAIMED:
        raise ValueError("verification requires the currently claimed dispatch")
    if WorkUnitStatus(row["work_unit_status"]) in TERMINAL_WORK_UNIT_STATUSES:
        raise ValueError("verification WorkUnit is terminal")
    if not {Capability.READ_REPOSITORY, Capability.RUN_COMMAND}.issubset(
        json.loads(row["permitted_capabilities"])
    ):
        raise ValueError("verification dispatch has no read and command authority")
    if (
        lease["intent_id"] != intent_id
        or lease["worker_id"] != worker_id
        or lease["target_project_id"] != row["target_project_id"]
    ):
        raise ValueError("verification lease does not own this dispatch and project")
    if (
        LeaseStatus(lease["status"]) is not LeaseStatus.ACTIVE
        or lease["cancel_requested_at"] is not None
        or float(lease["lease_expires_at"]) <= now()
    ):
        raise ValueError("verification lease is inactive, cancelled, or expired")
    subject = VerificationSubject(
        intent_id=intent_id,
        target_project_id=row["target_project_id"],
        work_unit_id=row["work_unit_id"],
        milestone_key=row["stable_key"],
        attempt=row["attempt"],
        compiled_plan_hash=row["compiled_plan_hash"],
    )
    return subject, BoundLease(
        lease_id=lease_id,
        worker_id=worker_id,
        created_at=float(lease["created_at"]),
        permission_envelope_sha256=lease["permission_envelope_sha256"],
        declared_base_commit=row["base_commit_sha"],
    )


def verification_subject_for_dispatch(intent_id: str) -> VerificationSubject | None:
    """Resolve the current attempt link without parsing display source strings."""
    with tx() as connection:
        rows = connection.execute(
            "SELECT cm.stable_key, m.attempt, m.work_unit_id, "
            "w.compiled_plan_hash, d.target_project_id "
            "FROM milestone_executions m JOIN work_units w ON w.work_unit_id=m.work_unit_id "
            "JOIN compiled_milestones cm ON cm.milestone_id=m.milestone_id "
            "JOIN dispatch_intents d ON d.intent_id=m.dispatch_intent_id WHERE d.intent_id=?",
            (intent_id,),
        ).fetchall()
    if len(rows) != 1:
        return None
    row = rowdict(rows[0])
    return VerificationSubject(
        intent_id=intent_id,
        target_project_id=row["target_project_id"],
        work_unit_id=row["work_unit_id"],
        milestone_key=row["stable_key"],
        attempt=row["attempt"],
        compiled_plan_hash=row["compiled_plan_hash"],
    )


def _git(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        check=False,
        env=verification_git_environment(),
    )
    if completed.returncode != 0:
        raise ValueError("verification source is not a readable committed Git snapshot")
    return completed.stdout


def _store_git_object(destination: Path, object_id: str, kind: str, content: bytes) -> None:
    framed = f"{kind} {len(content)}\0".encode("ascii") + content
    algorithm = "sha1" if len(object_id) == 40 else "sha256"
    if hashlib.new(algorithm, framed).hexdigest() != object_id:
        raise ValueError("verification Git object bytes do not match their identity")
    path = destination / ".git" / "objects" / object_id[:2] / object_id[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = zlib.compress(framed)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    except FileExistsError:
        # Several paths can name one blob or tree. An installed identity is
        # immutable: reuse its exact bytes rather than reopening it for writing.
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as retained:
                if (
                    not stat.S_ISREG(os.fstat(retained.fileno()).st_mode)
                    or retained.read() != encoded
                ):
                    raise ValueError("existing verification Git object conflicts with its identity")
        except OSError as exc:
            raise ValueError(
                "existing verification Git object is not a safe immutable file"
            ) from exc
    else:
        with os.fdopen(descriptor, "wb") as installed:
            installed.write(encoded)


def _snapshot(repository: Path, commit: str, base: str, destination: Path) -> CommittedSource:
    resolved = _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    tree = _git(repository, "rev-parse", "--verify", f"{resolved}^{{tree}}").decode().strip()
    resolved_base = _git(repository, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    _git(repository, "merge-base", "--is-ancestor", resolved_base, resolved)
    metadata_root = destination / ".git"
    (metadata_root / "refs").mkdir(parents=True)
    (metadata_root / "HEAD").write_text(resolved + "\n")
    (metadata_root / "config").write_text(
        "[core]\n\trepositoryformatversion = "
        + ("1" if len(resolved) == 64 else "0")
        + "\n\tbare = false\n\tfilemode = true\n"
        + ("[extensions]\n\tobjectformat = sha256\n" if len(resolved) == 64 else "")
    )
    # Keep only the exact verified commit/tree, with an explicit shallow boundary.
    # No remote, hooks, credentials, replacement refs or mutable object alternates
    # are copied from the host checkout.
    (metadata_root / "shallow").write_text(resolved + "\n")
    for object_id, kind in ((resolved, "commit"), (tree, "tree")):
        _store_git_object(
            destination, object_id, kind, _git(repository, "cat-file", kind, object_id)
        )
    entries = _git(repository, "ls-tree", "-r", "-t", "-z", resolved).split(b"\0")
    # Read object bytes directly. Export attributes and checkout filters must
    # never remove or transform a test while the receipt names the original tree.
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split(b" ", 2)
        identity = object_id.decode("ascii")
        content = _git(repository, "cat-file", kind.decode("ascii"), identity)
        if kind == b"tree" and mode == b"040000":
            _store_git_object(destination, identity, "tree", content)
            continue
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError("verification snapshots do not admit symlinks or submodules")
        relative = Path(os.fsdecode(raw_name))
        if relative.is_absolute() or any(
            part == ".." or part.casefold() == ".git" for part in relative.parts
        ):
            raise ValueError("verification source contains an unsupported path")
        target = destination / relative
        if target.exists():
            raise ValueError("verification source paths collide on the snapshot filesystem")
        target.parent.mkdir(parents=True, exist_ok=True)
        _store_git_object(destination, identity, "blob", content)
        target.write_bytes(content)
        target.chmod(0o555 if mode == b"100755" else 0o444)
    _git(destination, "read-tree", resolved)
    for path in metadata_root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    manifest = [
        (str(path.relative_to(destination)), _digest(path.read_bytes()))
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    ]
    return CommittedSource(
        commit=resolved,
        tree=tree,
        base=resolved_base,
        manifest_digest=_digest(_json(manifest).encode()),
    )


@dataclass(frozen=True)
class _InstalledToolchain:
    environment: Path
    executables: tuple[Path, ...]
    readable: tuple[Path, ...]

    def gate_environment(self, snapshot: Path, outputs: Path) -> dict[str, str]:
        paths = (self.environment / "bin", *(path.parent for path in self.executables))
        search_paths = dict.fromkeys((*paths, Path("/usr/bin"), Path("/bin")))
        git_prefix = self.executables[1].parent.parent
        return {
            **verification_git_environment(),
            **installed_node_environment(snapshot, self.executables[2]),
            "PATH": os.pathsep.join(map(str, search_paths)),
            "HOME": str(outputs),
            "TMPDIR": str(outputs),
            "XDG_CACHE_HOME": str(outputs / "cache"),
            "UV_CACHE_DIR": str(outputs / "uv-cache"),
            "UV_PROJECT_ENVIRONMENT": str(self.environment),
            "VIRTUAL_ENV": str(self.environment),
            "GIT_EXEC_PATH": str(git_prefix / "libexec" / "git-core"),
            "GIT_TEMPLATE_DIR": str(git_prefix / "share" / "git-core" / "templates"),
            "UV_PYTHON": str(self.environment / "bin" / "python"),
            "UV_OFFLINE": "1",
            "UV_NO_SYNC": "1",
            "UV_NO_ENV_FILE": "1",
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "PYTHONPATH": os.pathsep.join((str(snapshot / "src"), str(snapshot))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": f"-o cache_dir={outputs / 'pytest-cache'}",
            "RUFF_CACHE_DIR": str(outputs / "ruff-cache"),
            "PYRIGHT_PYTHON_GLOBAL_NODE": "1",
            "PYRIGHT_PYTHON_NODEJS_WHEEL": "0",
            "AIDASHOS_VERIFICATION_OUTPUT_DIR": str(outputs),
        }


def _installed_toolchain(
    project_root: Path, *, source_root: Path | None = None
) -> _InstalledToolchain:
    project_venv = project_root / ".venv"
    if project_venv.is_symlink():
        raise ValueError("registered project environment must not be a symlink")
    environment = project_venv if project_venv.is_dir() else Path(sys.prefix)
    node_source = source_root if source_root is not None else project_root
    node_environment = project_environment(node_source)
    roots = (Path(sys.base_prefix), environment)
    if any(path.resolve() in {Path("/"), project_root.resolve()} for path in roots):
        raise ValueError("installed toolchain root would expose mutable source")
    executables: list[Path] = []
    readable = set(roots)
    for name in ("uv", "git", "node"):
        found = (
            shutil.which(name, path=node_environment.get("PATH"))
            if name == "node"
            else shutil.which(name)
        )
        if found is None:
            raise ValueError(f"registered verification tool is unavailable: {name}")
        executable = Path(found).resolve(strict=True)
        if name == "node":
            installed_node_environment(node_source, executable)
        executables.append(executable)
        readable.update(_linked_runtime_files(executable))
        if name == "git":
            result = subprocess.run(
                (str(executable), "--exec-path"),
                capture_output=True,
                text=True,
                check=True,
                env=verification_git_environment(),
            )
            helpers = Path(result.stdout.strip()).resolve(strict=True)
            if not helpers.is_relative_to(executable.parent.parent):
                raise ValueError("Git helpers are outside the installed executable prefix")
            readable.add(helpers)
            templates = executable.parent.parent / "share" / "git-core" / "templates"
            if templates.is_dir():
                readable.add(templates.resolve())
    return _InstalledToolchain(environment.resolve(), tuple(executables), tuple(sorted(readable)))


def _sandbox_policy(
    snapshot: Path,
    outputs: Path,
    toolchain: tuple[Path, ...],
    forbidden: tuple[Path, ...],
    relay_port: int | None = None,
) -> SeatbeltPolicy:
    # The gate may read its frozen installed toolchain, but all writes are confined
    # to an output directory separate from source. Children inherit this profile.
    readable = (
        snapshot,
        outputs,
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/private/etc"),
        Path("/private/var/db/dyld"),
        Path("/private/var/db/timezone"),
        Path("/Library/Apple"),
        *toolchain,
    )
    return SeatbeltPolicy(
        reads=(
            (
                *(PathGrant(path) for path in readable if path.exists()),
                PathGrant(Path("/"), PathScope.EXACT),
                PathGrant(Path("/dev/null"), PathScope.EXACT),
            ),
        ),
        writes=((PathGrant(outputs), PathGrant(Path("/dev/null"), PathScope.EXACT)),),
        outbound=((TcpGrant("localhost", relay_port),) if relay_port is not None else (),),
        forbidden_reads=tuple(PathGrant(path) for path in forbidden),
        forbidden_writes=(PathGrant(snapshot),),
        pty=True,
        fixed_process_group=True,
    )


def _sandbox_profile(
    snapshot: Path, outputs: Path, toolchain: tuple[Path, ...], forbidden: tuple[Path, ...]
) -> str:
    return _sandbox_policy(snapshot, outputs, toolchain, forbidden).render()


@dataclass(frozen=True)
class _GateProcesses:
    captures: tuple[CommandRunCapture, ...]
    execution_end: GateExecutionEnd


def _process_outcome(
    exit_codes: tuple[int, ...], execution_end: GateExecutionEnd
) -> _ProcessOutcome:
    if any(code < 0 for code in exit_codes):
        return "cancelled"
    if isinstance(execution_end, AllCommandsCompleted) and all(code == 0 for code in exit_codes):
        return "passed"
    return "failed"


@dataclass(frozen=True)
class _GateAuthorityInvalidated:
    captures: tuple[CommandRunCapture, ...]
    reason: str


def _run_gate_commands(
    commands: tuple[str, ...],
    snapshot: Path,
    deadline: float,
    subject: VerificationSubject,
    lease: BoundLease,
    broker: NativeVerificationBroker,
) -> _GateProcesses | _GateAuthorityInvalidated:
    captures: list[CommandRunCapture] = []
    for command in commands:
        try:
            current = _subject_and_lease(subject.intent_id, lease.lease_id, lease.worker_id)
        except ValueError as exc:
            return _GateAuthorityInvalidated(tuple(captures), str(exc))
        if current != (subject, lease):
            return _GateAuthorityInvalidated(
                tuple(captures), "verification subject or execution lease changed"
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _GateProcesses(
                tuple(captures), GateDeadlineExceeded(started_command_count=len(captures))
            )
        capture = broker.run(("/bin/sh", "-c", command), snapshot)
        if broker.authority_invalidated:
            return _GateAuthorityInvalidated(
                tuple((*captures, capture)), "verification authority revoked during command"
            )
        captures.append(capture)
        if capture.exit_code == PROCESS_TIMEOUT_EXIT_CODE or monotonic() >= deadline:
            return _GateProcesses(
                tuple(captures), GateDeadlineExceeded(started_command_count=len(captures))
            )
    return _GateProcesses(tuple(captures), AllCommandsCompleted())


@dataclass(frozen=True)
class _ObservedGate:
    source: CommittedSource
    captures: tuple[CommandRunCapture, ...]
    execution_end: GateExecutionEnd
    runtime_identity: str
    started_at: float
    completed_at: float


type _AcquiredResources = VerificationResourcesAbsent | VerificationResourcesPresent


def _resource_redact(resources: _AcquiredResources, text: str) -> str:
    return (
        resources.lease.redact(text)
        if isinstance(resources, VerificationResourcesPresent)
        else text
    )


def _close_resources(resources: _AcquiredResources) -> VerificationResourceDisposition:
    if isinstance(resources, VerificationResourcesAbsent):
        return NoExternalVerificationResource()
    try:
        cleanup = resources.lease.close()
    except Exception:
        return PendingVerificationResource(identity=resources.lease.identity)
    if (
        isinstance(cleanup, VerificationResourceClosed)
        and cleanup.lease_id == resources.lease.identity.lease_id
    ):
        return ClosedVerificationResource(identity=resources.lease.identity)
    return PendingVerificationResource(identity=resources.lease.identity)


def _with_resource_disposition(
    outcome: VerificationProcessOutcome, resources: VerificationResourceDisposition
) -> VerificationOutcome:
    if isinstance(resources, PendingVerificationResource):
        return VerificationCleanupPending(outcome, resources)
    return outcome


def _observe_gate(
    project: LinkedProject,
    subject: VerificationSubject,
    lease: BoundLease,
    source_repository: Path,
    source_commit: str,
    base_commit: str,
    commands: tuple[str, ...],
    deadline: float,
    resources: _AcquiredResources,
) -> _ObservedGate | VerificationUnavailable:
    from .operator_identity import operator_token_file

    started = now()
    if isinstance(resources, VerificationResourcesPresent):
        resource_remaining = (
            datetime.fromisoformat(resources.lease.identity.expires_at).timestamp() - now()
        )
        if resource_remaining <= 0:
            return VerificationUnavailable("verification resource lease has expired")
        deadline = min(deadline, monotonic() + resource_remaining)
    with tempfile.TemporaryDirectory(prefix="host-verification-") as raw:
        root = Path(raw).resolve()
        frozen = root / "source"
        frozen.mkdir()
        source = _snapshot(source_repository, source_commit, base_commit, frozen)
        with UidVerifierClient(
            source_binding=source.manifest_digest,
            deadline=deadline,
            process_limit=256,
        ) as uid_owner:
            snapshot = uid_owner.staging.source
            # Copy as the operator into the helper-created ACL anchor.
            # Root never opens or recursively mutates caller-controlled source paths.
            shutil.copytree(frozen, snapshot, dirs_exist_ok=True, copy_function=shutil.copyfile)
            for original in frozen.rglob("*"):
                if original.is_file():
                    (snapshot / original.relative_to(frozen)).chmod(
                        stat.S_IMODE(original.stat().st_mode)
                    )
            staged = stage_installed_toolchain(
                project.expanded_path,
                uid_owner.staging.toolchain,
                source_root=snapshot,
                public_ca=resources.lease.public_ca
                if isinstance(resources, VerificationResourcesPresent)
                else None,
            )
            try:
                if deadline <= monotonic():
                    return VerificationUnavailable(
                        "verification deadline expired during preparation"
                    )
                toolchain = staged.toolchain
                root_prepared = uid_owner.prepare(parent=None, scratch=None)
                outputs = root_prepared.scratch
                resource_reads = (
                    (staged.public_ca.copied.path,) if staged.public_ca is not None else ()
                )
                policy = replace(
                    _sandbox_policy(
                        snapshot,
                        outputs,
                        (*toolchain.readable, *resource_reads),
                        (operator_token_file(),),
                        resources.lease.identity.relay_port
                        if isinstance(resources, VerificationResourcesPresent)
                        else None,
                    ),
                    # The qualified helper owns detached descendants by an exclusive UID.
                    fixed_process_group=False,
                )
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key in {"LANG", "LC_ALL", "SYSTEMROOT"}
                }
                environment.update(toolchain.gate_environment(snapshot, outputs))
                environment.update(
                    {
                        "HOME": str(root_prepared.home),
                        "TMPDIR": str(root_prepared.scratch),
                        # Cross-UID fixture repositories are confined to this verified anchor.
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "safe.directory",
                        "GIT_CONFIG_VALUE_0": str(uid_owner.staging.directory) + "/*",
                    }
                )
                if isinstance(resources, VerificationResourcesPresent):
                    scoped_environment = resources.lease.environment(staged_ca=staged.public_ca)
                    if set(scoped_environment) != {"LOCAL_AGENT_TEST_DATABASE_URL"}:
                        raise ValueError(
                            "verification resource tried to replace unrelated process authority"
                        )
                    environment.update(scoped_environment)
                if deadline <= monotonic():
                    return VerificationUnavailable(
                        "verification deadline expired during preparation"
                    )

                def authority_valid() -> bool:
                    return _subject_and_lease(
                        subject.intent_id, lease.lease_id, lease.worker_id
                    ) == (
                        subject,
                        lease,
                    )

                with NativeVerificationBroker(
                    policy=policy,
                    snapshot=snapshot,
                    outputs=outputs,
                    environment=environment,
                    deadline=deadline,
                    authority_valid=authority_valid,
                    uid_owner=uid_owner,
                    root_prepared=root_prepared,
                    runtime_dependencies=staged.runtime_dependencies,
                ) as broker:
                    processes = _run_gate_commands(
                        commands, snapshot, deadline, subject, lease, broker
                    )
                nested_processes = broker.records
                if isinstance(processes, _GateAuthorityInvalidated):
                    return VerificationUnavailable(
                        "verification authority invalidated before the next command: "
                        + processes.reason
                    )
                captures = tuple(
                    CommandRunCapture(
                        command=command,
                        cwd=capture.cwd,
                        stdout=_resource_redact(resources, capture.stdout),
                        stderr=_resource_redact(resources, capture.stderr),
                        exit_code=capture.exit_code,
                    )
                    for command, capture in zip(
                        commands[: len(processes.captures)], processes.captures, strict=True
                    )
                )

                return _ObservedGate(
                    source=source,
                    captures=captures,
                    execution_end=processes.execution_end,
                    started_at=started,
                    completed_at=now(),
                    runtime_identity=_json(
                        {
                            "python": sys.version,
                            "python_executable_sha256": _digest(Path(sys.executable).read_bytes()),
                            "platform": platform.platform(),
                            "shell_sha256": _digest(Path("/bin/sh").read_bytes()),
                            "containment_profile_sha256": _digest(broker.policy.render().encode()),
                            "native_processes": nested_processes,
                            "toolchain_provenance_sha256": staged.manifest_digest,
                            "toolchain_provenance_path": str(staged.manifest),
                            "staged_toolchain_disposition": "removed_after_uid_cleanup",
                            "uid_gate_cleanup": uid_owner.close(),
                            "installed_executables": {
                                str(path): _digest(path.read_bytes())
                                for path in toolchain.executables
                            },
                        }
                    ),
                )
            finally:
                uid_owner.close()
                staged.cleanup_after(uid_owner)


def run_registered_verification(
    *,
    intent_id: str,
    lease_id: str,
    worker_id: str,
    source_repository: Path,
    source_commit: str,
    base_commit: str,
    timeout_seconds: int,
) -> VerificationOutcome:
    """Observe registered processes, then separately attest their resource cleanup."""
    try:
        if timeout_seconds <= 0:
            raise ValueError("verification gate requires a positive total execution budget")
        deadline = monotonic() + timeout_seconds
        subject, lease = _subject_and_lease(intent_id, lease_id, worker_id)
        project = load_project_center().project_by_id(subject.target_project_id)
        commands = tuple(project.verification_commands)
        if not commands:
            return VerificationUnavailable("no registered verification gate")
        actual_common = _git(
            source_repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        registered_common = _git(
            project.expanded_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if actual_common.strip() != registered_common.strip():
            return VerificationUnavailable(
                "verification source is outside the registered repository"
            )
        if platform.system() != "Darwin" or not _SANDBOX.is_file():
            return VerificationUnavailable("protected committed snapshots require macOS Seatbelt")
        resources = acquire_verification_resources(subject.target_project_id, source_repository)
        if isinstance(resources, VerificationResourcesRefused):
            return VerificationUnavailable(resources.reason)
        try:
            observed = _observe_gate(
                project,
                subject,
                lease,
                source_repository,
                source_commit,
                base_commit,
                commands,
                deadline,
                resources,
            )
        except (ValueError, OSError, subprocess.SubprocessError, UidVerifierUnavailable) as exc:
            observed = VerificationUnavailable(_resource_redact(resources, str(exc)))
        finally:
            resource_disposition = _close_resources(resources)
        if isinstance(observed, VerificationUnavailable):
            return _with_resource_disposition(
                VerificationUnavailable(_resource_redact(resources, observed.reason)),
                resource_disposition,
            )
        outputs_json = _json(
            [
                ProcessCapture(
                    command=capture.command,
                    cwd=capture.cwd,
                    exit_code=capture.exit_code,
                    stdout=capture.stdout,
                    stderr=capture.stderr,
                ).model_dump()
                for capture in observed.captures
            ]
        )
        outcome = _process_outcome(
            tuple(capture.exit_code for capture in observed.captures), observed.execution_end
        )
        receipt = Receipt(
            receipt_id=str(uuid.uuid4()),
            subject=subject,
            worker_lease_id=lease_id,
            worker_id=worker_id,
            lease_created_at=lease.created_at,
            source=observed.source,
            gate_digest=_gate_digest(subject.target_project_id, commands),
            commands=commands,
            runtime_identity=observed.runtime_identity,
            permission_envelope_sha256=lease.permission_envelope_sha256,
            output_digest=_digest(outputs_json.encode()),
            process_outcome=outcome,
            execution_end=observed.execution_end,
            resources=resource_disposition,
            started_at=observed.started_at,
            completed_at=observed.completed_at,
        )
        payload = receipt.model_dump_json()
        try:
            with tx() as connection:
                current_subject, current_lease = _locked_subject_and_lease(
                    connection, intent_id, lease_id, worker_id
                )
                if current_subject != subject or current_lease != lease:
                    return _with_resource_disposition(
                        VerificationUnavailable("verification ownership changed during execution"),
                        resource_disposition,
                    )
                if (
                    lease.declared_base_commit is not None
                    and lease.declared_base_commit != observed.source.base
                ):
                    return _with_resource_disposition(
                        VerificationUnavailable(
                            "verification source base differs from the dispatch base"
                        ),
                        resource_disposition,
                    )
                connection.execute(
                    "INSERT INTO host_verification_receipts "
                    "(receipt_id, intent_id, payload_json, payload_sha256, "
                    "output_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        receipt.receipt_id,
                        intent_id,
                        payload,
                        _digest(payload.encode()),
                        outputs_json,
                        now(),
                    ),
                )
        except (ValueError, OSError, subprocess.SubprocessError, UidVerifierUnavailable) as exc:
            return _with_resource_disposition(
                VerificationUnavailable(_resource_redact(resources, str(exc))), resource_disposition
            )
        match outcome:
            case "passed":
                result = VerificationPassed(receipt.receipt_id, observed.captures)
            case "failed":
                result = VerificationFailed(receipt.receipt_id, observed.captures)
            case "cancelled":
                result = VerificationCancelled(receipt.receipt_id, observed.captures)
        return _with_resource_disposition(result, resource_disposition)
    except (ValueError, OSError, subprocess.SubprocessError, UidVerifierUnavailable) as exc:
        return VerificationUnavailable(str(exc))


def _resolve_protected_receipt(
    receipt_id: str, expected: VerificationSubject
) -> _ProtectedReceiptObservation | RetainedLegacyObservation | ContradictoryEvidence:
    """Validate protected process history without treating failure as successful proof."""
    with tx() as connection:
        row = connection.execute(
            "SELECT * FROM host_verification_receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
    if row is None:
        return RetainedLegacyObservation("no protected host receipt exists for this reference")
    stored = rowdict(row)
    try:
        if _digest(stored["payload_json"].encode()) != stored["payload_sha256"]:
            raise ValueError("verification receipt payload digest mismatch")
        receipt = Receipt.model_validate_json(stored["payload_json"])
        if (
            receipt.receipt_id != receipt_id
            or stored["intent_id"] != expected.intent_id
            or receipt.subject != expected
        ):
            raise ValueError("verification receipt belongs to a different execution subject")
        if _digest(stored["output_json"].encode()) != receipt.output_digest:
            raise ValueError("verification process output digest mismatch")
        captures = tuple(
            ProcessCapture.model_validate(item) for item in json.loads(stored["output_json"])
        )
        started_count = (
            receipt.execution_end.started_command_count
            if isinstance(receipt.execution_end, GateDeadlineExceeded)
            else len(receipt.commands)
        )
        if tuple(c.command for c in captures) != receipt.commands[:started_count]:
            raise ValueError("verification process captures do not match the registered gate")
        commands = tuple(
            load_project_center().project_by_id(expected.target_project_id).verification_commands
        )
        if _gate_digest(expected.target_project_id, commands) != receipt.gate_digest:
            raise ValueError("registered verification gate changed after execution")
        project = load_project_center().project_by_id(expected.target_project_id)
        if not isinstance(receipt.resources, NoExternalVerificationResource):
            resource = receipt.resources.identity
            current_common = (
                _git(
                    project.expanded_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
                )
                .decode()
                .strip()
            )
            if (
                resource.target_project_id != expected.target_project_id
                or resource.source_common_directory != current_common
            ):
                raise ValueError("verification resource does not belong to the receipt subject")
            if (
                receipt.process_outcome == "passed"
                and receipt.completed_at >= datetime.fromisoformat(resource.expires_at).timestamp()
            ):
                raise ValueError("verification success is outside the resource lease interval")
        current_tree = (
            _git(
                project.expanded_path, "rev-parse", "--verify", f"{receipt.source.commit}^{{tree}}"
            )
            .decode()
            .strip()
        )
        if current_tree != receipt.source.tree:
            raise ValueError("verification source commit no longer identifies its recorded tree")
        observed_outcome = _process_outcome(
            tuple(capture.exit_code for capture in captures), receipt.execution_end
        )
        if receipt.process_outcome != observed_outcome:
            raise ValueError("verification receipt contradicts its retained process outcomes")
        with tx() as connection:
            lease_row = connection.execute(
                "SELECT * FROM agent_execution_leases WHERE lease_id=?", (receipt.worker_lease_id,)
            ).fetchone()
        if lease_row is None:
            raise ValueError("verification worker lease is absent")
        lease = rowdict(lease_row)
        if (
            lease["intent_id"] != expected.intent_id
            or lease["target_project_id"] != expected.target_project_id
            or lease["worker_id"] != receipt.worker_id
            or float(lease["created_at"]) != receipt.lease_created_at
            or lease["permission_envelope_sha256"] != receipt.permission_envelope_sha256
        ):
            raise ValueError("verification worker lease binding changed")
        if (
            receipt.started_at < receipt.lease_created_at
            or receipt.completed_at < receipt.started_at
            or receipt.completed_at >= float(lease["lease_expires_at"])
            or (
                lease["cancel_requested_at"] is not None
                and float(lease["cancel_requested_at"]) <= receipt.completed_at
            )
        ):
            raise ValueError("verification was not observed within the valid worker lease")
        return _ProtectedReceiptObservation(receipt, captures)
    except (ValueError, TypeError, KeyError) as exc:
        return ContradictoryEvidence(str(exc))


def resolve_verification_receipt(
    receipt_id: str, expected: VerificationSubject
) -> VerificationEvidence:
    """Only a protected passing receipt can discharge a verification requirement."""
    observed = _resolve_protected_receipt(receipt_id, expected)
    if not isinstance(observed, _ProtectedReceiptObservation):
        return observed
    if isinstance(observed.receipt.resources, PendingVerificationResource):
        return RetainedLegacyObservation(
            "verification process completed but resource cleanup is unproven"
        )
    if observed.receipt.process_outcome != "passed":
        return RetainedLegacyObservation("host verification did not pass every registered process")
    return VerifiedReceiptReference(observed.receipt, observed.captures)


def resolve_retained_verification_outcome(
    receipt_id: str,
    expected_subject: VerificationSubject,
    expected_lease_id: str,
    expected_source_commit: str,
) -> VerificationOutcome:
    """Replay an exact retained outcome without restarting its processes or lease."""
    try:
        observed = _resolve_protected_receipt(receipt_id, expected_subject)
    except (OSError, subprocess.SubprocessError) as exc:
        return VerificationUnavailable(str(exc))
    if not isinstance(observed, _ProtectedReceiptObservation):
        return VerificationUnavailable(observed.reason)
    if (
        observed.receipt.worker_lease_id != expected_lease_id
        or observed.receipt.source.commit != expected_source_commit
    ):
        return VerificationUnavailable("retained verification belongs to another lease or source")
    captures: list[CommandRunCapture] = []
    for capture in observed.captures:
        if capture.cwd is None:
            return VerificationUnavailable(
                "retained verification has no recorded process directory"
            )
        captures.append(
            CommandRunCapture(
                command=capture.command,
                cwd=capture.cwd,
                stdout=capture.stdout,
                stderr=capture.stderr,
                exit_code=capture.exit_code,
            )
        )
    match observed.receipt.process_outcome:
        case "passed":
            return _with_resource_disposition(
                VerificationPassed(receipt_id, tuple(captures)), observed.receipt.resources
            )
        case "failed":
            return _with_resource_disposition(
                VerificationFailed(receipt_id, tuple(captures)), observed.receipt.resources
            )
        case "cancelled":
            return _with_resource_disposition(
                VerificationCancelled(receipt_id, tuple(captures)), observed.receipt.resources
            )

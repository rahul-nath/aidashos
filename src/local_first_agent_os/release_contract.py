# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Release identity and evidence, independent of transport and runtime startup."""

from __future__ import annotations

import argparse
import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Version = Annotated[str, Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReleaseCheck(StrEnum):
    CLEAN_INSTALL = "clean_install"
    PREVIOUS_CLIENTS = "previous_clients"
    RETAINED_DATA = "retained_data"
    SCHEMA_UPGRADE = "schema_upgrade"
    INTERRUPTED_RECOVERY = "interrupted_recovery"
    ROLLBACK = "rollback"
    REPOSITORY_GATES = "repository_gates"


class ReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateIdentity(ReleaseRecord):
    version: Version
    public_commit: GitCommit
    artifact_sha256: Sha256
    baseline_commit: GitCommit
    baseline_kind: Literal["public_release", "pre_release_commit"]
    environment: Annotated[str, Field(min_length=1)]

    @property
    def tag(self) -> str:
        return f"v{self.version}"


class CheckPassed(ReleaseRecord):
    state: Literal["passed"] = "passed"
    check: ReleaseCheck
    candidate: CandidateIdentity
    evidence_file: Annotated[str, Field(min_length=1)]
    evidence_sha256: Sha256


class CheckMissing(ReleaseRecord):
    state: Literal["missing"] = "missing"
    check: ReleaseCheck
    reason: Annotated[str, Field(min_length=1)]


class CheckFailed(ReleaseRecord):
    state: Literal["failed"] = "failed"
    check: ReleaseCheck
    reason: Annotated[str, Field(min_length=1)]
    evidence_file: Annotated[str, Field(min_length=1)]
    evidence_sha256: Sha256


CheckEvidence = Annotated[CheckPassed | CheckMissing | CheckFailed, Field(discriminator="state")]


class ReleaseEvidence(ReleaseRecord):
    schema_version: Literal["release_evidence.v1"] = "release_evidence.v1"
    candidate: CandidateIdentity
    checks: tuple[CheckEvidence, ...]

    @model_validator(mode="after")
    def complete_inventory(self) -> Self:
        names = [item.check for item in self.checks]
        if len(names) != len(set(names)) or set(names) != set(ReleaseCheck):
            raise ValueError("every release check must occur exactly once")
        if any(
            isinstance(item, CheckPassed) and item.candidate != self.candidate
            for item in self.checks
        ):
            raise ValueError("passed evidence belongs to a different candidate or environment")
        return self

    def require_qualified(self, evidence_root: Path, artifact: Path) -> None:
        """Verify evidence bytes before accepting recorded verdicts; never run publication."""
        unfinished = [item.check.value for item in self.checks if not isinstance(item, CheckPassed)]
        if unfinished:
            raise ValueError(f"release not qualified: {', '.join(unfinished)}")
        if file_sha256(artifact) != self.candidate.artifact_sha256:
            raise ValueError("artifact does not match qualified candidate")
        root = evidence_root.resolve()
        for item in self.checks:
            if not isinstance(item, CheckPassed):
                raise AssertionError("qualification failed to reject an incomplete check")
            path = (root / item.evidence_file).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("evidence must name a file inside the evidence bundle")
            if file_sha256(path) != item.evidence_sha256:
                raise ValueError(f"evidence changed: {item.check.value}")


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a candidate's release evidence bundle.")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    record = ReleaseEvidence.model_validate_json(args.evidence.read_text())
    record.require_qualified(args.evidence.parent, args.artifact)
    print(json.dumps({"state": "qualified", "candidate": record.candidate.model_dump(mode="json")}))


if __name__ == "__main__":
    main()

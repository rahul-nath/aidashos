# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The repo audit as a typed, anchored, falsifiable artifact.

Every dispatched agent re-audits the repository from scratch, because the only
form its predecessor's audit survives in is prose, and prose has no way to say
whether it is still true. This module gives the audit a schema whose whole
design is falsifiability rather than compression:

- A claim carries the file and line span it was read from, the commit it was
  read at, and the *assumption files* whose contents make it true. The
  engineering doctrine already obliges agents to identify those assumptions;
  this is the place they land as data.
- Given the set of files changed since the audit's commit, claims partition
  mechanically: a claim whose anchor and assumptions are untouched is a
  verified pointer the next agent may build on; a claim any changed file
  intersects is demoted to a hypothesis to re-verify; a claim with no anchor
  at all is always a hypothesis, because nothing can invalidate it and
  therefore nothing can vouch for it.

Git supplies the invalidation set for free, which is why anchoring beats
summarizing: a summary's staleness is undetectable, an anchor's is a diff.
The 2026-08-10 staff review modeled the whole protocol by hand - "every claim
read off HEAD 28795c9; this worktree is at 64b42cc" - and re-verified only what
mattered. This schema is that sentence, made durable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

REPO_AUDIT_SCHEMA_VERSION = "repo_audit.v1"

# The fenced block a reading agent emits inside its otherwise-prose output.
# Fenced and language-tagged so extraction never guesses: the tag is the
# schema version, and everything outside the fence stays narrative.
_AUDIT_BLOCK_PATTERN = re.compile(
    r"```repo_audit\.v1\s*\n(?P<body>.*?)```",
    re.DOTALL,
)

_FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")

# The model-facing half of the producer contract. It lives beside the schema it
# describes so the two cannot drift apart silently: the fence tag here is the
# same constant the extractor matches on. Identity and commit are pointedly
# omitted from the example, because the host supplies both and an agent that
# states them is making a claim the extractor will overwrite anyway.
AUDIT_EMISSION_INSTRUCTION = (
    "Repository audit block: end your response with a fenced code block tagged "
    f"{REPO_AUDIT_SCHEMA_VERSION} containing one JSON object of the form\n"
    f"```{REPO_AUDIT_SCHEMA_VERSION}\n"
    '{"claims": [{"claim": "<one falsifiable statement about this repository>",\n'
    '             "file": "<repo-relative path the claim was read from>",\n'
    '             "line_start": 1, "line_end": 2,\n'
    '             "assumption_files": ["<other files whose contents make it true>"]}]}\n'
    "```\n"
    "Replace the placeholders with your actual findings. Anchor every claim you "
    "can to the file and lines it was read from; a claim with no anchor is "
    "always demoted to a hypothesis for your successor. Do not state a commit "
    "sha or project id inside the block - the host records both."
)


def contains_repo_audit_block(agent_output: str) -> bool:
    """Whether a fenced audit block exists at all, before any validation.

    Cheap by design: callers use it to decide whether resolving the host-side
    commit sha is worth a git invocation, so it must not parse or validate.
    """

    return _AUDIT_BLOCK_PATTERN.search(agent_output) is not None


class RepoAuditError(ValueError):
    """A payload that claims to be an audit and is not one."""


@dataclass(frozen=True)
class AuditClaim:
    """One statement about the repository, carrying its own falsifiability.

    ``file`` and the optional line span are where the claim was read.
    ``assumption_files`` are the other files whose contents make it correct -
    the doctrine's assumption tree, as paths. A claim is *anchored* when it
    names at least one file; only anchored claims can survive a diff, because
    only they can be invalidated by one.
    """

    claim: str
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    assumption_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def anchored(self) -> bool:
        return self.file is not None or bool(self.assumption_files)

    @property
    def watched_files(self) -> tuple[str, ...]:
        return tuple(path for path in (self.file, *self.assumption_files) if path)

    def invalidated_by(self, changed_files: Iterable[str]) -> bool:
        changed = set(changed_files)
        return any(path in changed for path in self.watched_files)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"claim": self.claim}
        if self.file is not None:
            payload["file"] = self.file
        if self.line_start is not None:
            payload["line_start"] = self.line_start
        if self.line_end is not None:
            payload["line_end"] = self.line_end
        if self.assumption_files:
            payload["assumption_files"] = list(self.assumption_files)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> AuditClaim:
        claim = payload.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise RepoAuditError("an audit claim requires non-empty 'claim' text")
        file = payload.get("file")
        if file is not None and not isinstance(file, str):
            raise RepoAuditError("'file' must be a path string when present")
        raw_assumptions = payload.get("assumption_files", [])
        if not isinstance(raw_assumptions, list) or any(
            not isinstance(item, str) for item in raw_assumptions
        ):
            raise RepoAuditError("'assumption_files' must be a list of path strings")
        lines: dict[str, int | None] = {}
        for name in ("line_start", "line_end"):
            value = payload.get(name)
            if value is not None and (not isinstance(value, int) or value < 1):
                raise RepoAuditError(f"'{name}' must be a positive line number when present")
            lines[name] = value
        return cls(
            claim=claim.strip(),
            file=file,
            line_start=lines["line_start"],
            line_end=lines["line_end"],
            assumption_files=tuple(raw_assumptions),
        )


@dataclass(frozen=True)
class AuditPartition:
    """The mechanical outcome of holding an audit against a diff."""

    verified: tuple[AuditClaim, ...]
    demoted: tuple[AuditClaim, ...]


@dataclass(frozen=True)
class RepoAudit:
    """An agent's read of one repository at one commit, claim by claim."""

    target_project_id: str
    commit_sha: str
    claims: tuple[AuditClaim, ...]

    def partition(self, changed_files: Iterable[str]) -> AuditPartition:
        """Split claims into still-verified pointers and demoted hypotheses.

        Demotion is deliberately one-way and eager: an unanchored claim demotes
        even against an empty diff, because a claim nothing can invalidate is a
        claim nothing vouches for.
        """

        changed = set(changed_files)
        verified: list[AuditClaim] = []
        demoted: list[AuditClaim] = []
        for claim in self.claims:
            if claim.anchored and not claim.invalidated_by(changed):
                verified.append(claim)
            else:
                demoted.append(claim)
        return AuditPartition(verified=tuple(verified), demoted=tuple(demoted))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": REPO_AUDIT_SCHEMA_VERSION,
            "target_project_id": self.target_project_id,
            "commit_sha": self.commit_sha,
            "claims": [claim.to_payload() for claim in self.claims],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> RepoAudit:
        if payload.get("schema_version") != REPO_AUDIT_SCHEMA_VERSION:
            raise RepoAuditError(
                f"expected schema_version {REPO_AUDIT_SCHEMA_VERSION!r}, "
                f"got {payload.get('schema_version')!r}"
            )
        target = payload.get("target_project_id")
        sha = payload.get("commit_sha")
        if not isinstance(target, str) or not target:
            raise RepoAuditError("an audit requires 'target_project_id'")
        if not isinstance(sha, str) or not _FULL_SHA_PATTERN.match(sha):
            raise RepoAuditError("an audit requires 'commit_sha' as a git sha")
        raw_claims = payload.get("claims")
        if not isinstance(raw_claims, list) or not raw_claims:
            raise RepoAuditError("an audit requires a non-empty 'claims' list")
        return cls(
            target_project_id=target,
            commit_sha=sha,
            claims=tuple(
                AuditClaim.from_payload(item) for item in raw_claims if isinstance(item, dict)
            ),
        )


def extract_repo_audit(
    agent_output: str,
    *,
    target_project_id: str,
    commit_sha: str,
) -> RepoAudit | None:
    """Pull the fenced audit block out of an agent's prose output, if one exists.

    The agent supplies the claims; the *host* supplies identity and commit,
    because the worktree's HEAD is a fact the host already holds and an agent's
    self-reported sha is a claim like any other. A missing block is a normal
    answer - audits accelerate the next agent, they do not gate this one. A
    block that is present but malformed raises, because a wrong audit silently
    dropped would teach agents that the contract is optional in the worst way.
    """

    match = _AUDIT_BLOCK_PATTERN.search(agent_output)
    if match is None:
        return None
    try:
        body = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        raise RepoAuditError(f"repo_audit.v1 block is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise RepoAuditError("repo_audit.v1 block must be a JSON object")
    body.setdefault("schema_version", REPO_AUDIT_SCHEMA_VERSION)
    body["target_project_id"] = target_project_id
    body["commit_sha"] = commit_sha
    return RepoAudit.from_payload(body)


def render_audit_context_block(
    audit: RepoAudit,
    *,
    head_sha: str,
    changed_files: Sequence[str],
) -> str:
    """The prompt block a successor agent receives instead of a cold start.

    Verified claims arrive as pointers with their anchors, ready to be built
    on; demoted claims arrive named as hypotheses with the reason. The block
    states both shas so the receiving agent can widen the diff itself if it
    distrusts the host's file list.
    """

    partition = audit.partition(changed_files)
    lines = [
        f"Prior repository audit, read at {audit.commit_sha}; this worktree is at {head_sha}.",
        f"Files changed since then: {', '.join(sorted(changed_files)) or 'none'}.",
    ]
    if partition.verified:
        lines.append(
            "Verified pointers (anchors untouched by the diff; build on them, "
            "spot-check rather than re-derive):"
        )
        for claim in partition.verified:
            anchor = claim.file or ""
            if claim.line_start is not None:
                anchor += f":{claim.line_start}"
                if claim.line_end is not None and claim.line_end != claim.line_start:
                    anchor += f"-{claim.line_end}"
            suffix = f" [{anchor}]" if anchor else ""
            if claim.assumption_files:
                suffix += f" (assumes {', '.join(claim.assumption_files)})"
            lines.append(f"- {claim.claim}{suffix}")
    if partition.demoted:
        lines.append(
            "Hypotheses (anchor or assumption touched by the diff, or never "
            "anchored; re-verify before relying):"
        )
        for claim in partition.demoted:
            lines.append(f"- {claim.claim}")
    return "\n".join(lines)


__all__ = [
    "AUDIT_EMISSION_INSTRUCTION",
    "REPO_AUDIT_SCHEMA_VERSION",
    "AuditClaim",
    "AuditPartition",
    "RepoAudit",
    "RepoAuditError",
    "contains_repo_audit_block",
    "extract_repo_audit",
    "render_audit_context_block",
]

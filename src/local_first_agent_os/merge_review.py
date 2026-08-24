# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operator-facing review packets for CODE_MERGE approval gates."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import ApprovalRequestType
from .coordination import ListApprovalRequests, ListDispatchIntents
from .coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)
from .dispatch_results import DispatchRunnerResult, normalize_dispatch_runner_result
from .pow_wow.ledger import run_coordination_command
from .project_center import load_project_center
from .review_recovery import (
    DOCTRINE_PROVENANCE_CODES,
    diagnose_staff_review_provenance,
    merge_gate_evidence,
)

_OUTPUT_LIMIT = 4_000


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded(value: object, limit: int = _OUTPUT_LIMIT) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters omitted ...]\n{text[-limit:]}"


def _git(repo: Path, *args: str) -> tuple[str, str | None]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return "", (completed.stderr or completed.stdout).strip()
    return completed.stdout.strip(), None


def _all_artifacts(run_result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts = [_mapping(item) for item in _sequence(run_result.get("artifacts"))]
    for raw_task in _sequence(run_result.get("tasks")):
        task = _mapping(raw_task)
        artifacts.extend(_mapping(item) for item in _sequence(task.get("artifacts")))
    return artifacts


def _checkpoint_rows(run_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_branch: dict[str, dict[str, Any]] = {}
    for artifact in _all_artifacts(run_result):
        if artifact.get("artifact_type") != "worktree_commit_checkpoint":
            continue
        content = _mapping(artifact.get("content"))
        branch = _text(content.get("branch_name"))
        commit = _text(content.get("commit_sha"))
        if not branch or not commit:
            continue
        by_branch[branch] = {
            "task_name": artifact.get("task_name") or content.get("task_name"),
            "branch_name": branch,
            "base_head_sha": content.get("base_head_sha"),
            "commit_sha": commit,
            "commit_created": bool(content.get("commit_created")),
            "changed_from_base": bool(content.get("changed_from_base")),
            "checkpointed_files": list(_sequence(content.get("checkpointed_files"))),
            "error": content.get("error"),
        }
    return [by_branch[key] for key in sorted(by_branch)]


def _patch_rows(run_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in _all_artifacts(run_result):
        if artifact.get("artifact_type") != "code_patch":
            continue
        content = _mapping(artifact.get("content"))
        rows.append(
            {
                "worktree_group": content.get("worktree_group"),
                "base_head_sha": content.get("base_head_sha"),
                "branch_name": content.get("branch_name"),
                "commit_sha": content.get("commit_sha"),
                "byte_size": content.get("byte_size"),
                "truncated": bool(content.get("truncated")),
            }
        )
    return rows


def _review_rows(run_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_task in _sequence(run_result.get("tasks")):
        task = _mapping(raw_task)
        task_name = _text(task.get("task_name"))
        role = _text(task.get("role"))
        is_review = "review" in task_name.casefold() or "review" in role.casefold()
        if not is_review:
            continue
        verdict = ""
        for raw_artifact in _sequence(task.get("artifacts")):
            artifact = _mapping(raw_artifact)
            candidate = _text(_mapping(artifact.get("content")).get("verdict"))
            if candidate:
                verdict = candidate
                break
        rows.append(
            {
                "task_name": task_name,
                "role": role,
                "status": task.get("status"),
                "summary": task.get("summary"),
                "verdict": verdict or None,
                "risks": list(_sequence(task.get("risks"))),
            }
        )
    return rows


def _verification_rows(run_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    commands = [_text(item) for item in _sequence(run_result.get("verification_commands"))]
    output = [_bounded(item) for item in _sequence(run_result.get("verification_output"))]
    rows: list[dict[str, Any]] = []
    count = max(len(commands), len(output))
    for index in range(count):
        rows.append(
            {
                "command": commands[index] if index < len(commands) else None,
                "output": output[index] if index < len(output) else None,
            }
        )
    return rows


def build_merge_review_packet(
    *,
    saga_id: str,
    approval_id: str | None,
    requested_by: str | None,
    intent_id: str | None,
    pow_wow_id: str | None,
    target_project_id: str | None,
    run_result: Mapping[str, Any],
    target_project_path: Path | None = None,
    fallback_changed_files: Sequence[object] = (),
    dispatch_result: DispatchRunnerResult | None = None,
) -> dict[str, Any]:
    """Build a bounded, serializable review packet from durable run evidence."""

    checkpoints = _checkpoint_rows(run_result)
    repo_path_text = _text(run_result.get("target_project_path"))
    repo_path = target_project_path or (Path(repo_path_text) if repo_path_text else None)
    diff_rows: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        base = _text(checkpoint.get("base_head_sha"))
        commit = _text(checkpoint.get("commit_sha"))
        row: dict[str, Any] = {
            "branch_name": checkpoint["branch_name"],
            "base_head_sha": base or None,
            "commit_sha": commit or None,
        }
        if repo_path is not None and base and commit:
            stat, stat_error = _git(repo_path, "diff", "--stat", base, commit)
            names, names_error = _git(repo_path, "diff", "--name-status", base, commit)
            commit_summary, log_error = _git(repo_path, "show", "-s", "--format=%h %s", commit)
            row.update(
                {
                    "diff_stat": stat,
                    "name_status": names,
                    "commit_summary": commit_summary,
                    "error": stat_error or names_error or log_error,
                    "inspect_command": shlex.join(
                        ["git", "-C", str(repo_path), "diff", "--find-renames", base, commit]
                    ),
                }
            )
        else:
            row["error"] = "Target repository or checkpoint range unavailable."
        diff_rows.append(row)

    changed_files = list(_sequence(run_result.get("changed_files")))
    if not changed_files:
        changed_files = list(fallback_changed_files)
    result_origin = (
        dispatch_result.origin if dispatch_result is not None else DispatchResultOrigin.AUTOMATED
    )
    result_state = (
        dispatch_result.state
        if dispatch_result is not None
        else (
            DispatchResultState.COMPLETED
            if run_result.get("status") == "COMPLETED"
            else DispatchResultState.FAILED
        )
    )
    promotion_state = (
        dispatch_result.promotion_state
        if dispatch_result is not None
        else (
            DispatchPromotionState.MERGE_PENDING
            if changed_files
            else DispatchPromotionState.RESULT_RECORDED
        )
    )
    return {
        "schema_version": "merge_review_packet.v1",
        "approval_id": approval_id,
        "saga_id": saga_id,
        "requested_by": requested_by,
        "intent_id": intent_id,
        "pow_wow_id": pow_wow_id or run_result.get("pow_wow_id"),
        "target_project_id": target_project_id or run_result.get("target_project_id"),
        "target_project_path": str(repo_path) if repo_path is not None else None,
        "dispatch_result_origin": result_origin.value,
        "dispatch_result_state": result_state.value,
        "promotion_state": promotion_state.value,
        "executor_status": run_result.get("status"),
        "summary": run_result.get("output_summary"),
        "changed_files": changed_files,
        "checkpoints": checkpoints,
        "diffs": diff_rows,
        "patches": _patch_rows(run_result),
        "verification": _verification_rows(run_result),
        "reviews": _review_rows(run_result),
        "risks": list(_sequence(run_result.get("risks"))),
        "approval_is_separate": True,
    }


def _project_path(settings: Any, project_id: str | None) -> Path | None:
    if not project_id:
        return None
    try:
        return load_project_center(settings).project_by_id(project_id).expanded_path
    except (FileNotFoundError, KeyError, ValueError):
        return None


def review_packet_for_approval(
    approval: Mapping[str, Any],
    *,
    settings: Any,
) -> dict[str, Any]:
    """Return a stored packet or hydrate a legacy approval from its intent result."""

    payload = _mapping(approval.get("payload"))
    stored = payload.get("review_packet")
    if isinstance(stored, Mapping):
        packet = dict(stored)
        packet["approval_id"] = approval.get("approval_id")
        return packet

    intent_id = _text(payload.get("intent_id")) or None
    intent: Mapping[str, Any] = {}
    if intent_id:
        intents = run_coordination_command(
            ListDispatchIntents(), timeout=15, settings=settings
        ).get("intents", [])
        intent = next(
            (_mapping(item) for item in intents if _mapping(item).get("intent_id") == intent_id),
            {},
        )
    dispatch_result = normalize_dispatch_runner_result(
        intent_result=intent.get("result"),
        approval_payload=payload,
    )
    run_result = dispatch_result.run_result
    target_project_id = (
        _text(payload.get("target_project_id"))
        or _text(intent.get("target_project_id"))
        or _text(run_result.get("target_project_id"))
        or None
    )
    return build_merge_review_packet(
        saga_id=_text(approval.get("saga_id")),
        approval_id=_text(approval.get("approval_id")) or None,
        requested_by=_text(approval.get("requested_by")) or None,
        intent_id=intent_id,
        pow_wow_id=_text(payload.get("pow_wow_id")) or None,
        target_project_id=target_project_id,
        run_result=run_result,
        target_project_path=_project_path(settings, target_project_id),
        fallback_changed_files=_sequence(payload.get("changed_files")),
        dispatch_result=dispatch_result,
    )


def pending_code_merge_approval(
    *, settings: Any, approval_id: str | None = None
) -> Mapping[str, Any]:
    requests = run_coordination_command(
        ListApprovalRequests(status="PENDING"), timeout=15, settings=settings
    ).get("requests", [])
    matches = [
        _mapping(item)
        for item in requests
        if _mapping(item).get("request_type") == ApprovalRequestType.CODE_MERGE.value
        and (approval_id is None or _mapping(item).get("approval_id") == approval_id)
    ]
    if not matches:
        suffix = f" {approval_id}" if approval_id else ""
        raise ValueError(f"No pending CODE_MERGE approval{suffix} was found.")
    return max(
        matches,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("approval_id") or "")),
    )


def require_staff_review_provenance(
    approval: Mapping[str, Any],
    *,
    settings: Any,
) -> None:
    """Reject merge approval unless host-stamped staff evidence approved it."""

    payload = _mapping(approval.get("payload"))
    intent_result: object = None
    intent_id = _text(payload.get("intent_id"))
    if intent_id:
        intents = run_coordination_command(
            ListDispatchIntents(), timeout=15, settings=settings
        ).get("intents", [])
        intent = next(
            (_mapping(item) for item in intents if _mapping(item).get("intent_id") == intent_id),
            {},
        )
        intent_result = intent.get("result")
    dispatch_result = normalize_dispatch_runner_result(
        intent_result=intent_result,
        approval_payload=payload,
    )
    if dispatch_result.promotion_state is not DispatchPromotionState.MERGE_PENDING:
        raise ValueError(
            "CODE_MERGE is not approvable: typed promotion state is "
            f"{dispatch_result.promotion_state.value}, expected MERGE_PENDING"
        )
    evidence = merge_gate_evidence(dispatch_result.run_result)
    final = evidence.final_review
    if final is None:
        raise ValueError(
            "CODE_MERGE is not approvable without host-stamped review_result.v1 evidence"
        )
    expected_commit = _text(payload.get("commit_sha")) or _text(
        evidence.checkpoint.get("commit_sha")
    )
    expected_base = _text(payload.get("base_sha")) or _text(
        evidence.checkpoint.get("base_head_sha")
    )
    if not (expected_commit and expected_base):
        raise ValueError(
            "CODE_MERGE is not approvable: neither the approval payload nor the "
            "retained checkpoint names the reviewed commit and base"
        )
    issue = diagnose_staff_review_provenance(
        final,
        evidence.reviews[:-1],
        {
            "commit_sha": expected_commit,
            "base_head_sha": expected_base,
        },
    )
    if issue is None:
        return
    message = f"CODE_MERGE is not approvable: {issue.code.value}: {issue.message}."
    if issue.code in DOCTRINE_PROVENANCE_CODES:
        message += (
            " Run `agent-ledger list_doctrine_stale_reviews` to see every pending "
            "review in this state and the recovery commands; the operator procedure "
            "is documented in docs/doctrine_bump_recovery.md."
        )
    raise ValueError(message)


def render_merge_review_packet(packet: Mapping[str, Any]) -> str:
    lines = [
        "CODE_MERGE review packet",
        f"Approval: {packet.get('approval_id') or 'unknown'}",
        f"Saga: {packet.get('saga_id') or 'unknown'}",
        f"Intent: {packet.get('intent_id') or 'not recorded'}",
        f"Pow-wow: {packet.get('pow_wow_id') or 'not recorded'}",
        f"Target: {packet.get('target_project_id') or 'unknown'}",
        f"Result origin: {packet.get('dispatch_result_origin') or 'unknown'}",
        f"Result state: {packet.get('dispatch_result_state') or 'unknown'}",
        f"Promotion state: {packet.get('promotion_state') or 'unknown'}",
        f"Executor status: {packet.get('executor_status') or 'unknown'}",
        "",
        "Summary:",
        _text(packet.get("summary")) or "No run summary was recorded.",
        "",
        "Changed files:",
    ]
    changed = [_text(item) for item in _sequence(packet.get("changed_files"))]
    lines.extend(f"- {item}" for item in changed if item)
    if not any(changed):
        lines.append("- None recorded")

    for index, raw_diff in enumerate(_sequence(packet.get("diffs")), start=1):
        diff = _mapping(raw_diff)
        lines.extend(
            [
                "",
                (
                    f"Diff {index}: {diff.get('base_head_sha') or '?'}.."
                    f"{diff.get('commit_sha') or '?'}"
                ),
                _text(diff.get("commit_summary")) or "Commit summary unavailable.",
                "Stat:",
                _text(diff.get("diff_stat")) or "No diff stat available.",
                "Name/status:",
                _text(diff.get("name_status")) or "No name/status data available.",
            ]
        )
        if diff.get("error"):
            lines.append(f"Diff warning: {diff['error']}")

    lines.extend(["", "Verification:"])
    verification = list(_sequence(packet.get("verification")))
    if verification:
        for raw_row in verification:
            row = _mapping(raw_row)
            lines.append(f"- {row.get('command') or 'recorded output'}")
            if row.get("output"):
                lines.append(_bounded(row["output"]))
    else:
        lines.append("- No verification evidence recorded")

    lines.extend(["", "Reviewer verdicts:"])
    reviews = list(_sequence(packet.get("reviews")))
    if reviews:
        for raw_review in reviews:
            review = _mapping(raw_review)
            lines.append(
                f"- {review.get('task_name') or review.get('role')}: "
                f"{review.get('status') or 'unknown'}"
            )
            lines.append(_bounded(review.get("verdict")) or _text(review.get("summary")))
    else:
        lines.append("- No independent reviewer result recorded")

    lines.extend(["", "Residual risks:"])
    risks = [_text(item) for item in _sequence(packet.get("risks"))]
    lines.extend(f"- {item}" for item in risks if item)
    if not any(risks):
        lines.append("- None recorded")

    inspect_commands = [
        _text(_mapping(item).get("inspect_command")) for item in _sequence(packet.get("diffs"))
    ]
    if any(inspect_commands):
        lines.extend(["", "Inspect the full diff:"])
        lines.extend(command for command in inspect_commands if command)
    lines.extend(
        [
            "",
            "Viewing this packet did not approve or merge anything.",
            f"Approve only after review: pi /approve-merge {packet.get('approval_id')}",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "build_merge_review_packet",
    "pending_code_merge_approval",
    "require_staff_review_provenance",
    "render_merge_review_packet",
    "review_packet_for_approval",
]

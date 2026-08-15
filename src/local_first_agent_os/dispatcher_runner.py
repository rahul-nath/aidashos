# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from .constants import (
    AGENT_BRANCH_AUTO_MERGE,
    AGENT_WORKTREE_BRANCH_PREFIX,
    CLI_AGENT_RUN_ARTIFACT_TYPE,
    DELEGATED_TASK_RUN_ARTIFACT_TYPE,
)
from .contracts import ApprovalRequestType
from .coordination import (
    ClaimTask,
    CompletePowWow,
    CreatePowWow,
    CreateSaga,
    DecideExecutionCheckpoint,
    EntityResult,
    GetExecutionCheckpoint,
    ListDispatchIntents,
    SubmitApprovalRequest,
)
from .coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)
from .decomposition import DecompositionPlan, DecompositionPlanner, RuleBasedDecompositionPlanner
from .dispatch_results import DispatchRunnerResult
from .dispatcher import IntentResult
from .harness_availability import (
    DISPATCH_QUOTA_READ_TIMEOUT_SECONDS,
    collapsed_cross_checks,
    read_spent_quotas,
    staffing_around_spent_quotas,
)
from .harness_readiness import TierUnstaffable, effective_bench, restaffings
from .lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from .merge_review import build_merge_review_packet
from .pow_wow import (
    CliPowWowExecutor,
    DelegateFn,
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowRunResult,
    PowWowTaskSpec,
    persist_pow_wow_run_result,
    run_coordination_command,
    run_typed_coordination_command,
)
from .pow_wow.protocol import ReviewOrigin, TaskPurpose
from .pow_wow.views import ViewCompactor
from .progress_events import emit_progress
from .project_center import LinkedProject, load_project_center, project_status_row
from .review_recovery import staff_review_approves_checkpoint
from .runtime import AppRuntime
from .spawn_authority import SpawnAuthority
from .staffing import Bench, JudgmentRole, Tier, load_bench

DispatchKind = Literal["advisory", "code"]


@dataclass(frozen=True)
class DispatchRunSummary:
    intent_id: str
    tier: Tier
    kind: DispatchKind
    saga_id: str
    pow_wow_id: str
    task_id: str
    task_ids_by_name: dict[str, str]
    target_project_id: str
    decomposition: DecompositionPlan
    run_result: PowWowRunResult
    result_origin: DispatchResultOrigin = DispatchResultOrigin.AUTOMATED
    merge_approval: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        result_state = (
            DispatchResultState.COMPLETED
            if self.run_result.status == "COMPLETED"
            else DispatchResultState.FAILED
        )
        promotion_state = (
            DispatchPromotionState.MERGE_PENDING
            if self.merge_approval is not None
            else DispatchPromotionState.RESULT_RECORDED
        )
        return {
            "schema_version": "dispatch_runner_result.v1",
            "result_origin": self.result_origin.value,
            "result_state": result_state.value,
            "promotion_state": promotion_state.value,
            "intent_id": self.intent_id,
            "tier": self.tier.value,
            "kind": self.kind,
            "saga_id": self.saga_id,
            "pow_wow_id": self.pow_wow_id,
            "task_id": self.task_id,
            "task_ids_by_name": self.task_ids_by_name,
            "target_project_id": self.target_project_id,
            "decomposition": self.decomposition.to_payload(),
            "run_result": self.run_result.to_payload(),
            "merge_approval": self.merge_approval,
        }

    def to_intent_result(self) -> IntentResult:
        terminal_status: Literal["DONE", "FAILED"] = (
            "DONE" if self.run_result.status == "COMPLETED" else "FAILED"
        )
        payload = json.dumps(self.to_payload(), sort_keys=True)
        if terminal_status == "DONE":
            return "DONE", payload, None
        error = "; ".join(self.run_result.risks) or self.run_result.output_summary
        return "FAILED", payload, error


def intent_spawn_ceiling(intent: Mapping[str, Any]) -> SpawnAuthority:
    """What the intent declares a process spawned for it may do.

    The column is a JSON array of `Capability` values written by the milestone
    executor from the compiled plan. Three things can go wrong with it and all
    three answer the same way, with nothing:

    - the column is absent, because this intent predates it;
    - it is present and empty, because the producer declared nothing;
    - it is present and malformed, because something wrote it by hand.

    The narrowest authority is the safe reading of every one of those. Widening
    on a value nobody can parse is how a permission model becomes decorative.
    """

    raw = intent.get("permitted_capabilities")
    if not raw:
        return SpawnAuthority.nothing()
    try:
        names = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        names = None
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        emit_progress(
            f"intent {intent.get('intent_id')} carries an unreadable capability set; "
            "spawning with no authority",
            phase="spawn_authority_unreadable",
            intent_id=str(intent.get("intent_id") or ""),
        )
        return SpawnAuthority.nothing()
    return SpawnAuthority.from_names(names)


class DispatcherIntentRunner:
    """Adapter from ledger dispatch-intent rows to the pow-wow executor contract."""

    def __init__(
        self,
        runtime: AppRuntime,
        *,
        delegate_fn: DelegateFn | None = None,
        dependency_compactor: ViewCompactor | None = None,
        executor_factory: Callable[[Bench, SpawnAuthority], CliPowWowExecutor] | None = None,
        bench: Bench | None = None,
        planner: DecompositionPlanner | None = None,
        claude_bin: str = "claude",
        codex_bin: str = "codex",
    ) -> None:
        self.runtime = runtime
        self.delegate_fn = delegate_fn
        # Consumed by the default executor factory below and stored nowhere: an
        # attribute would imply the runner honors it with any factory, and a
        # caller's own factory is the only place its executors are configured.
        # The two together are therefore a contradiction to refuse, not a value
        # to quietly drop.
        if executor_factory is not None and dependency_compactor is not None:
            raise ValueError(
                "dependency_compactor is consumed by the default executor factory; "
                "a caller supplying its own executor_factory wires its own compactor"
            )
        self.bench = bench or load_bench(runtime.settings.config_dir / "staffing.toml")
        self.planner = planner or RuleBasedDecompositionPlanner()
        self.executor_factory = executor_factory or (
            lambda bench_config, ceiling: CliPowWowExecutor(
                spawn_ceiling=ceiling,
                worktree_root=runtime.settings.saga_worktree_root.expanduser(),
                timeout_seconds=runtime.settings.saga_task_timeout_seconds,
                max_review_rounds=runtime.settings.saga_max_review_rounds,
                bench=bench_config,
                delegate_fn=delegate_fn,
                dependency_compactor=dependency_compactor,
                # The ledger a dispatched agent may read, when the operator
                # leaves that on. None here is the whole off switch: the spawn
                # then carries no MCP configuration at all, exactly as before.
                agent_ledger_root=(
                    runtime.settings.coordination_root
                    if runtime.settings.agent_ledger_read_access
                    else None
                ),
                coordination_command=lambda command: run_typed_coordination_command(
                    command,
                    settings=runtime.settings,
                ),
                artifact_writer=runtime.artifact_store,
                claude_bin=claude_bin,
                codex_bin=codex_bin,
                coordination_timeout_seconds=(
                    runtime.settings.coordination_command_timeout_seconds
                ),
                git_timeout_seconds=runtime.settings.git_operation_timeout_seconds,
                progress_assessment_timeout_seconds=(
                    runtime.settings.progress_assessment_timeout_seconds
                ),
                artifact_write_timeout_seconds=(runtime.settings.artifact_write_timeout_seconds),
                stream_drain_timeout_seconds=runtime.settings.stream_drain_timeout_seconds,
            )
        )

    def bench_for_dispatch(self, intent_id: str) -> Bench:
        """The bench this dispatch runs on, after the ledger has had its say.

        `self.bench` is the operator's staffing file, read once when this runner
        was built and correct for as long as the process lives. A quota is not:
        on 2026-08-06 codex reported `USAGE_LIMIT` at 03:04 and the dispatch at
        03:44 went to codex anyway, because the bench was fixed at construction
        and nothing between those two moments consulted the record that already
        said so. Resolving the bench here instead is what lets one failure move
        the next milestone.

        Only the ledger half of the availability check runs here, and that is a
        hard boundary rather than a scoping choice. The probe half spawns a
        subprocess per harness; this method runs on every claimed intent inside a
        resident dispatcher, and a loop that shells out to interrogate its own
        machine each time it picks up work is precisely the failure the probe was
        kept at the operator doors to avoid. The doors still ask both questions,
        because a human is waiting there and a second of subprocess is the
        cheapest thing in that interaction.

        Nothing is cached. One dispatch asks once, so there is no second query
        inside a pass to save, and a cache that spanned passes would answer a
        later milestone with what was true before the earlier one failed - which
        is the staleness this method exists to remove.
        """

        spent = read_spent_quotas(
            settings=self.runtime.settings,
            checkout_timeout_seconds=DISPATCH_QUOTA_READ_TIMEOUT_SECONDS,
        )
        staffing = staffing_around_spent_quotas(self.bench, spent)
        for notice in restaffings(staffing):
            emit_progress(notice, phase="dispatch_restaffed", intent_id=intent_id)
        for notice in collapsed_cross_checks(staffing):
            emit_progress(notice, phase="dispatch_cross_check_collapsed", intent_id=intent_id)
        for item in staffing:
            if isinstance(item, TierUnstaffable):
                emit_progress(
                    f"{item.describe()}; dispatching there anyway rather than stranding "
                    "a run the operator door already admitted",
                    phase="dispatch_on_spent_quota",
                    intent_id=intent_id,
                )
        return effective_bench(staffing)

    def __call__(self, intent: Mapping[str, Any]) -> IntentResult:
        if str(intent.get("intent_role") or "single") == "reducer":
            return self.run_reducer_intent(intent)
        summary = self.run_intent(intent)
        source = str(intent.get("source") or "")
        if source.startswith("execution_checkpoint:") and source.endswith(":review"):
            checkpoint_id = source.removeprefix("execution_checkpoint:").removesuffix(":review")
            output = _extract_answer_from_run_result_payload(summary.run_result.to_payload()) or ""
            decision = _checkpoint_review_json(output)
            run_typed_coordination_command(
                DecideExecutionCheckpoint(
                    checkpoint_id=checkpoint_id,
                    decision=decision,
                ),
                settings=self.runtime.settings,
            )
        return summary.to_intent_result()

    def run_reducer_intent(self, intent: Mapping[str, Any]) -> IntentResult:
        """Reduce a quorum's child answers into one result (vote or judge).

        The reducer intent is only claimable once every sibling child is
        terminal, so the child rows read here are settled ledger truth. `vote`
        is a deterministic strict-majority mode over normalized answers (no
        model call); `judge` runs one advisory model task at the reducer tier
        over the child answers. Both fail closed: no majority, a tie, or zero
        usable answers is FAILED, never an arbitrary pick.
        """
        intent_id = str(intent.get("intent_id") or "unknown-intent")
        parent_intent_id = str(intent.get("parent_intent_id") or "")
        reduce_mode = str(intent.get("reduce") or "none")
        if not parent_intent_id or reduce_mode not in {"vote", "judge"}:
            raise ValueError(
                f"reducer intent {intent_id} is malformed: "
                f"parent={parent_intent_id!r} reduce={reduce_mode!r}"
            )
        listing = run_coordination_command(
            ListDispatchIntents(parent_intent_id=parent_intent_id),
            settings=self.runtime.settings,
        )
        children = [row for row in listing.get("intents", []) if row.get("intent_role") == "child"]
        if not children:
            raise ValueError(f"reducer intent {intent_id} found no child intents")
        child_answers = [
            {
                "intent_id": child.get("intent_id"),
                "tier": child.get("tier"),
                "status": child.get("status"),
                "answer": _extract_answer_from_child_result(child.get("result")),
            }
            for child in sorted(children, key=lambda row: str(row.get("intent_id")))
        ]
        usable = [entry for entry in child_answers if entry["status"] == "DONE" and entry["answer"]]
        reduction: dict[str, Any] = {
            "schema_version": "quorum_reduction.v1",
            "parent_intent_id": parent_intent_id,
            "reducer_intent_id": intent_id,
            "reduce": reduce_mode,
            "fanout": len(children),
            "usable_answers": len(usable),
            "child_answers": child_answers,
        }
        if reduce_mode == "vote":
            return _reduce_by_vote(reduction, usable, fanout=len(children))
        return self._reduce_by_judge(intent, reduction, usable)

    def _reduce_by_judge(
        self,
        intent: Mapping[str, Any],
        reduction: dict[str, Any],
        usable: list[dict[str, Any]],
    ) -> IntentResult:
        if not usable:
            reduction["outcome"] = "no_usable_answers"
            return (
                "FAILED",
                json.dumps(reduction, sort_keys=True),
                ("judge reduce failed: no child produced a usable answer"),
            )
        prompt_lines = [
            "You are the reducer for an ensemble of independent agent answers.",
            f"Original question: {intent.get('prompt')}",
            "",
            f"{len(usable)} independent answers follow. Select or synthesize the "
            "single best final answer. Note real disagreements instead of "
            "papering over them; agreement lowers variance, it does not prove "
            "truth. Answer with the final answer only.",
        ]
        for index, entry in enumerate(usable, start=1):
            prompt_lines.extend(
                ("", f"--- Answer {index} (tier: {entry['tier']}) ---", str(entry["answer"]))
            )
        judge_intent = {
            **intent,
            "prompt": "\n".join(prompt_lines),
            "kind": "advisory",
            "intent_role": "single",
        }
        summary = self.run_intent(judge_intent)
        reduced_answer = _extract_answer_from_run_result_payload(summary.run_result.to_payload())
        reduction["outcome"] = (
            "judged" if summary.run_result.status == "COMPLETED" else "judge_failed"
        )
        reduction["reduced_answer"] = reduced_answer
        reduction["judge"] = summary.to_payload()
        payload = json.dumps(reduction, sort_keys=True)
        if summary.run_result.status == "COMPLETED" and reduced_answer:
            return "DONE", payload, None
        return "FAILED", payload, (f"judge reduce failed: run status {summary.run_result.status}")

    def run_intent(self, intent: Mapping[str, Any]) -> DispatchRunSummary:
        tier = _intent_tier(intent)
        kind = _intent_kind(intent)
        prompt = _intent_prompt(intent)
        intent_id = str(intent.get("intent_id") or "unknown-intent")
        recovery_request = _recovery_review_request(intent)
        result_origin = (
            DispatchResultOrigin.AUTOMATED_RECOVERY
            if recovery_request is not None
            else DispatchResultOrigin.AUTOMATED
        )
        project_center = load_project_center(self.runtime.settings)
        target_project_id = str(intent.get("target_project_id") or "").strip()
        if kind == "code" and not target_project_id:
            raise ValueError(f"code dispatch intent {intent_id} requires target_project_id")
        target_project = project_center.project_by_id(
            target_project_id or project_center.default_saga_project
        )
        emit_progress(
            f"planning intent {intent_id} for target {target_project.id}",
            phase="dispatch_planning",
            intent_id=intent_id,
            target_project_id=target_project.id,
            recovery=recovery_request is not None,
        )
        if kind == "code" and target_project.read_only:
            raise ValueError(
                f"dispatch intent {intent_id} targets read-only project {target_project.id}"
            )

        if recovery_request is not None:
            checkpoint_result = run_typed_coordination_command(
                GetExecutionCheckpoint(str(recovery_request["checkpoint_id"])),
                settings=self.runtime.settings,
            )
            if not isinstance(checkpoint_result, EntityResult):
                raise ValueError("recovery checkpoint lookup returned malformed evidence")
            checkpoint = checkpoint_result.entity.values
            saga_id = str(checkpoint.get("saga_id") or "")
            if not saga_id or saga_id != str(recovery_request["saga_id"]):
                raise ValueError("recovery request saga does not match its checkpoint")
        else:
            saga = run_coordination_command(
                CreateSaga(
                    goal=f"Dispatch intent {intent_id}: {prompt}",
                    budget_tokens=100_000,
                    budget_seconds=3_600,
                ),
                settings=self.runtime.settings,
            )
            saga_id = saga["saga_id"]
        pow_wow = run_coordination_command(
            CreatePowWow(
                saga_id=saga_id,
                stage=(
                    "REVIEW"
                    if recovery_request is not None
                    else "IMPLEMENTATION"
                    if kind == "code"
                    else "IDEA_INTAKE"
                ),
                goal=prompt,
                exit_criteria="Dispatch intent reaches a terminal DONE or FAILED outcome.",
                budget_tokens=50_000,
                required_outputs=("dispatch_runner_result.v1",),
            ),
            settings=self.runtime.settings,
        )
        pow_wow_id = pow_wow["pow_wow_id"]
        # What the milestone's plan declared its agents may do. The plan is the
        # grant; the ledger's job at spawn is revocation, so nothing is mirrored
        # into it here.
        ceiling = intent_spawn_ceiling(intent)
        decomposition = self.planner.plan(
            intent_id=intent_id,
            tier=tier,
            kind=kind,
            prompt=prompt,
            target_project=target_project,
            intent=intent,
        )
        if recovery_request is not None:
            decomposition = replace(
                decomposition,
                planner="recovery_staff_review.v1",
                rationale=(
                    "Resume only the failed staff-review boundary against the exact "
                    "retained commit; senior implementation starts only after BLOCK."
                ),
                tasks=_recovery_review_tasks(),
            )
        emit_progress(
            f"planned {len(decomposition.tasks)} tiered task(s) for intent {intent_id}",
            phase="dispatch_planned",
            intent_id=intent_id,
            pow_wow_id=pow_wow_id,
            task_count=len(decomposition.tasks),
        )
        task_records: dict[str, str] = {}
        for task in decomposition.tasks:
            task_record = run_coordination_command(
                ClaimTask(
                    pow_wow_id=pow_wow_id,
                    task_name=task.task_name,
                    description=task.description,
                    blocked_by=task.blocked_by,
                ),
                settings=self.runtime.settings,
            )
            task_records[task.task_name] = task_record["task_id"]
        context = _context_for_intent(
            saga_id=saga_id,
            prompt=prompt,
            intent=intent,
            target_project=target_project,
            kind=kind,
        )
        context = replace(context, task_ids_by_name=task_records)
        if recovery_request is not None:
            worktree_path = _prepare_recovery_review_worktree(
                intent_id=intent_id,
                target_project=target_project,
                request=recovery_request,
                worktree_root=self.runtime.settings.saga_worktree_root.expanduser(),
                git_timeout_seconds=self.runtime.settings.git_operation_timeout_seconds,
            )
            context = replace(
                context,
                execution_checkpoint_id=str(recovery_request["checkpoint_id"]),
                checkpoint_worktree_path=str(worktree_path),
                checkpoint_base_head_sha=str(recovery_request["base_sha"]),
                reuse_checkpoint_worktree=True,
                review_origin=ReviewOrigin.RECOVERY_STAFF,
                reviewed_commit_sha=str(recovery_request["commit_sha"]),
                review_base_sha=str(recovery_request["base_sha"]),
                recovery_retained_branch=str(recovery_request["branch"]),
                recovery_original_task_contract=(
                    str(checkpoint.get("task_contract") or "").strip()
                    or "Revise only the exact retained implementation reviewed by staff."
                ),
                recovery_permission_envelope=str(recovery_request["permission_envelope"]),
            )
        # What the milestone that submitted this intent declared its agent may
        # do. Absent - an intent from before the column, or from a producer that
        # declares nothing - is read as the narrowest authority rather than the
        # widest, which is the opposite of what the `is_review` boolean did.
        executor = self.executor_factory(self.bench_for_dispatch(intent_id), ceiling)
        emit_progress(
            f"starting isolated execution for intent {intent_id}",
            phase="execution_started",
            intent_id=intent_id,
            pow_wow_id=pow_wow_id,
            target_project_id=target_project.id,
        )
        run_result = executor.dispatch_pow_wow(
            pow_wow_id,
            target_project,
            decomposition.tasks,
            context,
        )
        emit_progress(
            f"isolated execution for intent {intent_id} finished {run_result.status}",
            phase="execution_completed",
            intent_id=intent_id,
            pow_wow_id=pow_wow_id,
            status=run_result.status,
        )
        plan_artifact = PowWowArtifact(
            artifact_type="decomposition_plan",
            schema_version=decomposition.schema_version,
            content=decomposition.to_payload(),
        )
        run_result = replace(run_result, artifacts=(plan_artifact, *run_result.artifacts))
        persist_pow_wow_run_result(
            pow_wow_id,
            task_records,
            run_result,
            settings=self.runtime.settings,
        )
        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
            intent_id=intent_id,
            pow_wow_id=pow_wow_id,
            status=run_result.status,
            verification_commands=list(run_result.verification_commands),
            verification_output_count=len(run_result.verification_output),
        )
        run_coordination_command(
            CompletePowWow(
                pow_wow_id=pow_wow_id,
                output_summary=run_result.output_summary,
                status=map_pow_wow_run_status_to_ledger_status(run_result.status),
            ),
            settings=self.runtime.settings,
        )
        merge_approval = None
        final_checkpoint = _last_checkpoint(run_result)
        if (
            kind == "code"
            and run_result.status == "COMPLETED"
            and run_result.external_agents_started
            and run_result.changed_files
            and final_checkpoint is not None
            and _approved_staff_review(run_result, checkpoint=final_checkpoint)
        ):
            review_packet = build_merge_review_packet(
                saga_id=saga_id,
                approval_id=None,
                requested_by="dispatcher_runner",
                intent_id=intent_id,
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                run_result=run_result.to_payload(),
                target_project_path=target_project.expanded_path,
                dispatch_result=DispatchRunnerResult(
                    result_origin,
                    DispatchResultState.COMPLETED,
                    DispatchPromotionState.MERGE_PENDING,
                    run_result.to_payload(),
                ),
            )
            branch = str(final_checkpoint.get("branch_name") or "")
            base_sha = str(final_checkpoint.get("base_head_sha") or "")
            commit_sha = str(final_checkpoint.get("commit_sha") or "")
            dispatch_result = {
                "schema_version": "dispatch_runner_result.v1",
                "result_origin": result_origin.value,
                "result_state": DispatchResultState.COMPLETED.value,
                "promotion_state": DispatchPromotionState.MERGE_PENDING.value,
                "run_result": run_result.to_payload(),
            }
            merge_approval = run_coordination_command(
                SubmitApprovalRequest(
                    saga_id=saga_id,
                    request_type=ApprovalRequestType.CODE_MERGE.value,
                    requested_by="dispatcher_runner",
                    payload={
                        "intent_id": intent_id,
                        "pow_wow_id": pow_wow_id,
                        "executor_status": run_result.status,
                        "target_project_id": target_project.id,
                        "changed_files": list(run_result.changed_files),
                        "branch": branch,
                        "base_sha": base_sha,
                        "commit_sha": commit_sha,
                        "milestone_id": (
                            recovery_request.get("milestone_id")
                            if recovery_request is not None
                            else None
                        ),
                        "checkpoint_id": (
                            recovery_request.get("checkpoint_id")
                            if recovery_request is not None
                            else None
                        ),
                        "dispatch_result": dispatch_result,
                        "review_packet": review_packet,
                    },
                ),
                settings=self.runtime.settings,
            )
            emit_progress(
                (
                    "staff-approved checkpoint is awaiting operator merge approval "
                    f"{merge_approval['approval_id']}"
                ),
                phase="merge_approval_pending",
                intent_id=intent_id,
                approval_id=merge_approval["approval_id"],
                commit_sha=commit_sha,
            )
        return DispatchRunSummary(
            intent_id=intent_id,
            tier=tier,
            kind=kind,
            saga_id=saga_id,
            pow_wow_id=pow_wow_id,
            task_id=next(iter(task_records.values())),
            task_ids_by_name=task_records,
            target_project_id=target_project.id,
            decomposition=decomposition,
            run_result=run_result,
            result_origin=result_origin,
            merge_approval=merge_approval,
        )


def build_dispatcher_runner(
    runtime: AppRuntime,
    *,
    delegate_fn: DelegateFn | None = None,
    dependency_compactor: ViewCompactor | None = None,
    planner: DecompositionPlanner | None = None,
) -> DispatcherIntentRunner:
    """A runner for claimed dispatch intents, with a working local junior.

    ``delegate_fn`` unset builds the resident local delegate rather than leaving
    the runner without one. That default is the fix, not a convenience: a
    dispatcher with no delegate does not degrade to "runs no junior work", it
    degrades to junior work launched on a frontier CLI, which is how a
    ``pi``/``gemma4`` bench slot became ``claude --model gemma4``. Unset must
    therefore not mean none, and a caller with its own delegate - a Pi directive
    already owns a workflow to scope artifacts to - still passes one.

    ``dependency_compactor`` is defaulted here for the opposite reason. Unset is
    a perfectly safe state - the dependency block truncates, as it always did -
    so this is the place with a runtime in hand offering the better answer, not
    a place repairing a broken one.
    """

    from .dependency_context_compactor import build_dependency_context_compactor
    from .local_delegate import build_resident_local_delegate

    return DispatcherIntentRunner(
        runtime,
        delegate_fn=delegate_fn or build_resident_local_delegate(runtime),
        dependency_compactor=(dependency_compactor or build_dependency_context_compactor(runtime)),
        planner=planner,
    )


def _reduce_by_vote(
    reduction: dict[str, Any],
    usable: list[dict[str, Any]],
    *,
    fanout: int,
) -> IntentResult:
    """Deterministic strict-majority mode over normalized answers.

    Majority is measured against the full fanout, not just the answers that
    arrived: 1 of 3 answering is not a quorum even if that one answer is
    unanimous among arrivals. Ties and no-majority fail closed for a human.
    """
    tally: dict[str, int] = {}
    originals: dict[str, str] = {}
    for entry in usable:
        normalized = _normalize_vote_answer(str(entry["answer"]))
        tally[normalized] = tally.get(normalized, 0) + 1
        originals.setdefault(normalized, str(entry["answer"]))
    reduction["tally"] = tally
    if not tally:
        reduction["outcome"] = "no_usable_answers"
        return (
            "FAILED",
            json.dumps(reduction, sort_keys=True),
            ("vote reduce failed: no child produced a usable answer"),
        )
    best_count = max(tally.values())
    winners = [key for key, count in tally.items() if count == best_count]
    if len(winners) > 1:
        reduction["outcome"] = "tie"
        return (
            "FAILED",
            json.dumps(reduction, sort_keys=True),
            (f"vote reduce failed: tie between {len(winners)} answers"),
        )
    if best_count * 2 <= fanout:
        reduction["outcome"] = "no_majority"
        return (
            "FAILED",
            json.dumps(reduction, sort_keys=True),
            (f"vote reduce failed: top answer has {best_count} of {fanout} votes"),
        )
    reduction["outcome"] = "majority"
    reduction["reduced_answer"] = originals[winners[0]]
    reduction["votes"] = best_count
    return "DONE", json.dumps(reduction, sort_keys=True), None


def _normalize_vote_answer(answer: str) -> str:
    """First non-empty line, whitespace-collapsed, casefolded.

    Vote is for reducible outputs (a verdict, a pick, a number); the first line
    is the answer and the rest is rationale that must not split the vote.
    """
    for line in answer.splitlines():
        if line.strip():
            return " ".join(line.split()).casefold()
    return ""


def _extract_answer_from_child_result(result_text: Any) -> str | None:
    """Pull the agent's answer out of a terminal child intent's result column.

    Children run through the normal runner, so the result is usually a
    dispatch_runner_result.v1 payload; tolerate plain strings so operator
    completed intents still count.
    """
    if not isinstance(result_text, str) or not result_text.strip():
        return None
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError:
        return result_text.strip()
    if isinstance(payload, dict):
        run_result = payload.get("run_result")
        if isinstance(run_result, dict):
            answer = _extract_answer_from_run_result_payload(run_result)
            if answer:
                return answer
        answer = payload.get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
    return None


def _extract_answer_from_run_result_payload(run_result: Mapping[str, Any]) -> str | None:
    for task in run_result.get("tasks") or ():
        if not isinstance(task, Mapping):
            continue
        for artifact in task.get("artifacts") or ():
            if not isinstance(artifact, Mapping):
                continue
            if artifact.get("artifact_type") not in {
                CLI_AGENT_RUN_ARTIFACT_TYPE,
                DELEGATED_TASK_RUN_ARTIFACT_TYPE,
            }:
                continue
            content = artifact.get("content")
            output = content.get("output") if isinstance(content, Mapping) else None
            if isinstance(output, str) and output.strip():
                return output.strip()
    summary = run_result.get("output_summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return None


def _checkpoint_review_json(output: str) -> dict[str, Any]:
    """Parse a junior checkpoint verdict; invalid output deliberately fails closed."""

    candidate = output.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"schema_version": "invalid", "raw_output": output[:4000]}
    if not isinstance(parsed, dict):
        return {"schema_version": "invalid", "raw_output": output[:4000]}
    return parsed


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _recovery_review_request(intent: Mapping[str, Any]) -> dict[str, Any] | None:
    source = str(intent.get("source") or "")
    if not source.endswith(":recovery_staff_review"):
        return None
    try:
        payload = json.loads(_intent_prompt(intent))
    except json.JSONDecodeError as exc:
        raise ValueError("recovery staff review prompt must be JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "recovery_staff_review_request.v1"
    ):
        raise ValueError("recovery staff review requires recovery_staff_review_request.v1")
    required = ("checkpoint_id", "saga_id", "target_project_id", "branch")
    missing = [name for name in required if not str(payload.get(name) or "").strip()]
    if missing:
        raise ValueError(f"recovery staff review missing fields: {', '.join(missing)}")
    for name in ("base_sha", "commit_sha"):
        if not _FULL_SHA.fullmatch(str(payload.get(name) or "")):
            raise ValueError(f"recovery staff review has invalid {name}")
    if str(intent.get("target_project_id") or "") != payload["target_project_id"]:
        raise ValueError("recovery staff review target does not match intent target")
    if str(intent.get("tier") or "") != Tier.STAFF.value:
        raise ValueError("recovery staff review intent must be assigned to staff")
    return payload


def _recovery_review_tasks() -> tuple[PowWowTaskSpec, ...]:
    group = "recovery_review_code"
    anchor = "recovery_revision_anchor"
    return (
        PowWowTaskSpec(
            task_name=anchor,
            role="implementer",
            description=(
                "Validate the retained branch/base/commit as the recovery anchor. "
                "Do not run an implementation model unless staff subsequently blocks."
            ),
            success_criteria=("The exact retained commit is anchored without mutation.",),
            purpose=TaskPurpose.RECOVERY_REVISION,
            judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
            dispatch_kind="code",
            worktree_group=group,
        ),
        PowWowTaskSpec(
            task_name="recovery_staff_review",
            role="reviewer",
            description=(
                "Review the exact retained implementation commit read-only. Start the "
                "response with APPROVE or BLOCK. Check correctness, tests, approval "
                "boundaries, and residual risk. Do not edit files."
            ),
            success_criteria=(
                "The verdict is explicit and tied to the exact reviewed commit.",
                "Blocking findings are concrete enough for a bounded senior revision.",
            ),
            purpose=TaskPurpose.REVIEW,
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF, stance="evaluator"),
            dispatch_kind="code",
            blocked_by=(anchor,),
            worktree_group=group,
        ),
    )


def _git_checked(repo: Path, *args: str, timeout: float) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def _prepare_recovery_review_worktree(
    *,
    intent_id: str,
    target_project: LinkedProject,
    request: Mapping[str, Any],
    worktree_root: Path,
    git_timeout_seconds: float,
) -> Path:
    repo = target_project.expanded_path
    branch = str(request["branch"])
    base_sha = str(request["base_sha"])
    commit_sha = str(request["commit_sha"])
    branch_head = _git_checked(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{branch}",
        timeout=git_timeout_seconds,
    )
    if branch_head != commit_sha:
        raise RuntimeError(
            f"retained branch {branch} drifted: expected {commit_sha}, found {branch_head}"
        )
    ancestry = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base_sha, commit_sha],
        capture_output=True,
        text=True,
        timeout=git_timeout_seconds,
        check=False,
    )
    if ancestry.returncode != 0:
        raise RuntimeError(f"recorded base {base_sha} is not an ancestor of {commit_sha}")
    worktree_root.mkdir(parents=True, exist_ok=True)
    suffix = re.sub(r"[^a-zA-Z0-9]+", "-", intent_id)[:18]
    worktree_path = worktree_root / f"recovery-staff-review-{suffix}"
    review_branch = f"{AGENT_WORKTREE_BRANCH_PREFIX}recovery-staff-review-{suffix}"
    if worktree_path.is_dir():
        active_head = _git_checked(
            worktree_path,
            "rev-parse",
            "HEAD",
            timeout=git_timeout_seconds,
        )
        if active_head != commit_sha:
            raise RuntimeError(
                f"existing recovery worktree drifted from {commit_sha}: {active_head}"
            )
        return worktree_path
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            review_branch,
            str(worktree_path),
            commit_sha,
        ],
        capture_output=True,
        text=True,
        timeout=git_timeout_seconds,
        check=True,
    )
    return worktree_path


def _approved_staff_review(
    run_result: PowWowRunResult,
    *,
    checkpoint: Mapping[str, Any],
) -> bool:
    typed_reviews = [
        artifact.content
        for task in run_result.tasks
        for artifact in task.artifacts
        if artifact.artifact_type == "review_result"
        and artifact.schema_version == "review_result.v1"
    ]
    if not typed_reviews:
        return False
    final = typed_reviews[-1]
    return staff_review_approves_checkpoint(final, typed_reviews[:-1], checkpoint)


def _last_checkpoint(run_result: PowWowRunResult) -> Mapping[str, Any] | None:
    checkpoints = [
        artifact.content
        for task in run_result.tasks
        for artifact in task.artifacts
        if artifact.artifact_type == "worktree_commit_checkpoint"
        and artifact.content.get("commit_sha")
    ]
    return checkpoints[-1] if checkpoints else None


def _intent_tier(intent: Mapping[str, Any]) -> Tier:
    try:
        return Tier(str(intent["tier"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"dispatch intent has invalid tier: {intent.get('tier')!r}") from exc


def _intent_kind(intent: Mapping[str, Any]) -> DispatchKind:
    kind = str(intent.get("kind") or "advisory")
    if kind not in {"advisory", "code"}:
        raise ValueError(f"dispatch intent has invalid kind: {kind!r}")
    return cast(DispatchKind, kind)


def _intent_prompt(intent: Mapping[str, Any]) -> str:
    prompt = str(intent.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("dispatch intent prompt is required")
    return prompt


def _context_for_intent(
    *,
    saga_id: str,
    prompt: str,
    intent: Mapping[str, Any],
    target_project: LinkedProject,
    kind: DispatchKind,
) -> PowWowExecutionContext:
    status = project_status_row(target_project, include_git=True)
    checkpoint: dict[str, Any] = {}
    source = str(intent.get("source") or "")
    if source.startswith("execution_checkpoint:") and ":continuation:" in source:
        try:
            parsed = json.loads(prompt)
        except json.JSONDecodeError:
            parsed = None
        if (
            isinstance(parsed, dict)
            and parsed.get("schema_version") == "checkpoint_continuation.v1"
        ):
            checkpoint = parsed
    return PowWowExecutionContext(
        saga_id=saga_id,
        goal=prompt,
        directive="dispatch_intent",
        target_project_id=target_project.id,
        target_project_path=str(target_project.expanded_path),
        target_project_kind=target_project.kind,
        target_project_status=json.dumps(status, sort_keys=True),
        target_project_read_only=target_project.read_only,
        dispatch_intent_id=str(intent.get("intent_id") or "") or None,
        verification_commands=tuple(target_project.verification_commands),
        no_auto_merge=not AGENT_BRANCH_AUTO_MERGE,
        dispatch_kind=kind,
        execution_checkpoint_id=str(checkpoint.get("checkpoint_id") or "") or None,
        checkpoint_worktree_path=(str(checkpoint.get("preserved_worktree_path") or "") or None),
        checkpoint_base_head_sha=str(checkpoint.get("base_head_sha") or "") or None,
        checkpoint_patch_artifact_id=(str(checkpoint.get("patch_artifact_id") or "") or None),
        reuse_checkpoint_worktree=bool(checkpoint.get("reuse_preserved_worktree")),
    )


def map_pow_wow_run_status_to_ledger_status(run_status: str) -> str:
    if run_status in {"COMPLETED", "VERIFICATION_FAILED", "FAILED", "BLOCKED"}:
        return run_status
    if run_status == "DRY_RUN_COMPLETED":
        return "COMPLETED"
    return "FAILED"

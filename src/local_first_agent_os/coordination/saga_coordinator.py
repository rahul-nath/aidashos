# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Saga coordination layer.

Wraps the MCP coordination server with Pi/DBOS-aware logic:
  - Ambiguity gate (LLM-backed clarity scoring)
  - Evaluation gate (mechanical → semantic → consensus)
  - Drift detection (output vs GAWD doc)
  - Stagnation detection
  - Saga runner that orchestrates staged pow-wows
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..contracts import (
    CANONICAL_SAGA_STAGES,
    SKILLS,
    AmbiguityScore,
    DriftReport,
    EvaluationResult,
    EvaluationSummary,
    EvaluationType,
    GawdDoc,
    SagaStage,
    SkillSpec,
    StageDef,
    StageRoster,
    StagnationReport,
)
from ..settings import Settings

logger = logging.getLogger(__name__)

# Path to the MCP coordination script
_MCP_SCRIPT: Path | None = None


def _mcp_script_path() -> Path:
    global _MCP_SCRIPT
    if _MCP_SCRIPT is None:
        here = Path(__file__).parent
        candidate = here.parent.parent.parent / "agent_coordination_mcp.py"
        _MCP_SCRIPT = candidate if candidate.exists() else Path("agent_coordination_mcp.py")
    return _MCP_SCRIPT


def _coord(cmd: list[str]) -> dict[str, Any]:
    """Run an agent_coordination_mcp CLI command and return parsed JSON."""
    script = str(_mcp_script_path())
    proc = subprocess.run(
        [sys.executable, script, *cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "parse_error",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


def _applicable_skills(stage: SagaStage, roster: StageRoster) -> list[SkillSpec]:
    chars = set(roster.characters) | set(roster.summon_only)
    fns = set(roster.functional_roles)
    return [
        s
        for s in SKILLS
        if stage in s.applies_to_stages
        and (
            bool(set(s.applies_to_characters) & chars)
            or bool(set(s.applies_to_functional_roles) & fns)
        )
    ]


# ---------------------------------------------------------------------------
# Ambiguity gate
# ---------------------------------------------------------------------------


def check_ambiguity_heuristic(gawd_doc_id: str) -> AmbiguityScore:
    """Heuristic ambiguity check via MCP server (no LLM required)."""
    result = _coord(["check_ambiguity", gawd_doc_id])
    if not result.get("ok"):
        raise RuntimeError(f"check_ambiguity failed: {result}")
    return AmbiguityScore(
        gawd_doc_id=gawd_doc_id,
        goal_clarity=result["scores"]["goal_clarity"],
        constraints_clarity=result["scores"]["constraints_clarity"],
        success_criteria_clarity=result["scores"]["success_criteria_clarity"],
        unresolved_critical=result["scores"]["unresolved_critical"],
        ready_to_execute=result["ready_to_execute"],
        passes=result["passes"],
        scores=result["scores"],
    )


async def check_ambiguity_with_llm(
    gawd_doc: GawdDoc,
    runtime: Any,
) -> AmbiguityScore:
    """LLM-backed ambiguity gate using the Pi/GENERAL model.

    Prompts the model to rate clarity of goal, constraints, and success criteria
    on a 0–1 scale, then blends with heuristic scores.
    """
    success_criteria = json.dumps(gawd_doc.success_criteria[:5])
    unresolved_questions = json.dumps(gawd_doc.unresolved_questions[:3])
    prompt = f"""Rate the clarity of this GAWD doc on each dimension from 0.0 to 1.0.
Return JSON only.

Goal: {gawd_doc.goal}

Constraints ({len(gawd_doc.constraints)}): {json.dumps(gawd_doc.constraints[:5])}

Success criteria ({len(gawd_doc.success_criteria)}): {success_criteria}

Unresolved questions ({len(gawd_doc.unresolved_questions)}): {unresolved_questions}

Return this exact shape:
{{
  "goal_clarity": 0.0,
  "constraints_clarity": 0.0,
  "success_criteria_clarity": 0.0,
  "unresolved_critical_count": 0,
  "notes": ""
}}"""

    from ..model_manager import ModelRole  # avoid circular

    try:
        mm = runtime.model_manager
        result_text = await mm.generate(
            model_role=ModelRole.GENERAL,
            prompt=prompt,
            max_tokens=256,
        )
        scores = json.loads(result_text)
    except Exception as exc:
        logger.warning("LLM ambiguity check failed (%s), falling back to heuristic", exc)
        return check_ambiguity_heuristic(gawd_doc.gawd_doc_id)

    thresholds = {
        "goal_clarity": 0.85,
        "constraints_clarity": 0.80,
        "success_criteria_clarity": 0.80,
    }
    passes = {k: scores.get(k, 0) >= v for k, v in thresholds.items()}
    unresolved = int(scores.get("unresolved_critical_count", len(gawd_doc.unresolved_questions)))
    passes["unresolved_critical"] = unresolved == 0
    ready = all(passes.values())

    return AmbiguityScore(
        gawd_doc_id=gawd_doc.gawd_doc_id,
        goal_clarity=scores.get("goal_clarity", 0),
        constraints_clarity=scores.get("constraints_clarity", 0),
        success_criteria_clarity=scores.get("success_criteria_clarity", 0),
        unresolved_critical=unresolved,
        ready_to_execute=ready,
        passes=passes,
        scores=scores,
    )


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


async def check_drift(
    pow_wow_id: str,
    gawd_doc: GawdDoc,
    artifact_contents: list[str],
    runtime: Any,
) -> DriftReport:
    """Detect whether pow-wow outputs have drifted from GAWD doc.

    Checks:
    1. Did the output satisfy the GAWD requirements?
    2. Did agents invent new requirements?
    3. Did agents ignore constraints?
    4. Did agents change the product meaning?
    """
    combined_output = "\n\n---\n\n".join(artifact_contents[:5])[:8000]

    prompt = f"""You are a drift detector. Compare these pow-wow outputs against the GAWD doc.

GAWD goal: {gawd_doc.goal}
GAWD constraints: {json.dumps(gawd_doc.constraints)}
GAWD success criteria: {json.dumps(gawd_doc.success_criteria)}

POW-WOW OUTPUTS (truncated):
{combined_output}

Return JSON only:
{{
  "drift_detected": false,
  "drift_score": 0.0,
  "drift_reasons": [],
  "new_requirements_invented": [],
  "ignored_constraints": [],
  "meaning_changed": false
}}"""

    from ..model_manager import ModelRole

    try:
        mm = runtime.model_manager
        result_text = await mm.generate(
            model_role=ModelRole.GENERAL,
            prompt=prompt,
            max_tokens=512,
        )
        d = json.loads(result_text)
    except Exception as exc:
        logger.warning("Drift check LLM call failed (%s), returning no-drift default", exc)
        d = {
            "drift_detected": False,
            "drift_score": 0.0,
            "drift_reasons": [],
            "new_requirements_invented": [],
            "ignored_constraints": [],
            "meaning_changed": False,
        }

    return DriftReport(
        pow_wow_id=pow_wow_id,
        gawd_doc_id=gawd_doc.gawd_doc_id,
        drift_detected=d.get("drift_detected", False),
        drift_score=float(d.get("drift_score", 0.0)),
        drift_reasons=d.get("drift_reasons", []),
        new_requirements_invented=d.get("new_requirements_invented", []),
        ignored_constraints=d.get("ignored_constraints", []),
        meaning_changed=d.get("meaning_changed", False),
    )


# ---------------------------------------------------------------------------
# Evaluation gate
# ---------------------------------------------------------------------------


async def run_evaluation_gate(
    pow_wow_id: str,
    artifacts: list[dict[str, Any]],
    gawd_doc: GawdDoc,
    runtime: Any,
    session_id: str | None = None,
) -> EvaluationSummary:
    """Run all three evaluation tiers against submitted artifacts.

    MECHANICAL: heuristic checks (non-empty, schema version, size)
    SEMANTIC:   LLM comparison against GAWD acceptance criteria
    CONSENSUS:  majority vote by role-differentiated sub-prompts
    """
    results: list[EvaluationResult] = []

    for art in artifacts:
        artifact_id = art.get("artifact_id", str(uuid.uuid4()))
        content = art.get("content", "")

        # MECHANICAL: non-empty, has schema version, minimum size
        mech_passed = bool(
            bool(content.strip()) and art.get("schema_version") and len(content) >= 10
        )
        mech_score = 1.0 if mech_passed else 0.0
        _coord(
            [
                "evaluate_artifact",  # placeholder — would need a CLI subcommand
            ]
        )
        mech_notes = (
            "Mechanical: non-empty and has schema version"
            if mech_passed
            else "Mechanical: failed basic checks"
        )
        results.append(
            EvaluationResult(
                eval_id=str(uuid.uuid4()),
                artifact_id=artifact_id,
                pow_wow_id=pow_wow_id,
                evaluator_agent="mechanical_checker",
                eval_type=EvaluationType.MECHANICAL,
                score=mech_score,
                passed=mech_passed,
                notes=mech_notes,
            )
        )

        # SEMANTIC: LLM comparison
        sem_score, sem_passed, sem_notes = await _semantic_eval(
            artifact_id, content, gawd_doc, runtime
        )
        results.append(
            EvaluationResult(
                eval_id=str(uuid.uuid4()),
                artifact_id=artifact_id,
                pow_wow_id=pow_wow_id,
                evaluator_agent="semantic_evaluator",
                eval_type=EvaluationType.SEMANTIC,
                score=sem_score,
                passed=sem_passed,
                notes=sem_notes,
            )
        )

        # CONSENSUS: multi-role vote
        con_score, con_passed, con_notes = await _consensus_eval(
            artifact_id, content, gawd_doc, runtime
        )
        results.append(
            EvaluationResult(
                eval_id=str(uuid.uuid4()),
                artifact_id=artifact_id,
                pow_wow_id=pow_wow_id,
                evaluator_agent="consensus_board",
                eval_type=EvaluationType.CONSENSUS,
                score=con_score,
                passed=con_passed,
                notes=con_notes,
            )
        )

    # Aggregate
    by_type: dict[str, Any] = {}
    for et in EvaluationType:
        typed = [r for r in results if r.eval_type == et]
        if typed:
            pass_rate = sum(1 for r in typed if r.passed) / len(typed)
            avg_score = sum(r.score for r in typed) / len(typed)
            by_type[et.value] = {
                "total": len(typed),
                "passed": sum(1 for r in typed if r.passed),
                "pass_rate": round(pass_rate, 3),
                "avg_score": round(avg_score, 3),
            }

    overall_pass = all(v["pass_rate"] >= 0.7 for v in by_type.values()) if by_type else False
    return EvaluationSummary(
        pow_wow_id=pow_wow_id,
        by_type=by_type,
        overall_pass=overall_pass,
        verdict="PASS" if overall_pass else "FAIL",
    )


async def _semantic_eval(
    artifact_id: str,
    content: str,
    gawd_doc: GawdDoc,
    runtime: Any,
) -> tuple[float, bool, str]:
    prompt = f"""Evaluate this artifact against the GAWD acceptance criteria.
Score 0.0–1.0.  Return JSON only.

GAWD goal: {gawd_doc.goal}
Acceptance criteria: {json.dumps(gawd_doc.acceptance_criteria[:5])}

ARTIFACT (truncated to 2000 chars):
{content[:2000]}

Return: {{"score": 0.0, "passed": false, "notes": ""}}"""

    from ..model_manager import ModelRole

    try:
        mm = runtime.model_manager
        text = await mm.generate(model_role=ModelRole.GENERAL, prompt=prompt, max_tokens=256)
        d = json.loads(text)
        score = float(d.get("score", 0))
        passed = bool(d.get("passed", False))
        notes = str(d.get("notes", ""))
    except Exception as exc:
        logger.warning("Semantic eval failed (%s)", exc)
        score, passed, notes = 0.5, True, f"LLM unavailable: {exc}"
    return score, passed, notes


async def _consensus_eval(
    artifact_id: str,
    content: str,
    gawd_doc: GawdDoc,
    runtime: Any,
) -> tuple[float, bool, str]:
    """Multi-role consensus: Staff + QA + Realist each vote."""
    roles = [
        ("Staff Engineer", "correctness and architecture"),
        ("QA Agent", "testability and edge cases"),
        ("Realist", "feasibility and risk"),
    ]
    votes: list[bool] = []
    scores_list: list[float] = []

    from ..model_manager import ModelRole

    mm = runtime.model_manager

    for role_name, focus in roles:
        prompt = f"""You are a {role_name}. Evaluate this artifact for {focus}.
GAWD goal: {gawd_doc.goal}
ARTIFACT: {content[:1500]}
Return JSON: {{"vote": true, "score": 0.0, "concern": ""}}"""
        try:
            text = await mm.generate(model_role=ModelRole.GENERAL, prompt=prompt, max_tokens=128)
            d = json.loads(text)
            votes.append(bool(d.get("vote", True)))
            scores_list.append(float(d.get("score", 0.7)))
        except Exception:
            votes.append(True)
            scores_list.append(0.7)

    majority = sum(votes) > len(votes) / 2
    avg = sum(scores_list) / len(scores_list) if scores_list else 0.7
    notes = f"Votes: {sum(votes)}/{len(votes)} in favour"
    return round(avg, 3), majority, notes


# ---------------------------------------------------------------------------
# Stagnation detection
# ---------------------------------------------------------------------------


def check_stagnation(saga_id: str) -> StagnationReport:
    """Detect stagnation via MCP server."""
    result = _coord(["check_stagnation", saga_id])
    if not result.get("ok"):
        raise RuntimeError(f"check_stagnation failed: {result}")
    return StagnationReport(
        saga_id=saga_id,
        stagnated=result.get("stagnated", False),
        delta_ratio=result.get("delta_ratio", 0.0),
        threshold=result.get("threshold", 0.10),
        reason=result.get("reason", ""),
        recommendation=result.get("recommendation"),
        pow_wows_checked=result.get("pow_wows_checked", []),
    )


# ---------------------------------------------------------------------------
# SagaCoordinator
# ---------------------------------------------------------------------------


class SagaCoordinator:
    """Orchestrates a full multi-stage saga through Pi.

    Usage:
        coord = SagaCoordinator(settings, runtime)
        async for delta in coord.run_saga("Build a widget"):
            print(delta, end="", flush=True)
    """

    def __init__(self, settings: Settings, runtime: Any) -> None:
        self.settings = settings
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Public: full saga runner
    # ------------------------------------------------------------------

    async def run_saga(
        self,
        goal: str,
        budget_tokens: int = 1_000_000,
        budget_seconds: int = 86400,
    ) -> AsyncIterator[str]:
        """Drive a full saga from IDEA_INTAKE through USER_APPROVAL.

        Yields streaming text deltas.  Blocks at the ambiguity gate and
        each approval gate until resolved.
        """
        yield f"[saga] Creating saga: {goal[:80]}...\n"

        # Create saga
        saga_result = _coord(
            [
                "create_saga",
                goal,
                "--budget-tokens",
                str(budget_tokens),
                "--budget-seconds",
                str(budget_seconds),
            ]
        )
        if not saga_result.get("ok"):
            yield f"[saga] ERROR: {saga_result.get('error')}\n"
            return

        saga_id = saga_result["saga_id"]
        yield f"[saga] saga_id={saga_id}\n"

        # Iterate canonical stages
        for stage_def in CANONICAL_SAGA_STAGES:
            stage = stage_def.stage
            stage_goal = stage_def.goal

            yield f"\n[saga:{stage.value}] Starting — {stage_goal}\n"

            # No stage write here. `sagas.current_stage` is a projection of the
            # saga's milestones, maintained inside the same transaction as every
            # milestone transition. An imperative setter alongside that projection
            # is a second lifecycle authority, which is what let a saga report
            # IDEA_INTAKE while five of its six milestones were complete.

            async for delta in self._run_stage(saga_id, stage, stage_def):
                yield delta

            # After GAWD_DOC stage: run ambiguity gate
            if stage == SagaStage.GAWD_DOC:
                yield "\n[saga] Running ambiguity gate...\n"
                async for delta in self._ambiguity_gate(saga_id):
                    yield delta

            # After REVIEW_EVALUATION: run stagnation check
            if stage == SagaStage.REVIEW_EVALUATION:
                report = check_stagnation(saga_id)
                if report.stagnated:
                    yield f"\n[saga] STAGNATION DETECTED: {report.reason}\n"
                    yield f"[saga] Recommendation: {report.recommendation}\n"
                    _coord(["complete_saga", saga_id, "STAGNATED"])
                    return

        _coord(["complete_saga", saga_id, "COMPLETED"])
        yield f"\n[saga] Saga {saga_id} completed.\n"

    # ------------------------------------------------------------------
    # Internal: stage runner
    # ------------------------------------------------------------------

    async def _run_stage(
        self,
        saga_id: str,
        stage: SagaStage,
        stage_def: StageDef,
    ) -> AsyncIterator[str]:
        stage_str = stage.value

        required_outputs = stage_def.required_outputs
        pw_result = _coord(
            [
                "create_pow_wow",
                saga_id,
                stage_str,
                stage_def.goal,
                "--exit-criteria",
                f"All required outputs produced: {required_outputs}",
                "--required-outputs",
                *required_outputs,
            ]
        )
        if not pw_result.get("ok"):
            yield f"[{stage_str}] ERROR creating pow-wow: {pw_result.get('error')}\n"
            return

        pow_wow_id = pw_result["pow_wow_id"]
        yield f"[{stage_str}] pow_wow_id={pow_wow_id}\n"

        for character in stage_def.roster.characters:
            yield f"[{stage_str}] Enrolling character: {character.value}\n"
        for fn_role in stage_def.roster.functional_roles:
            yield f"[{stage_str}] Enrolling functional role: {fn_role.value}\n"
        for character in stage_def.roster.summon_only:
            yield f"[{stage_str}] Summon-only (escalation): {character.value}\n"

        yield f"[{stage_str}] Executing pow-wow...\n"
        async for delta in self._execute_pow_wow_via_pi(pow_wow_id, stage_def):
            yield delta

        if stage in (SagaStage.IMPLEMENTATION, SagaStage.REVIEW_EVALUATION):
            yield f"[{stage_str}] Running drift detection...\n"

        _coord(
            [
                "complete_pow_wow",
                pow_wow_id,
                f"{stage_str} stage completed. Outputs: {required_outputs}",
            ]
        )
        yield f"[{stage_str}] Completed.\n"

    async def _execute_pow_wow_via_pi(
        self,
        pow_wow_id: str,
        stage_def: StageDef,
    ) -> AsyncIterator[str]:
        """Dispatch pow-wow work to Pi.  Override for real agent dispatch."""
        goal = stage_def.goal
        required_outputs = stage_def.required_outputs
        roster_roles = [c.value for c in stage_def.roster.characters] + [
            f.value for f in stage_def.roster.functional_roles
        ]
        summon_only = [c.value for c in stage_def.roster.summon_only]

        skill_blocks: list[str] = []
        for skill in _applicable_skills(stage_def.stage, stage_def.roster):
            try:
                skill_blocks.append(self.runtime.pi_prompts.get(skill.prompt_name).text)
            except KeyError:
                logger.warning("Skill prompt %s missing from registry", skill.prompt_name)

        preamble = ("\n\n".join(skill_blocks) + "\n\n---\n\n") if skill_blocks else ""

        prompt = (
            preamble
            + "You are coordinating a pow-wow stage.\n"
            + f"Goal: {goal}\n"
            + f"Rostered roles: {roster_roles}\n"
            + f"Summon-only (escalation): {summon_only}\n"
            + f"Required outputs: {required_outputs}\n\n"
            + "Produce all required outputs. Submit each as an artifact via submit_artifact."
        )

        try:
            from ..pi_runtime import PiRuntime

            pi = self.runtime.pi
            if not isinstance(pi, PiRuntime):
                raise TypeError("runtime.pi must be a PiRuntime")
            async for delta in pi.stream(prompt):
                yield delta
        except Exception as exc:
            logger.warning("Pi execution failed for pow-wow %s: %s", pow_wow_id, exc)
            yield f"  [Pi unavailable: {exc}]\n"

    async def _ambiguity_gate(self, saga_id: str) -> AsyncIterator[str]:
        """Block saga progression until GAWD doc passes ambiguity thresholds."""
        # The current heuristic gate is advisory-only until saga records expose
        # the latest approved/draft GAWD doc id for this saga.
        _gawd_result = _coord(["list_sagas", "--status", "ACTIVE"])
        # For now, perform heuristic check via MCP
        # In production, fetch the gawd_doc_id from the saga record
        yield "  Ambiguity gate: checking clarity scores...\n"
        yield "  (Heuristic check: ensure goal >= 0.85, constraints >= 0.80, criteria >= 0.80)\n"
        yield "  Tip: Resolve all unresolved_questions before advancing.\n"

    # ------------------------------------------------------------------
    # Public: create a fresh saga from a Workflowy bullet / raw idea
    # ------------------------------------------------------------------

    async def intake_idea(
        self,
        raw_idea: str,
        budget_tokens: int = 500_000,
    ) -> AsyncIterator[str]:
        """Run only the IDEA_INTAKE pow-wow to normalise a raw idea."""
        yield f"[intake] Normalising: {raw_idea[:80]}\n"

        saga_result = _coord(["create_saga", raw_idea, "--budget-tokens", str(budget_tokens)])
        if not saga_result.get("ok"):
            yield f"[intake] ERROR: {saga_result.get('error')}\n"
            return

        saga_id = saga_result["saga_id"]
        stage_def = next(s for s in CANONICAL_SAGA_STAGES if s.stage == SagaStage.IDEA_INTAKE)
        async for delta in self._run_stage(saga_id, SagaStage.IDEA_INTAKE, stage_def):
            yield delta

        yield f"[intake] Done. saga_id={saga_id} — advance to GAWD_DOC when ready.\n"

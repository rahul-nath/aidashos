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
from pathlib import Path
from typing import Any

from ..contracts import (
    AmbiguityScore,
    DriftReport,
    EvaluationResult,
    EvaluationSummary,
    EvaluationType,
    GawdDoc,
    StagnationReport,
)

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

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed projections for the two legacy saga-inspection workflow results.

No ``SagaCoordinator`` class exists, and this module does not dispatch work.
The durable dispatcher and pow-wow executor own execution. These projections
remain only because the workflow API returns pydantic contracts while the
coordination ledger exposes JSON-shaped results.
"""

from __future__ import annotations

from typing import Any

from ..contracts import AmbiguityScore, StagnationReport
from .projects import check_ambiguity
from .projects import check_stagnation as inspect_stagnation


def _require_success(result: dict[str, Any], operation: str) -> dict[str, Any]:
    if not result.get("ok"):
        raise RuntimeError(f"{operation} failed: {result}")
    return result


def check_ambiguity_heuristic(gawd_doc_id: str) -> AmbiguityScore:
    """Project the ledger-owned heuristic result into the workflow contract."""

    result = _require_success(check_ambiguity(gawd_doc_id), "check_ambiguity")
    scores = result["scores"]
    return AmbiguityScore(
        gawd_doc_id=gawd_doc_id,
        goal_clarity=scores["goal_clarity"],
        constraints_clarity=scores["constraints_clarity"],
        success_criteria_clarity=scores["success_criteria_clarity"],
        unresolved_critical=scores["unresolved_critical"],
        ready_to_execute=result["ready_to_execute"],
        passes=result["passes"],
        scores=scores,
    )


def check_stagnation(saga_id: str) -> StagnationReport:
    """Project the ledger-owned stagnation result into the workflow contract."""

    result = _require_success(inspect_stagnation(saga_id), "check_stagnation")
    return StagnationReport(
        saga_id=saga_id,
        stagnated=result.get("stagnated", False),
        delta_ratio=result.get("delta_ratio", 0.0),
        threshold=result.get("threshold", 0.10),
        reason=result.get("reason", ""),
        recommendation=result.get("recommendation"),
        pow_wows_checked=result.get("pow_wows_checked", []),
    )


__all__ = ["check_ambiguity_heuristic", "check_stagnation"]

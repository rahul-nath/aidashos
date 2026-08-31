# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The retirement dial on the standalone approved-GAWD door.

docs/completed/designdoc_governed_work_unit_execution_design.md resolved that a
saga with milestones converges into a WorkUnit, and the WorkUnit is the only
lane with a durable owner of control flow between milestones. The standalone
`/start /approved-gawd` loop predates that resolution and kept running beside
it, which left two lanes with different spawn-authority rules and split the
trust the durable lane exists to earn.

These two functions are the whole policy, kept pure so the tests pin the exact
words an operator reads. The posture itself lives in settings
(`LOCAL_AGENT_GOVERNED_SAGA_DOOR`); the schedule for flipping its default to
RETIRED lives in docs/completed/governed_saga_door_retirement_gawd.md and is gated on the
WorkUnit lane's first production IMPLEMENT delivery, because retiring the lane
with a track record before its replacement has one would trade a working system
for a principle.
"""

from __future__ import annotations

from ..vocabulary import GovernedSagaDoorPosture

RETIREMENT_DOC = "docs/completed/governed_saga_door_retirement_gawd.md"
RETIREMENT_PROOF_WORK_UNIT_ID = "2f8e57d35257795531717cfc796ef3ac"
"""Production WorkUnit that crossed IMPLEMENT, integration, review, and delivery."""


def governed_saga_door_refusal(posture: GovernedSagaDoorPosture) -> str:
    """The permanent redirect from the removed governed saga execution lane."""

    del posture
    return (
        "the standalone saga door for governed work is retired "
        f"({RETIREMENT_DOC}). Drive this contract through the WorkUnit lane "
        "instead: agent-ledger compile_design_doc <design doc>, then the "
        "start_work_unit command it prints."
    )


def governed_saga_door_notice(posture: GovernedSagaDoorPosture) -> dict[str, str] | None:
    """Compatibility callable retained while old callers stop asking for a notice."""

    del posture
    return None


__all__ = [
    "RETIREMENT_DOC",
    "RETIREMENT_PROOF_WORK_UNIT_ID",
    "governed_saga_door_notice",
    "governed_saga_door_refusal",
]

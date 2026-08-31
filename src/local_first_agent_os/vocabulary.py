# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared vocabulary types for the whole system.

This module is a leaf: it imports nothing from the rest of the application, so
any module may import it without risk of a cycle. That is a structural
guarantee, not one that has to be re-checked as new importers appear. Put a
type here only when it is shared vocabulary - a closed set of names that more
than one subsystem must agree on - and only when it carries no behavior that
would give this file a reason to import something. Behavior that branches on
these types belongs next to that logic, not here.
"""

from __future__ import annotations

from enum import StrEnum


class GovernedSagaDoorPosture(StrEnum):
    """How the standalone approved-GAWD door treats governed work.

    Governed work - a milestone-bearing contract with approval gates - has two
    lanes today, and docs/completed/designdoc_governed_work_unit_execution_design.md
    already named the WorkUnit as where such sagas converge. This posture is the
    retirement dial for the standalone lane: OPEN is the historical behavior,
    DEPRECATED runs the door while stamping every result with the notice that
    the lane is closing, RETIRED refuses with the WorkUnit commands that replace
    it. The default lives in settings; the flip to RETIRED is gated by
    docs/completed/governed_saga_door_retirement_gawd.md.
    """

    OPEN = "open"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class DispatchTier(StrEnum):
    """Seniority axis - an engineer persona's level IS its tier.

    One name for a concept that used to have four: the ledger's dispatch tier,
    staffing's persona tier, and two `Literal["junior","senior","staff"]`
    aliases. They shared values by construction and drifted by hand; this is the
    single source they collapse into.
    """

    JUNIOR = "junior"  # local, cheap, high-count
    SENIOR = "senior"  # strong implementer
    STAFF = "staff"  # strongest; reviewer / finisher


class ToolPermissionStatus(StrEnum):
    """The states a `tool_permission_requests` row can occupy.

    The column is free text with no CHECK constraint, so this enum is the closed
    set every writer and reader must stay inside; a status spelled inline is a
    row no query will ever match. Shared vocabulary: the coordination ledger
    writes these and `capability_gate` queries them.

    Lifecycle: PENDING resolves to GRANTED or DENIED; a GRANTED row an operator
    takes back becomes REVOKED; a REVOKED row an operator lifts becomes
    RESTORED. REVOKED is a standing refusal - the gate refuses the capability
    while such a row exists, and a newer grant does not override it. RESTORED is
    terminal and neutral: it no longer revokes and it does not grant, so the
    capability returns to whatever the plan and any live grants say.
    """

    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    REVOKED = "REVOKED"
    RESTORED = "RESTORED"

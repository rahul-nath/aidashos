# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DesignDoc-governed WorkUnit execution.

One DesignDoc revision compiles into one immutable plan, one plan becomes one
WorkUnit, and one WorkUnit is one root DBOS workflow execution over a fixed
seven-phase engineering lifecycle. Documents vary the contents of the phases, not
the shape of the lifecycle.

Module map:

- ``lifecycle``: the fixed phases and the three state machines.
- ``design_doc``: the loss-preserving parser and its diagnostics.
- ``plan``: the compiled plan contract, canonical serialization, and plan hash.
- ``executors``: the trusted executor registry a plan may select from.
- ``compiler``: compile-time validation and guardrail injection.
- ``repository``: durable persistence and the one canonical transition operation.
- ``scheduling``: pure readiness and phase-exit rules.
- ``execution``: how a milestone reaches the world.
- ``root_workflow``: the root workflow, phase workflows, and milestone workflows.
- ``projection``: the cockpit read model, rebuildable from events.
- ``service``: the public operations.
"""

from .lifecycle import (
    LIFECYCLE_PROFILE,
    LIFECYCLE_PROFILE_VERSION,
    ORDERED_PHASES,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)

__all__ = [
    "LIFECYCLE_PROFILE",
    "LIFECYCLE_PROFILE_VERSION",
    "ORDERED_PHASES",
    "LifecyclePhase",
    "MilestoneExecutionStatus",
    "PhaseStatus",
    "WorkUnitStatus",
]

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Concurrent WorkUnit event persistence against a real Postgres server.

The failure this pins is the one that stopped the Pest project: several dispatch
completions persisting at once deadlocked because runtime persistence was still
executing schema DDL. Schema creation belongs to deployment or an explicit startup
migration, never to a dispatch completion, and only a real server with real
concurrent connections can show that.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination import store
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import (
    ArtifactKind,
    ArtifactRecord,
    MilestoneTransition,
    RequirableArtifact,
)
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LOCAL_AGENT_RUN_POSTGRES_INTEGRATION") != "1",
        reason="set LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 to run real Postgres tests",
    ),
]

_CONCURRENT_COMPLETIONS = 6


def test_concurrent_milestone_completions_persist_without_a_schema_lock_deadlock(
    postgres_ledger: str,
) -> None:
    """Six completions land at once, and every event is recorded exactly once.

    The schema is applied before the concurrent work starts, which is the whole
    point: nothing in this test's write path performs DDL, so nothing in it can
    take a relation lock that a sibling completion is waiting on.
    """

    compiled = compile_acceptance_doc(design_doc_id=f"concurrency_{uuid.uuid4().hex[:8]}")
    assert compiled.compiled_plan_revision_id is not None
    unit = repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit

    for key, phase in (("a", LifecyclePhase.PLAN), ("b", LifecyclePhase.IMPLEMENT)):
        for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
            repo.record_fact(
                unit.work_unit_id,
                MilestoneTransition(
                    phase=phase,
                    milestone_key=key,
                    status=status,
                    attempt=1,
                ),
            )

    def complete(index: int) -> bool:
        # Every worker submits the same fact for milestone `b`. One insert wins and
        # the rest are absorbed by the idempotency key, which is the behavior a
        # re-delivered dispatch completion needs.
        outcome = repo.record_fact(
            unit.work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.IMPLEMENT,
                milestone_key="b",
                status=MilestoneExecutionStatus.SUCCEEDED,
                attempt=1,
                result_summary=f"worker {index}",
                artifacts=(
                    ArtifactRecord(
                        artifact_type=RequirableArtifact(ArtifactKind.SOURCE_PATCH),
                        uri="workunit://concurrent/source_patch",
                        content_hash="deadbeef",
                    ),
                ),
            ),
        )
        return outcome.applied

    with ThreadPoolExecutor(max_workers=_CONCURRENT_COMPLETIONS) as pool:
        results = list(pool.map(complete, range(_CONCURRENT_COMPLETIONS)))

    assert sum(results) == 1, "exactly one completion may apply"
    events = repo.list_work_unit_events(unit.work_unit_id, limit=1000)
    assert sum(1 for item in events if item.event_type.value == "MILESTONE_SUCCEEDED") == 1
    assert len(repo.list_work_unit_artifacts(unit.work_unit_id)) == 1
    executions = {
        item.stable_key: item.status for item in repo.list_milestone_executions(unit.work_unit_id)
    }
    assert executions["b"] is MilestoneExecutionStatus.SUCCEEDED


def test_runtime_event_persistence_performs_no_schema_migration(
    postgres_ledger: str,
) -> None:
    """A completion must not run DDL, even on a database that is already current.

    The assertion is on the schema version marker: it is written once by the
    migration path and never touched by a transition.
    """

    compiled = compile_acceptance_doc(design_doc_id=f"no_ddl_{uuid.uuid4().hex[:8]}")
    assert compiled.compiled_plan_revision_id is not None
    unit = repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit

    with psycopg.connect(postgres_ledger) as connection:
        before = connection.execute(
            "SELECT version, applied_at FROM coordination_schema_versions WHERE component=%s",
            (store.POSTGRES_SCHEMA_COMPONENT,),
        ).fetchone()

    repo.record_fact(
        unit.work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key="a",
            status=MilestoneExecutionStatus.READY,
            attempt=1,
        ),
    )

    with psycopg.connect(postgres_ledger) as connection:
        after = connection.execute(
            "SELECT version, applied_at FROM coordination_schema_versions WHERE component=%s",
            (store.POSTGRES_SCHEMA_COMPONENT,),
        ).fetchone()

    assert before is not None
    assert after == before

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared DesignDoc fixtures for the WorkUnit tests.

The acceptance document is the one from the design brief: A in PLAN, B and C in
IMPLEMENT depending on A, D in VERIFY depending on both, E in REVIEW requiring
operator approval, F in DELIVER. Every WorkUnit test that needs a realistic plan
uses this one so a change in compiler behavior shows up in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

from local_first_agent_os.settings import get_settings
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.design_doc import parse_design_doc
from local_first_agent_os.work_units.execution import SimulatedExecutorRuntime
from local_first_agent_os.work_units.lifecycle import (
    TERMINAL_WORK_UNIT_STATUSES,
    FailureClass,
)
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    WorkUnitEngine,
    set_engine,
)

ACCEPTANCE_DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "examples" / "work_unit_acceptance_design_doc.md"
)

# One copy of the example, on disk, read by both the tests and the walkthrough
# documentation. A second copy in either place would be a second source of truth
# for what the compiler accepts.
ACCEPTANCE_DESIGN_DOC = ACCEPTANCE_DESIGN_DOC_PATH.read_text(encoding="utf-8")


def acceptance_target_project_id() -> str:
    """The example's declared identity, used to provision its isolated test registry."""

    target = parse_design_doc(
        ACCEPTANCE_DESIGN_DOC, design_doc_id="acceptance_fixture"
    ).declared_target_project_id
    assert target is not None
    return target


def register_document_target(markdown: str, target: Path) -> str:
    """Give a document its declared project in the isolated test registry."""

    project_id = parse_design_doc(
        markdown, design_doc_id="registered_fixture"
    ).declared_target_project_id
    assert project_id is not None
    target.mkdir(parents=True, exist_ok=True)
    write_test_project_registry(get_settings().config_dir, project_id, target)
    return project_id


def write_test_project_registry(config_dir: Path, project_id: str, target: Path) -> Path:
    """Register the actual test target before compilation can attempt adoption."""

    config_dir.mkdir(parents=True, exist_ok=True)
    registry = config_dir / "linked_projects.toml"
    identity = json.dumps(project_id)
    registry.write_text(
        f"""[center]
id = {identity}
description = "Isolated test center"
control_plane_project = {identity}
default_saga_project = {identity}
default_memory_project = {identity}

[[projects]]
id = {identity}
kind = "test_repo"
path = {json.dumps(str(target))}
status = "active"
read_only = false
description = "Isolated test target"
primary_interfaces = ["pytest"]
owns = ["tests"]
avoid = []
verification_commands = ["uv run pytest"]
""",
        encoding="utf-8",
    )
    return registry


def compile_acceptance_doc(
    *,
    design_doc_id: str = "acceptance_design_doc",
    content: str | None = None,
) -> service.CompileResult:
    result = service.compile_design_doc_text(
        content if content is not None else ACCEPTANCE_DESIGN_DOC,
        design_doc_id=design_doc_id,
    )
    assert result.compiled_plan_revision_id is not None, result.diagnostics
    return result


def install_simulated_engine(
    *,
    failing_milestones: frozenset[str] = frozenset(),
    failure_class: FailureClass = FailureClass.NONRECOVERABLE,
    approval_wait_seconds: float = 0.0,
    delay_seconds: float = 0.0,
) -> SimulatedExecutorRuntime:
    """Install a deterministic engine and return the runtime it will use.

    ``approval_wait_seconds=0`` makes an approval gate park immediately instead of
    polling, which is how a test observes the waiting state without sleeping.
    """

    runtime = SimulatedExecutorRuntime(
        failing_milestones=failing_milestones,
        failure_class=failure_class,
        delay_seconds=delay_seconds,
    )
    set_engine(
        WorkUnitEngine(
            runtime=runtime,
            approval_wait_seconds=approval_wait_seconds,
            approval_poll_seconds=0.01,
        )
    )
    return runtime


def settle_operator_decisions(
    work_unit_id: str,
    *,
    decision: str = "APPROVED",
    max_rounds: int = 4,
) -> int:
    """Answer every pending decision and resume, until nothing is waiting.

    This is what an operator does, expressed as a loop: approve the named request,
    resume the WorkUnit, and look again. It is bounded so a test cannot hang on a
    lifecycle that keeps asking.
    """

    rounds = 0
    while rounds < max_rounds:
        pending = service.pending_operator_decisions(work_unit_id)
        if not pending:
            return rounds
        for request in pending:
            service.submit_work_unit_decision(
                work_unit_id,
                request["request_id"],
                decision,
                f"idem-{request['request_id']}-{rounds}",
            )
        rounds += 1
        if repo.get_work_unit(work_unit_id).status in TERMINAL_WORK_UNIT_STATUSES:
            return rounds
        service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
    return rounds


def run_acceptance_work_unit(*, design_doc_id: str = "acceptance_design_doc") -> str:
    """Compile, start, approve, and resume the acceptance document to completion."""

    compiled = compile_acceptance_doc(design_doc_id=design_doc_id)
    assert compiled.compiled_plan_revision_id is not None
    started = service.start_work_unit(
        compiled.compiled_plan_revision_id,
        delivery=EnqueueDelivery.INLINE,
    )
    work_unit_id = str(started["work_unit_id"])
    settle_operator_decisions(work_unit_id)
    return work_unit_id


def start_inline(compiled_plan_revision_id: str, **kwargs: object) -> dict[str, object]:
    """Start a WorkUnit and drive its lifecycle in this process.

    Tests are the process, so they ask for inline delivery explicitly rather than
    relying on what happens when no DBOS runtime is up.
    """

    return service.start_work_unit(
        compiled_plan_revision_id,
        delivery=EnqueueDelivery.INLINE,
        **kwargs,  # type: ignore[arg-type]
    )

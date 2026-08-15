# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dependency-context block's overflow behaviour, and its fallback.

The property under test throughout is that a summariser is an improvement and
never a dependency: no absence, refusal, or misbehaviour of one may change
whether a prompt can be built.
"""

from __future__ import annotations

from pathlib import Path

from local_first_agent_os import dependency_context_compactor
from local_first_agent_os.agent_adapters import AgentResult
from local_first_agent_os.constants import DELEGATED_TASK_RUN_ARTIFACT_TYPE
from local_first_agent_os.contracts import WorkflowStatus, WorkflowType
from local_first_agent_os.dependency_context_compactor import (
    COMPACTION_WORKFLOW_ID,
    DEPENDENCY_COMPACTION_TIMEOUT_SECONDS,
    build_dependency_context_compactor,
)
from local_first_agent_os.pi_prompts import PiPromptRegistry
from local_first_agent_os.pow_wow.prompts import (
    build_agent_task_prompt,
    render_dependency_context_block,
)
from local_first_agent_os.pow_wow.types import (
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowTaskResult,
    PowWowTaskSpec,
)
from local_first_agent_os.pow_wow.views import ViewCompactionRequest

_LIMIT = 12000


def _result(task_name: str, *, output_chars: int) -> PowWowTaskResult:
    return PowWowTaskResult(
        task_name=task_name,
        role="implementer",
        status="completed",
        summary=f"{task_name} finished",
        changed_files=(f"src/{task_name}.py",),
        artifacts=(
            PowWowArtifact(
                artifact_type=DELEGATED_TASK_RUN_ARTIFACT_TYPE,
                task_name=task_name,
                content={"output": f"{task_name}-" * output_chars},
            ),
        ),
    )


def _overflowing_results() -> tuple[PowWowTaskResult, ...]:
    """Enough dependency output that the joined block cannot fit the limit.

    Several tasks rather than one, because the defect truncation causes is
    positional: the last dependency is the one that disappears.
    """

    return tuple(_result(f"task_{index}", output_chars=400) for index in range(12))


def _context() -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-1",
        goal="Ship the compaction path",
        directive="/saga Ship the compaction path",
        target_project_id="repo",
        target_project_path="/tmp/repo",
        target_project_kind="code",
        target_project_status="active",
        target_project_read_only=False,
    )


def test_block_under_the_limit_is_passed_through_untouched() -> None:
    block = render_dependency_context_block(
        (_result("task_0", output_chars=5),),
        compactor=lambda _request: "a summary nobody asked for",
    )

    assert "compacted view" not in block
    assert "truncated view" not in block
    assert block.startswith("Completed dependency outputs:")
    assert "task_0 finished" in block


def test_over_the_limit_summarises_when_a_compactor_is_supplied() -> None:
    seen: list[ViewCompactionRequest] = []

    def compactor(request: ViewCompactionRequest) -> str:
        seen.append(request)
        return "task_0 through task_11 all completed; see changed files."

    block = render_dependency_context_block(_overflowing_results(), compactor=compactor)

    assert len(seen) == 1
    assert seen[0].source == "pow_wow_dependency_context"
    assert seen[0].char_limit == _LIMIT
    assert len(seen[0].content) > _LIMIT
    # The whole point of a summary over a prefix: the last dependency survives.
    assert "task_11" in seen[0].content
    assert "task_11" in block
    assert "compacted view from pow_wow_dependency_context" in block
    assert "truncated view" not in block


def test_over_the_limit_truncates_when_no_compactor_is_supplied() -> None:
    block = render_dependency_context_block(_overflowing_results())

    assert "truncated view from pow_wow_dependency_context" in block
    assert "compacted view" not in block


def test_over_the_limit_truncates_when_the_compactor_raises() -> None:
    def unreachable_compactor(_request: ViewCompactionRequest) -> str:
        raise ConnectionError("llama server is not running")

    block = render_dependency_context_block(
        _overflowing_results(),
        compactor=unreachable_compactor,
    )

    assert "truncated view from pow_wow_dependency_context" in block
    assert "compacted view" not in block
    assert block == render_dependency_context_block(_overflowing_results())


def test_over_the_limit_truncates_when_the_compactor_returns_nothing() -> None:
    block = render_dependency_context_block(
        _overflowing_results(),
        compactor=lambda _request: "   \n  ",
    )

    assert "truncated view from pow_wow_dependency_context" in block


def test_over_the_limit_truncates_when_the_summary_ignores_the_budget() -> None:
    block = render_dependency_context_block(
        _overflowing_results(),
        compactor=lambda request: "x" * (request.char_limit + 1),
    )

    assert "truncated view from pow_wow_dependency_context" in block
    assert "xxxx" not in block


def test_a_compacted_prompt_says_so_to_whoever_reads_it_later() -> None:
    task = PowWowTaskSpec(
        task_name="implement",
        role="implementer",
        description="build on the dependencies",
        dispatch_kind="code",
    )

    prompt = build_agent_task_prompt(
        task,
        _context(),
        dependency_results=_overflowing_results(),
        dependency_compactor=lambda _request: "every dependency completed.",
    )

    assert "compacted view from pow_wow_dependency_context" in prompt
    assert "every dependency completed." in prompt


def test_a_failed_compactor_still_produces_the_full_prompt() -> None:
    task = PowWowTaskSpec(
        task_name="implement",
        role="implementer",
        description="build on the dependencies",
        dispatch_kind="code",
    )

    def dead_compactor(_request: ViewCompactionRequest) -> str:
        raise RuntimeError("compactor model refused to load")

    prompt = build_agent_task_prompt(
        task,
        _context(),
        dependency_results=_overflowing_results(),
        dependency_compactor=dead_compactor,
    )

    assert prompt == build_agent_task_prompt(
        task,
        _context(),
        dependency_results=_overflowing_results(),
    )
    assert "truncated view from pow_wow_dependency_context" in prompt
    assert "assigned worktree" in prompt


# --- the runtime-backed compactor --------------------------------------------


def _summarising_delegate(calls: list[dict]):
    """A delegate that answers like a compactor and writes down how it was asked.

    The real delegate path is a model over a socket; what this file owns is the
    contract the compactor holds up on its side of that call - the parent
    workflow row, the bounded timeout, the prompt - so the seam is faked exactly
    at the boundary the module itself declares.
    """

    async def fake_delegate(_runtime, **kwargs):
        calls.append(kwargs)
        return AgentResult(
            task_id="compaction-fake",
            success=True,
            output="task_0 through task_11 all completed; see changed files.",
        )

    return fake_delegate


def test_the_runtime_compactor_registers_a_terminal_workflow_row(runtime, monkeypatch) -> None:
    """`model_invocations.workflow_id` is NOT NULL REFERENCES workflow_runs.

    Prompt assembly has no workflow of its own to borrow, so a compactor that
    does not open one cannot call the model at all. The row must also rest at
    COMPLETED from the start: `list_pending_workflow_runs` selects CREATED, and
    operator recovery marks any pending row without an input event
    FAILED_PERMANENT - a permanent failure asserted over a workflow that never
    failed, on the one record the startup skill names as authoritative.
    """

    runtime.settings.mock_models = False
    calls: list[dict] = []
    monkeypatch.setattr(
        dependency_context_compactor, "delegate_agent_task", _summarising_delegate(calls)
    )
    compactor = build_dependency_context_compactor(runtime)
    assert not runtime.repository.workflow_run_exists(COMPACTION_WORKFLOW_ID)

    block = render_dependency_context_block(_overflowing_results(), compactor=compactor)

    assert "compacted view from pow_wow_dependency_context" in block
    state = runtime.repository.get_workflow_run_state(COMPACTION_WORKFLOW_ID)
    assert state is not None
    assert state.workflow_type is WorkflowType.CONTEXT_COMPACTION
    assert state.status is WorkflowStatus.COMPLETED
    assert COMPACTION_WORKFLOW_ID not in [
        workflow_id for workflow_id, _, _ in runtime.repository.list_pending_workflow_runs()
    ]


def test_the_compaction_call_is_bounded_to_an_advisory_budget(runtime, monkeypatch) -> None:
    """The stall this bounds sits before the execution-attempt lease opens.

    Left to the default, the call inherits the frontier agent's full hour, and a
    wedged llama server holds the tier slot invisibly for all of it. The repo
    already priced this shape of call at an advisory budget
    (DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS); compaction pays the same.
    """

    runtime.settings.mock_models = False
    calls: list[dict] = []
    monkeypatch.setattr(
        dependency_context_compactor, "delegate_agent_task", _summarising_delegate(calls)
    )
    compactor = build_dependency_context_compactor(runtime)

    render_dependency_context_block(_overflowing_results(), compactor=compactor)

    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == DEPENDENCY_COMPACTION_TIMEOUT_SECONDS
    assert DEPENDENCY_COMPACTION_TIMEOUT_SECONDS <= 300


def test_a_swept_compaction_row_is_healed_on_the_next_registration(runtime, monkeypatch) -> None:
    """A ledger the old sweep already damaged corrects itself.

    Recovery used to mark the CREATED row FAILED_PERMANENT, and
    `start_workflow_run` is read-then-insert, so nothing ever reset it: the
    ledger showed a permanent failure accruing successful model invocations.
    Registration now re-stamps COMPLETED unconditionally, which is also the
    repair.
    """

    runtime.settings.mock_models = False
    monkeypatch.setattr(
        dependency_context_compactor, "delegate_agent_task", _summarising_delegate([])
    )
    runtime.repository.start_workflow_run(
        workflow_id=COMPACTION_WORKFLOW_ID,
        workflow_type=WorkflowType.CONTEXT_COMPACTION.value,
        workspace_id="general",
        input_event_id=None,
    )
    runtime.repository.update_workflow(
        COMPACTION_WORKFLOW_ID,
        status=WorkflowStatus.FAILED_PERMANENT,
        error="cannot recover workflow without an input event id",
    )

    compactor = build_dependency_context_compactor(runtime)
    render_dependency_context_block(_overflowing_results(), compactor=compactor)

    state = runtime.repository.get_workflow_run_state(COMPACTION_WORKFLOW_ID)
    assert state is not None
    assert state.status is WorkflowStatus.COMPLETED
    assert state.last_error is None


def test_the_runtime_compactor_declines_under_mock_models(runtime) -> None:
    """Under mock models, compaction must lose to truncation on purpose.

    The mock returns the Pi context-compaction schema for the COMPACTOR role:
    well-formed, non-empty, under budget, and containing none of the dependency
    content, so it passes every fallback gate while destroying the block. And
    mock_models ships true in docker-compose.yml and k8s/kind/app.yaml, not
    only here. Declining lands in the ordinary fallback, which preserves the
    real content as far as the budget allows.
    """

    assert runtime.settings.mock_models is True
    compactor = build_dependency_context_compactor(runtime)

    block = render_dependency_context_block(_overflowing_results(), compactor=compactor)

    assert block == render_dependency_context_block(_overflowing_results())
    assert "truncated view from pow_wow_dependency_context" in block
    assert "task_0-" in block
    assert "compacted view" not in block


def test_the_runtime_compactor_truncates_when_its_prompt_is_not_registered(
    runtime,
    tmp_path: Path,
) -> None:
    """An operator config predating this feature is a fallback, not a failure."""

    runtime.settings.mock_models = False
    runtime.pi_prompts = PiPromptRegistry(tmp_path / "no_such_prompts.toml")
    compactor = build_dependency_context_compactor(runtime)

    block = render_dependency_context_block(_overflowing_results(), compactor=compactor)

    assert "truncated view from pow_wow_dependency_context" in block
    assert "compacted view" not in block

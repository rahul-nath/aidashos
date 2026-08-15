# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from local_first_agent_os.coordination import (
    AmendSagaMilestone,
    ApprovalDecision,
    CollectionResult,
    CompleteDispatchIntent,
    CoordinationCommandName,
    DispatchTerminalStatus,
    ListDispatchIntents,
    RawCoordinationCommand,
    RecordingCoordinationTransport,
    ResolveApprovalRequest,
    RevokeApprovalRequest,
    SubmitArtifact,
    parse_coordination_result,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _str_enum_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "StrEnum" for base in node.bases)
    }


def test_outcome_taxonomy_does_not_redefine_contract_enums() -> None:
    contract_enums = _str_enum_definitions(
        PROJECT_ROOT / "src" / "local_first_agent_os" / "contracts.py"
    )
    outcome_enums = _str_enum_definitions(
        PROJECT_ROOT / "src" / "local_first_agent_os" / "coordination" / "outcomes.py"
    )

    assert contract_enums.isdisjoint(outcome_enums)


def test_command_sum_owns_command_specific_argv_shape() -> None:
    command = CompleteDispatchIntent(
        intent_id="intent-1",
        status=DispatchTerminalStatus.FAILED,
        error="verification failed",
    )

    assert command.to_argv() == [
        "complete_dispatch_intent",
        "intent-1",
        "FAILED",
        "--error",
        "verification failed",
    ]


def test_finite_fields_are_enums_not_free_strings() -> None:
    command = ResolveApprovalRequest(
        approval_id="approval-1",
        decision=ApprovalDecision.APPROVE,
        resolved_by="rahul",
    )
    assert command.to_argv()[2] == "approve"


def test_approval_revocation_owns_distinct_actor_and_reason_fields() -> None:
    command = RevokeApprovalRequest(
        approval_id="approval-1",
        revoked_by="rahul",
        reason="The target branch advanced.",
    )

    assert command.to_argv() == [
        "revoke_approval_request",
        "approval-1",
        "--revoked-by",
        "rahul",
        "--reason",
        "The target branch advanced.",
    ]


def test_milestone_amendment_owns_repeated_contract_fields() -> None:
    command = AmendSagaMilestone(
        milestone_id="m2",
        reason="Bound local resource usage.",
        amended_by="rahul",
        entry_criteria=("Verified claims record exists.",),
        exit_criteria=("Peak RSS is recorded.",),
        required_artifacts=("resource_usage_report",),
    )

    assert command.to_argv() == [
        "amend_saga_milestone",
        "m2",
        "--entry-criteria",
        "Verified claims record exists.",
        "--exit-criteria",
        "Peak RSS is recorded.",
        "--required-artifact",
        "resource_usage_report",
        "--reason",
        "Bound local resource usage.",
        "--amended-by",
        "rahul",
    ]


def test_raw_compatibility_adapter_rejects_unknown_commands() -> None:
    with pytest.raises(ValueError, match="not a valid CoordinationCommandName"):
        RawCoordinationCommand.from_argv(["complete_disptach_intent", "intent-1"])


def test_result_parser_returns_collection_variant() -> None:
    command = ListDispatchIntents(parent_intent_id="parent-1")
    result = parse_coordination_result(
        command,
        {"ok": True, "intents": [{"intent_id": "child-1"}]},
    )

    assert isinstance(result, CollectionResult)
    assert result.items[0].require_str("intent_id") == "child-1"


def test_recording_transport_is_a_typed_mock_provider() -> None:
    transport = RecordingCoordinationTransport(
        responses={
            CoordinationCommandName.SUBMIT_ARTIFACT: [{"ok": True, "artifact_id": "artifact-1"}]
        }
    )
    command = SubmitArtifact(
        pow_wow_id="pow-1",
        artifact_type="test_log",
        content="passed",
    )

    assert transport.execute(command)["artifact_id"] == "artifact-1"
    assert transport.commands == [command]


def test_exported_command_and_flag_enums_cover_the_cli_grammar() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "local_first_agent_os"
        / "coordination"
        / "cli.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    parser_commands: set[str] = set()
    parser_flags: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        value = node.args[0].value
        if not isinstance(value, str):
            continue
        if node.func.attr == "add_parser":
            parser_commands.add(value)
        elif node.func.attr == "add_argument" and value.startswith("--"):
            parser_flags.add(value)

    assert parser_commands == {item.value for item in CoordinationCommandName}
    from local_first_agent_os.coordination import CoordinationFlag

    assert parser_flags == {item.value for item in CoordinationFlag}


def test_production_callers_do_not_reintroduce_raw_argv_literals() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "local_first_agent_os"
    violations: list[str] = []
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "run_coordination_command" or not node.args:
                continue
            if isinstance(node.args[0], ast.List):
                violations.append(f"{source_path.name}:{node.lineno}")

    assert violations == []

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import subprocess
import warnings

import pytest
from pydantic import ValidationError
from pydantic.json_schema import PydanticJsonSchemaWarning

from local_first_agent_os.release_contract import (
    CandidateIdentity,
    CheckMissing,
    CheckPassed,
    ReleaseCheck,
    ReleaseEvidence,
    file_sha256,
)
from local_first_agent_os.release_packaging import build_source_candidate


def candidate(artifact):
    return CandidateIdentity(
        version="0.1.0",
        public_commit="a" * 40,
        artifact_sha256=file_sha256(artifact),
        baseline_commit="b" * 40,
        baseline_kind="pre_release_commit",
        environment="test-only",
    )


def test_evidence_cannot_qualify_unknown_checks_or_a_different_candidate(tmp_path):
    artifact = tmp_path / "candidate.tar.gz"
    artifact.write_bytes(b"artifact")
    identity = candidate(artifact)
    missing = tuple(CheckMissing(check=check, reason="not exercised") for check in ReleaseCheck)
    evidence = ReleaseEvidence(candidate=identity, checks=missing)
    with pytest.raises(ValueError, match="not qualified"):
        evidence.require_qualified(tmp_path, artifact)
    with pytest.raises(ValidationError, match="exactly once"):
        ReleaseEvidence(candidate=identity, checks=missing[:-1])
    with pytest.raises(ValidationError, match="exactly once"):
        ReleaseEvidence(candidate=identity, checks=(*missing, missing[0]))
    record = tmp_path / "result.txt"
    record.write_text("test result")
    passed = tuple(
        CheckPassed(
            check=check,
            candidate=identity,
            evidence_file=record.name,
            evidence_sha256=file_sha256(record),
        )
        for check in ReleaseCheck
    )
    evidence = ReleaseEvidence(candidate=identity, checks=passed)
    evidence.require_qualified(tmp_path, artifact)
    with pytest.raises(ValidationError, match="different candidate"):
        ReleaseEvidence(
            candidate=identity.model_copy(update={"public_commit": "c" * 40}), checks=passed
        )
    artifact.write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact"):
        evidence.require_qualified(tmp_path, artifact)


def test_qualified_record_rechecks_evidence_content_and_paths(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"candidate")
    identity = candidate(artifact)
    record = tmp_path / "log"
    record.write_bytes(b"passed")
    checks = tuple(
        CheckPassed(
            check=check,
            candidate=identity,
            evidence_file="log",
            evidence_sha256=file_sha256(record),
        )
        for check in ReleaseCheck
    )
    evidence = ReleaseEvidence(candidate=identity, checks=checks)
    record.write_bytes(b"rewritten")
    with pytest.raises(ValueError, match="evidence changed"):
        evidence.require_qualified(tmp_path, artifact)
    escaped = tuple(item.model_copy(update={"evidence_file": "../outside"}) for item in checks)
    with pytest.raises(ValueError, match="inside"):
        ReleaseEvidence(candidate=identity, checks=escaped).require_qualified(tmp_path, artifact)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def public_tree(tmp_path):
    source = tmp_path / "public"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "release-test@example.invalid")
    git(source, "config", "user.name", "Release test")
    for name, content in {
        "public_import.toml": "schema_version = 1\n",
        "LICENSE": "test license fixture",
        "uv.lock": "version = 1\n",
        "pyproject.toml": '[project]\nname="fixture"\nversion="0.1.0"\n',
    }.items():
        (source / name).write_text(content)
    git(source, "add", ".")
    git(source, "commit", "-qm", "Create release fixture")
    return source


def test_source_candidate_is_reproducible_and_never_overwrites(tmp_path):
    source = public_tree(tmp_path)
    first = build_source_candidate(source, tmp_path / "one")
    second = build_source_candidate(source, tmp_path / "two")
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        build_source_candidate(source, tmp_path / "one")
    (source / "untracked").write_text("operator context")
    with pytest.raises(ValueError, match="clean"):
        build_source_candidate(source, tmp_path / "three")


def test_source_candidate_refuses_operator_checkout_and_tracked_secrets(tmp_path):
    source = public_tree(tmp_path)
    git(source, "rm", "LICENSE")
    git(source, "commit", "-qm", "Remove public marker")
    with pytest.raises(ValueError, match="curated"):
        build_source_candidate(source, tmp_path / "one")
    (source / "LICENSE").write_text("test license fixture")
    (source / ".env").write_text("secret fixture")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Add forbidden state")
    with pytest.raises(ValueError, match="operator state"):
        build_source_candidate(source, tmp_path / "two")


def test_mcp_initialization_exposes_only_serializable_inputs():
    from local_first_agent_os.coordination.cli import build_mcp_server

    with warnings.catch_warnings():
        warnings.simplefilter("error", PydanticJsonSchemaWarning)
        server = build_mcp_server()
        tools = asyncio.run(server.list_tools())
    fleet = next(tool for tool in tools if tool.name == "run_refinery_fleet")
    assert "sleep" not in fleet.inputSchema["properties"]
    assert fleet.inputSchema["required"] == ["target_project_ids"]


def test_previous_client_contracts_remain_compatible():
    import gzip
    import json
    from pathlib import Path

    from local_first_agent_os.compatibility_contract import capture_contract, compatibility_changes

    root = Path(__file__).resolve().parents[1]
    previous = json.loads(
        gzip.decompress((root / "tests/fixtures/releases/pre-v0.1.0-contract.json.gz").read_bytes())
    )
    assert previous["baseline_commit"] == "5c7eb485cd83ebbc2b35968e8aee49d27bc5d217"
    baseline = previous["contract"]
    # A declared pre-release correction: a Python callback was never a
    # valid JSON input. Prove the exact delta rather than waive the entire tool.
    fleet = baseline["mcp"]["run_refinery_fleet"]["inputSchema"]["properties"]
    assert fleet.pop("sleep") == {}
    current = capture_contract(json.loads((root / "web/openapi.json").read_text()))
    # Enumerate the exact recovery-contract delta without weakening the generic
    # compatibility gate or replacing the immutable previous-release fixture.
    retry = current["cli"]["command:request_recovery_staff_review"].pop("argument:retry_of")
    assert retry == {
        "options": ["--retry-of"],
        "nargs": None,
        "required": False,
        "choices": None,
        "type": None,
        "action": "_StoreAction",
    }
    recovery_input = current["mcp"]["request_recovery_staff_review"]["inputSchema"]
    assert "retry_of" not in recovery_input["required"]
    assert recovery_input["properties"].pop("retry_of") == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
    }
    # This output enum extension is intentionally pre-release: an unavailable
    # reviewer must not be represented as a request to revise implementation.
    assert baseline["http_models"]["ReviewDisposition"]["enum"] == [
        "approve",
        "request_changes",
        "reject",
        "escalate",
        "unclassified",
    ]
    assert current["http_models"]["ReviewDisposition"]["enum"] == [
        "approve",
        "request_changes",
        "reject",
        "escalate",
        "unavailable",
        "unclassified",
    ]
    current["http_models"]["ReviewDisposition"]["enum"].remove("unavailable")
    assert compatibility_changes(baseline, current) == ()


def test_compatibility_gate_detects_removals_and_changed_shapes():
    from copy import deepcopy

    from local_first_agent_os.compatibility_contract import compatibility_changes

    baseline = {
        "schema_version": "public_contract_snapshot.v1",
        "cli": {},
        "mcp": {},
        "http": {"GET /example": {"responses": {"200": {"type": "string"}}}},
        "http_models": {},
    }
    current = deepcopy(baseline)
    current["http"]["GET /added"] = {}
    assert compatibility_changes(baseline, current) == ()
    current["http"]["GET /example"]["responses"]["200"]["type"] = "integer"
    assert compatibility_changes(baseline, current) == ("http:GET /example: changed",)
    del current["http"]["GET /example"]
    assert compatibility_changes(baseline, current) == ("http:GET /example: removed",)

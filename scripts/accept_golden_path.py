#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run the composed resident flow and its essential recovery contracts together.

The resident test owns disposable databases and processes. This command selects
that existing integration test explicitly and refuses a skipped or missing
scenario, so the ordinary suite's optional integration flag cannot imply proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from local_first_agent_os.verification_git import verification_git_environment

ROOT = Path(__file__).resolve().parents[1]
CORE = (
    "tests/test_work_unit_golden_path.py::test_the_golden_path_runs_through_the_resident_loops",
    "tests/test_direct_query_retirement.py",
    "tests/test_governed_saga_door.py",
    "tests/test_work_unit_golden_path.py::test_a_cancelled_work_unit_reaches_the_lease_its_intent_started",
    "tests/test_work_unit_retry_budget.py::test_retry_override_replay_cannot_authorize_a_second_extra_execution",
    "tests/test_work_unit_local_model_readiness.py::test_unavailable_local_model_does_not_spend_the_work_budget",
    "tests/test_worktree_loss.py::test_executor_keeps_loss_after_cleanup_and_allocates_a_fresh_retry",
    "tests/test_supervisor_checkpoint_failure_evidence.py::test_checkpoint_write_failure_preserves_primary_execution_evidence",
    "tests/test_execution_admission.py::test_original_verify_grant_refuses_agent_and_inspection_but_admits_command_driver",
)
NATIVE = (
    "tests/test_host_verification_receipts.py::test_real_contained_gate_receipt_can_discharge_verify_once",
    "tests/test_registered_verification_dispatch.py::test_public_runner_routes_real_gate_without_constructing_model_work",
)


def source_identity() -> dict[str, str]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "--no-optional-locks", "-C", str(ROOT), *args],
            env=verification_git_environment(),
        )

    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(files) - {b""}):
        path = ROOT / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.exists() or path.is_symlink():
            digest.update(str(path.lstat().st_mode & 0o777).encode() + b"\0")
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = b"<deleted>"
        digest.update(hashlib.sha256(content).digest())
    return {"commit": git("rev-parse", "HEAD").decode().strip(), "tree_sha256": digest.hexdigest()}


def coverage(report: Path, selections: tuple[str, ...]) -> dict[str, object]:
    cases = list(ET.parse(report).iter("testcase"))
    refused = [
        case.attrib.get("name", "unknown")
        for case in cases
        if any(case.find(kind) is not None for kind in ("skipped", "failure", "error"))
    ]
    missing = []
    for selection in selections:
        filename, _, function = selection.partition("::")
        module = filename.removesuffix(".py").replace("/", ".")
        matched = any(
            case.attrib.get("classname") == module
            and (
                not function
                or case.attrib.get("name") == function
                or case.attrib.get("name", "").startswith(function + "[")
            )
            for case in cases
        )
        if not matched:
            missing.append(selection)
    return {"cases": len(cases), "unsuccessful": refused, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--native",
        action="store_true",
        help="Require installed UID-verifier receipt/routing controls",
    )
    parser.add_argument("--report", type=Path, help="Fresh directory for retained results and logs")
    args = parser.parse_args()
    os.umask(0o077)
    if args.report is None:
        directory = Path(tempfile.mkdtemp(prefix="aidashos-golden-path-"))
    else:
        directory = args.report.expanduser().resolve()
        directory.mkdir(mode=0o700)
    selections = CORE + (NATIVE if args.native else ())
    report: dict[str, object] = {
        "schema": "golden_path_acceptance.v1",
        "source": source_identity(),
        "status": "incomplete",
        "selections": selections,
        "provider": "deterministic local-model fixture",
        "resident_workflow": "advisory PLAN -> operator REVIEW -> durable DELIVER record",
        "resident_processes": "real",
        "databases": "disposable Postgres and DBOS",
        "native_receipt_and_routing_controls": "required" if args.native else "not_run",
        "native_security_qualification": "not_run",
        "code_change_and_integration_workflow": "not_run",
        "registered_3600_second_gate": "not_run",
    }
    result_path = directory / "result.json"
    result_path.write_text(json.dumps(report, indent=2) + "\n")
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "LOCAL_AGENT_RUN_POSTGRES_INTEGRATION": "1",
        "LOCAL_AGENT_USE_DBOS": "false",
        "LOCAL_AGENT_MOCK_MODELS": "true",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("LOCAL_AGENT_POSTGRES_ADMIN_URL", None)
    if args.native:
        environment["AIDASHOS_REQUIRE_UID_VERIFIER"] = "1"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rs",
        *selections,
        f"--junitxml={directory / 'cases.xml'}",
    ]
    print(f"Golden-path acceptance logs: {directory}", flush=True)
    try:
        with (directory / "pytest.log").open("wb") as stream:
            completed = subprocess.run(
                command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT
            )
        observed = coverage(directory / "cases.xml", selections)
        passed = (
            completed.returncode == 0 and not observed["missing"] and not observed["unsuccessful"]
        )
        report.update(observed, exit_code=completed.returncode)
        report["source_after"] = source_identity()
        if report["source_after"] != report["source"]:
            passed = False
            report.update(status="failed", error="source changed during acceptance")
        report["status"] = "passed" if passed else "failed"
        if args.native:
            report["native_receipt_and_routing_controls"] = "passed" if passed else "not_certified"
    except (OSError, subprocess.SubprocessError, ET.ParseError, KeyboardInterrupt) as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        result_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(result_path)}, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

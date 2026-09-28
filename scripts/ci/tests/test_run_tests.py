# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise CI result admission with real child pytest, without a test database."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci import run_tests


@pytest.mark.parametrize(
    ("body", "success"),
    (
        ("def test_proof(): assert True", True),
        ("def test_proof(): assert False", False),
        ("import pytest\ndef test_proof(): pytest.skip('unavailable boundary')", False),
        ("# No proofs collected", False),
    ),
    ids=("passing", "failed", "skipped", "empty"),
)
def test_native_result_admission_uses_actual_pytest_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, success: bool
) -> None:
    # This tests the orchestration contract, never claims native containment proof.
    monkeypatch.setattr(run_tests.platform, "system", lambda: "Darwin")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_proof.py").write_text(body)
    monkeypatch.setattr(run_tests, "targets", lambda _: ("test_proof.py",))
    result = run_tests.main(
        ["native", "--root", str(tmp_path), "--output", str(tmp_path / "results")]
    )
    assert (result == 0) is success


def test_portable_retains_skips_without_claiming_native_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_proof.py").write_text(
        "import pytest\ndef test_proof(): pytest.skip('native qualification unavailable')"
    )
    monkeypatch.setattr(run_tests, "targets", lambda _: ("test_proof.py",))
    assert run_tests.run(run_tests.Lane.PORTABLE, tmp_path, tmp_path / "results") == 0
    report = run_tests.read_report(tmp_path / "results" / "portable.xml")
    assert report == run_tests.Report(tests=1, failures=0, skipped=1)


def test_native_lane_refuses_non_macos_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_tests.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="Darwin"):
        run_tests.run(run_tests.Lane.MACOS_CONTAINMENT, tmp_path, tmp_path / "results")
    assert not (tmp_path / "results").exists()


def test_failed_collection_cannot_reuse_an_old_success_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_tests, "targets", lambda _: ("absent.py",))
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    results = tmp_path / "results"
    results.mkdir()
    stale = results / "portable.xml"
    stale.write_text('<testsuite><testcase name="stale_success"/></testsuite>')
    assert run_tests.run(run_tests.Lane.PORTABLE, tmp_path, results) != 0
    assert "stale_success" not in stale.read_text()


def test_native_lane_does_not_select_provider_sessions() -> None:
    selected = run_tests.targets(run_tests.Lane.MACOS_CONTAINMENT)
    assert "tests/test_native_verification_broker.py" in selected
    assert not any("claude_code" in target for target in selected)
    assert all("test_host_verification_receipts" not in target for target in selected)


def test_empty_or_failed_reports_refuse_even_if_pytest_returned_zero() -> None:
    for report in (run_tests.Report(0, 0, 0), run_tests.Report(1, 1, 0)):
        with pytest.raises(ValueError):
            run_tests.validate_report(run_tests.Lane.PORTABLE, report)

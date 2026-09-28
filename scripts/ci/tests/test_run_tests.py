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


@pytest.mark.parametrize(("index", "count"), ((-1, 4), (4, 4), (0, 0), (0, -1), (1, 1)))
def test_invalid_shard_is_unrepresentable(index: int, count: int) -> None:
    with pytest.raises(ValueError, match="shard count"):
        run_tests.Shard(index=index, count=count)


def test_native_qualification_cannot_be_partially_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_tests.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="cannot be sharded"):
        run_tests.run(run_tests.Lane.MACOS_CONTAINMENT, tmp_path, tmp_path, run_tests.Shard(0, 4))


def test_empty_shard_refuses_instead_of_reporting_success() -> None:
    with pytest.raises(ValueError, match="no test files"):
        run_tests.Shard(3, 4).select(("one.py", "two.py"))


def test_file_shards_obey_real_pytest_config_and_exactly_partition_all_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\npython_files = case_*.py\n")
    (tmp_path / "test_not_selected.py").write_text("raise AssertionError('wrong discovery')")
    for directory in ("tests", "scripts/verifier_uid", "scripts/ci/tests"):
        target = tmp_path / directory
        target.mkdir(parents=True, exist_ok=True)
        # Equal basenames in different roots must remain distinct files and nodes.
        for filename in ("case_one.py", "case_two.py"):
            (target / filename).write_text(
                "import pytest\n@pytest.mark.parametrize('value', [0, 1])\n"
                "def test_proof(value): assert value in (0, 1)\n"
            )
    # Importlib mode intentionally permits same basename across non-package roots.
    monkeypatch.setenv("PYTEST_ADDOPTS", "--import-mode=importlib")
    full = run_tests.collect(tmp_path, run_tests.targets(run_tests.Lane.PORTABLE))
    assert len(full.files) == 6
    assert len(full.nodeids) == 12
    seen: set[str] = set()
    for index in range(4):
        shard = run_tests.Shard(index, 4)
        selected = shard.select(full.files)
        assert selected == shard.select(tuple(reversed(full.files)))
        partition = run_tests.collect(tmp_path, selected)
        assert not (seen & set(partition.nodeids))
        seen.update(partition.nodeids)
        assert run_tests.run(run_tests.Lane.PORTABLE, tmp_path, tmp_path / "results", shard) == 0
        retained = run_tests.read_report(tmp_path / "results" / f"portable-{index}-of-4.xml")
        assert retained.tests == len(partition.nodeids)
    assert seen == set(full.nodeids)


def test_sharding_preserves_collection_failures(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_broken.py").write_text("this is not valid Python !")
    with pytest.raises(ValueError, match="collection failed"):
        run_tests.collect(tmp_path, ("test_broken.py",))

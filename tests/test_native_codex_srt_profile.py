# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real child-pytest checks of the profile gate, without requiring a native host."""

from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_SKIP_PROBE = _TESTS / "fixtures/native_profile_skip_probe.py"


@pytest.mark.parametrize("native", [False, True], ids=["ordinary-offline", "native-release"])
def test_explicit_profile_turns_the_negative_control_skip_into_failure(
    pytester: pytest.Pytester, native: bool
) -> None:
    # Test the real release report hooks independently of machine admission.
    # No fabricated native runtime or passing prerequisite is introduced.
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {str(_TESTS)!r})\n"
        "from native_codex_srt_profile import (pytest_addoption, "
        "pytest_collection_modifyitems, pytest_runtest_makereport)\n"
    )
    pytester.makeini("[pytest]\nmarkers = native_codex_srt: native acceptance proof\n")
    pytester.makepyfile(native_profile_skip_probe=_SKIP_PROBE.read_text())
    flags = ["--run-native-codex-srt"] if native else []
    result = pytester.runpytest_subprocess("native_profile_skip_probe.py", "-q", *flags)
    if native:
        assert result.ret == pytest.ExitCode.TESTS_FAILED
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*Explicit native Codex/SRT acceptance cannot count*"])
    else:
        assert result.ret == pytest.ExitCode.OK
        result.assert_outcomes(skipped=1)


def test_full_profile_fails_on_missing_prerequisites_before_running_the_probe(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("LOCAL_AGENT_SRT_PROBE_ROOT", "LOCAL_AGENT_SRT_PROBE_NODE"):
        monkeypatch.delenv(key, raising=False)
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {str(_TESTS)!r})\n"
        "pytest_plugins = ('native_codex_srt_profile',)\n"
    )
    pytester.makepyfile(native_profile_skip_probe=_SKIP_PROBE.read_text())
    result = pytester.runpytest_subprocess(
        "native_profile_skip_probe.py", "--run-native-codex-srt", "-q"
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*missing prerequisites are not a passing or skipped*"])

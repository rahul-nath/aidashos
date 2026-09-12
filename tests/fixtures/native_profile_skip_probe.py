# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit subprocess fixture: the native profile must turn this skip into failure.

Not named test_*.py, so ordinary collection never treats this intentional
negative control as an acceptance scenario.
"""

import pytest


@pytest.mark.native_codex_srt
@pytest.mark.skip(reason="synthetic unavailable native proof")
def test_skipped_proof_cannot_pass_explicit_native_profile() -> None:
    raise AssertionError("the skip is applied before this body")

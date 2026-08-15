# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from local_first_agent_os.timeout_policy import OperationKind, infer_timeout_budget


def test_coordination_budget_is_not_widened_like_a_model_turn() -> None:
    budget = infer_timeout_budget(OperationKind.COORDINATION, expected_seconds=300)

    assert budget.timeout_seconds == 30
    assert budget.retryable is True
    assert budget.retry_attempts == 3


def test_application_http_budget_infers_headroom_from_expected_duration() -> None:
    budget = infer_timeout_budget(OperationKind.HTTP_APPLICATION, expected_seconds=100)

    assert budget.timeout_seconds == 200


def test_progress_assessment_has_its_own_model_sized_ceiling() -> None:
    budget = infer_timeout_budget(OperationKind.PROGRESS_ASSESSMENT, expected_seconds=500)

    assert budget.timeout_seconds == 600
    frontier_timeout = infer_timeout_budget(OperationKind.FRONTIER_MODEL).timeout_seconds
    assert budget.timeout_seconds < frontier_timeout


def test_expected_duration_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        infer_timeout_budget(OperationKind.HTTP_APPLICATION, expected_seconds=0)

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The standalone governed door is closing, on a dial rather than an event.

docs/completed/governed_saga_door_retirement_gawd.md owns the schedule. These pin the
dial's three positions, the default, and that the directive actually consults
the policy - a posture nothing reads is a setting, not a retirement.
"""

from __future__ import annotations

import inspect

from local_first_agent_os.settings import Settings
from local_first_agent_os.vocabulary import GovernedSagaDoorPosture
from local_first_agent_os.workflow.governed_door import (
    RETIREMENT_DOC,
    RETIREMENT_PROOF_WORK_UNIT_ID,
    governed_saga_door_notice,
    governed_saga_door_refusal,
)


def test_the_default_posture_is_retired_after_production_proof() -> None:
    """Governed document work now has one default control-flow owner."""

    assert Settings.model_fields["governed_saga_door"].default is GovernedSagaDoorPosture.RETIRED
    assert RETIREMENT_PROOF_WORK_UNIT_ID == "2f8e57d35257795531717cfc796ef3ac"


def test_a_retired_door_refuses_with_the_replacement_commands() -> None:
    refusal = governed_saga_door_refusal(GovernedSagaDoorPosture.RETIRED)

    assert refusal is not None
    assert "compile_design_doc" in refusal
    assert "start_work_unit" in refusal
    assert RETIREMENT_DOC in refusal
    assert "LOCAL_AGENT_GOVERNED_SAGA_DOOR" not in refusal


def test_legacy_posture_values_cannot_reopen_the_removed_lane() -> None:
    assert governed_saga_door_refusal(GovernedSagaDoorPosture.OPEN)
    assert governed_saga_door_refusal(GovernedSagaDoorPosture.DEPRECATED)


def test_the_removed_lane_never_stamps_a_deprecation_notice() -> None:
    assert governed_saga_door_notice(GovernedSagaDoorPosture.DEPRECATED) is None
    assert governed_saga_door_notice(GovernedSagaDoorPosture.OPEN) is None
    assert governed_saga_door_notice(GovernedSagaDoorPosture.RETIRED) is None


def test_the_directive_consults_the_policy() -> None:
    """Source-level pin: the door's gate and stamp both live in the directive.

    A posture the directive stopped reading would silently reopen the lane;
    stopping the import or the call sites stops this test.
    """

    from local_first_agent_os.workflow import engine

    source = inspect.getsource(engine.WorkflowEngine._approved_gawd_directive)
    assert "governed_saga_door_refusal(door)" in source
    assert "governed_saga_door_notice(door)" in source

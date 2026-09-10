# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Acceptance checks for driver compatibility without launching any process."""

from __future__ import annotations

from typing import cast

import pytest

from local_first_agent_os.capabilities import Capability
from local_first_agent_os.execution_admission import (
    AuthorizedExecution,
    ExecutionAdmissionError,
    ExecutionAdmissionRefusal,
    ExecutionContract,
    ExecutionDriver,
    admit_execution,
    declared_capabilities_for,
    require_authorized_execution,
)
from local_first_agent_os.spawn_authority import SpawnAuthority


def test_original_verify_grant_refuses_agent_and_inspection_but_admits_command_driver() -> None:
    authority = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND))
    for driver in (ExecutionDriver.PLANNED_DISPATCH, ExecutionDriver.CODEX_INSPECTION):
        refusal = admit_execution(ExecutionContract(driver), authority)
        assert isinstance(refusal, ExecutionAdmissionRefusal)
        assert refusal.missing_capabilities == frozenset((Capability.INVOKE_MODEL,))
    result = admit_execution(ExecutionContract(ExecutionDriver.REGISTERED_VERIFICATION), authority)
    assert isinstance(result, AuthorizedExecution)
    assert result.authority is authority
    assert result.authority.to_names() == ("read_repository", "run_command")


@pytest.mark.parametrize("extra", tuple(Capability))
def test_registered_command_driver_never_acquires_an_additional_capability(
    extra: Capability,
) -> None:
    authority = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND, extra))
    result = admit_execution(ExecutionContract(ExecutionDriver.REGISTERED_VERIFICATION), authority)
    if extra in (Capability.READ_REPOSITORY, Capability.RUN_COMMAND):
        assert isinstance(result, AuthorizedExecution)
        assert result.authority is authority
    else:
        assert isinstance(result, ExecutionAdmissionRefusal)
        assert result.forbidden_capabilities == frozenset((extra,))


@pytest.mark.parametrize("extra", tuple(Capability))
def test_inspection_admits_only_its_read_model_and_operator_contract(extra: Capability) -> None:
    authority = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL, extra))
    result = admit_execution(ExecutionContract(ExecutionDriver.CODEX_INSPECTION), authority)
    if extra in (Capability.READ_REPOSITORY, Capability.INVOKE_MODEL, Capability.ASK_OPERATOR):
        assert isinstance(result, AuthorizedExecution)
        assert result.authority is authority
    else:
        assert isinstance(result, ExecutionAdmissionRefusal)
        assert result.forbidden_capabilities == frozenset((extra,))


@pytest.mark.parametrize("driver", tuple(ExecutionDriver))
def test_minimum_declaration_progresses_and_each_missing_requirement_refuses(
    driver: ExecutionDriver,
) -> None:
    declared = SpawnAuthority.of(declared_capabilities_for(driver))
    admission = admit_execution(ExecutionContract(driver), declared)
    assert isinstance(admission, AuthorizedExecution)
    assert require_authorized_execution(admission, driver) is admission
    for required in declared.capabilities:
        narrowed = SpawnAuthority(declared.capabilities - {required})
        refusal = admit_execution(ExecutionContract(driver), narrowed)
        assert isinstance(refusal, ExecutionAdmissionRefusal)
        assert refusal.missing_capabilities == frozenset((required,))


def test_same_grant_for_another_driver_is_not_an_inspection_authorization() -> None:
    grant = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL))
    planned = admit_execution(ExecutionContract(ExecutionDriver.PLANNED_DISPATCH), grant)
    assert isinstance(planned, AuthorizedExecution)
    with pytest.raises(ValueError, match="cannot authorize codex_inspection"):
        require_authorized_execution(planned, ExecutionDriver.CODEX_INSPECTION)


def test_preflight_proof_cannot_authorize_model_inspection() -> None:
    preflight = admit_execution(
        ExecutionContract(ExecutionDriver.CODEX_TOOL_PREFLIGHT),
        SpawnAuthority.of((Capability.READ_REPOSITORY,)),
    )
    assert isinstance(preflight, AuthorizedExecution)
    with pytest.raises(ValueError, match="cannot authorize codex_inspection"):
        require_authorized_execution(preflight, ExecutionDriver.CODEX_INSPECTION)


def test_proof_cannot_be_constructed_without_admission_owner() -> None:
    with pytest.raises(ValueError, match="admission owner"):
        AuthorizedExecution(
            ExecutionContract(ExecutionDriver.REGISTERED_VERIFICATION),
            SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND)),
            object(),
        )


def test_consumption_revalidates_an_admission_whose_authority_was_corrupted() -> None:
    admission = admit_execution(
        ExecutionContract(ExecutionDriver.CODEX_INSPECTION),
        SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL)),
    )
    assert isinstance(admission, AuthorizedExecution)
    object.__setattr__(
        admission,
        "authority",
        SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND)),
    )
    with pytest.raises(ExecutionAdmissionError) as caught:
        require_authorized_execution(admission, ExecutionDriver.CODEX_INSPECTION)
    assert isinstance(caught.value.refusal, ExecutionAdmissionRefusal)
    assert caught.value.refusal.missing_capabilities == frozenset((Capability.INVOKE_MODEL,))
    assert caught.value.refusal.forbidden_capabilities == frozenset((Capability.RUN_COMMAND,))


def test_raw_driver_and_capability_strings_cannot_enter_typed_admission() -> None:
    with pytest.raises(TypeError, match="ExecutionDriver"):
        ExecutionContract(cast(ExecutionDriver, "registered_verification"))
    with pytest.raises(TypeError, match="typed Capability"):
        admit_execution(
            ExecutionContract(ExecutionDriver.REGISTERED_VERIFICATION),
            SpawnAuthority(
                cast(frozenset[Capability], frozenset(("read_repository", "run_command")))
            ),
        )

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where an approved agent branch enters the refinery.

The scenarios in ``features/refinery_enqueue.feature`` cover both ends of the
boundary: the admission rule on its own, and the approval resolution that drives
it against a real repository and a real ledger.

The outline runs against a real git repository rather than a fake probe. Two of
its rows - a commit the repository does not have, and one that does not descend
from the base the approval named - are questions only git can answer, and
`GitRepositoryProbe` has no other caller to be wrong in front of. The registry is
faked, because `_parse_linked_project_record` already refuses a writable project
that declares no verification commands, so `GATE_NOT_DECLARED` describes a state
configuration can no longer author and a TOML file can no longer express.

The ledger scenarios drive `resolve_approval_request` itself rather than a helper
beside it. Enqueue is bound to resolution on purpose, and a test that called the
enqueue directly would pass whether or not anything called it, which is the
defect this whole design was written against.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from refinery_support import (
    AGENT_BRANCH,
    TargetRepository,
    build_target_repository,
    code_merge_payload,
    write_registry_config,
)

from local_first_agent_os.coordination import store
from local_first_agent_os.coordination.approvals import (
    resolve_approval_request,
    submit_approval_request,
)
from local_first_agent_os.coordination.integration_queue import (
    list_integration_requests,
    read_integration_requests,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.refinery.enqueue import (
    EnqueueAdmitted,
    EnqueueRefusal,
    EnqueueRefused,
    admit_to_queue,
)
from local_first_agent_os.refinery.requests import IntegrationRequestId, Queued
from local_first_agent_os.settings import get_settings

scenarios("features/refinery_enqueue.feature")

_ABSENT_SHA = "0123456789abcdef0123456789abcdef01234567"
"""Well formed and not in any repository this suite builds.

Deliberately a valid object name: the point of the row that uses it is that the
sha passes every check a string can pass and still names nothing, which is the
only way to reach the probe rather than the field validator.
"""


@pytest.fixture
def state() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# The repository every scenario is about
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeRegistry:
    """Four projects over one repository, differing only in what they declare."""

    projects: Mapping[str, LinkedProject]

    def project_by_id(self, project_id: str) -> LinkedProject:
        try:
            return self.projects[project_id]
        except KeyError as exc:
            raise KeyError(f"unknown project {project_id!r}") from exc


def _registry(repository_path: Path) -> _FakeRegistry:
    def project(
        project_id: str,
        *,
        mode: AccessMode,
        commands: list[str],
        branch: str = "main",
    ) -> LinkedProject:
        return LinkedProject(
            id=project_id,
            kind="test_repo",
            path=repository_path,
            status="active",
            access=ProjectAccessPolicy(mode=mode),
            description="refinery fixture",
            verification_commands=commands,
            integrated_branch=branch,
        )

    return _FakeRegistry(
        {
            "writable": project("writable", mode=AccessMode.READ_WRITE, commands=["true"]),
            "readonly": project("readonly", mode=AccessMode.READ_ONLY, commands=["true"]),
            "no_gate": project("no_gate", mode=AccessMode.READ_WRITE, commands=[]),
            # `build_target_repository` builds `main`, `agent/refinery-test`, and
            # `unrelated`. Declaring a trunk it does not have is the shape of a
            # registry entry written for a repository whose default branch is
            # `master`, or of a project someone renamed the trunk of.
            "no_branch": project(
                "no_branch",
                mode=AccessMode.READ_WRITE,
                commands=["true"],
                branch="trunk",
            ),
        }
    )


def _payload(repository: TargetRepository, *, project_id: str, variant: str) -> dict[str, Any]:
    payload = code_merge_payload(repository, project_id=project_id)
    match variant:
        case "complete":
            return payload
        case "no_commit_sha":
            payload.pop("commit_sha")
            return payload
        case "no_provenance":
            payload.pop("intent_id")
            payload.pop("pow_wow_id")
            return payload
        case "abbreviated_sha":
            payload["commit_sha"] = repository.commit_sha[:10]
            return payload
        case "unknown_project":
            payload["target_project_id"] = "a_project_nobody_linked"
            return payload
        case "absent_commit":
            payload["commit_sha"] = _ABSENT_SHA
            return payload
        case "absent_base":
            payload["base_sha"] = _ABSENT_SHA
            return payload
        case "commit_is_base":
            payload["commit_sha"] = repository.base_sha
            return payload
        case "unrelated_commit":
            payload["commit_sha"] = repository.unrelated_sha
            return payload
    raise AssertionError(f"the feature file names a payload variant nothing builds: {variant!r}")


# ---------------------------------------------------------------------------
# The admission rule on its own
# ---------------------------------------------------------------------------


@given(parsers.parse('a linked project that is "{project_id}"'))
def _linked_project(state: dict[str, Any], tmp_path: Path, project_id: str) -> None:
    repository = build_target_repository(tmp_path / "target")
    state["repository"] = repository
    state["registry"] = _registry(repository.path)
    state["project_id"] = project_id


@given(parsers.parse('a CODE_MERGE approval whose payload is "{variant}"'))
def _approval_payload(state: dict[str, Any], variant: str) -> None:
    state["payload"] = _payload(
        state["repository"],
        project_id=state["project_id"],
        variant=variant,
    )


@when("the refinery is asked to admit it")
def _admit(state: dict[str, Any]) -> None:
    state["admission"] = admit_to_queue(
        state["payload"],
        approval_id="approval-refinery",
        request_id=IntegrationRequestId("request-refinery"),
        enqueued_at=1_700_000_000.0,
        registry=state["registry"],
    )


@then(parsers.parse('the admission is "{outcome}"'))
def _admission_is(state: dict[str, Any], outcome: str) -> None:
    admission = state["admission"]
    if outcome == "admitted":
        assert isinstance(admission, EnqueueAdmitted), admission
        return
    assert isinstance(admission, EnqueueRefused), admission
    # `EnqueueRefusal(outcome)` rather than a string compare, so a scenario naming
    # a refusal the enum does not have fails on construction here instead of
    # silently passing against a typo in the other one.
    assert admission.refusal is EnqueueRefusal(outcome)
    assert admission.message.strip(), "a refusal an operator cannot read is not a refusal"


def test_the_outline_accounts_for_every_refusal_the_enum_can_express() -> None:
    """The scenario table and the closed set have to be the same closed set.

    An outline is a sample unless something says it is a census. Without this, a
    new `EnqueueRefusal` member is a refusal with no scenario - reachable in
    production, never once exercised - and a deleted one leaves a row asserting
    against a name that no longer exists. Reading the table back is what makes
    the `@exhaustiveness` tag on it a fact rather than a label.
    """

    feature = Path(__file__).parent / "features" / "refinery_enqueue.feature"
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in feature.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("|")
    ]
    header, *examples = rows
    outcomes = {row[header.index("outcome")] for row in examples}

    assert outcomes - {"admitted"} == {member.value for member in EnqueueRefusal}


@then("the queued request names the approval, the intent, and the pow-wow it came from")
def _admission_carries_provenance(state: dict[str, Any]) -> None:
    admission = state["admission"]
    assert isinstance(admission, EnqueueAdmitted)
    subject = admission.request.subject
    assert subject.approval_id == "approval-refinery"
    assert subject.intent_id == "intent-refinery"
    assert subject.pow_wow_id == "pow-refinery"
    assert subject.milestone_key == "milestone-refinery"
    assert subject.changed_files == ("feature.py",)


@then("the queued request lands the commit rather than the branch name")
def _admission_binds_the_sha(state: dict[str, Any]) -> None:
    admission = state["admission"]
    assert isinstance(admission, EnqueueAdmitted)
    repository: TargetRepository = state["repository"]
    assert admission.request.subject.commit_sha == repository.commit_sha
    assert admission.request.subject.base_head_sha == repository.base_sha


# ---------------------------------------------------------------------------
# The approval resolution that drives it
# ---------------------------------------------------------------------------


@given("a target repository with an approved agent branch")
def _ledger_fixture(
    state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_target_repository(tmp_path / "target")
    write_registry_config(tmp_path / "configs", repository.path, project_id="target")
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(tmp_path / "configs"))
    get_settings.cache_clear()
    store.set_root(str(tmp_path / "coordination"))
    state["repository"] = repository
    state["saga_id"] = create_saga("Land an approved agent branch")["saga_id"]
    state["payload"] = _payload(repository, project_id="target", variant="complete")


@given("the approval payload names a commit the repository does not have")
def _payload_names_an_absent_commit(state: dict[str, Any]) -> None:
    state["payload"] = _payload(
        state["repository"],
        project_id="target",
        variant="absent_commit",
    )


def _submit(state: dict[str, Any], request_type: str) -> str:
    submitted = submit_approval_request(
        state["saga_id"],
        request_type,
        payload=state["payload"] if request_type == "CODE_MERGE" else {"item": "a keyboard"},
        requested_by="dispatcher_runner",
    )
    approval_id = str(submitted["approval_id"])
    state["approval_id"] = approval_id
    return approval_id


@when(parsers.parse("the operator resolves the CODE_MERGE approval to {decision}"))
def _resolve(state: dict[str, Any], decision: str) -> None:
    approval_id = _submit(state, "CODE_MERGE")
    state["resolution"] = resolve_approval_request(
        approval_id,
        approved=decision == "approved",
        resolved_by="operator",
    )


@when("a PURCHASE approval for the same saga is resolved to approved")
def _resolve_purchase(state: dict[str, Any]) -> None:
    approval_id = _submit(state, "PURCHASE")
    state["resolution"] = resolve_approval_request(
        approval_id,
        approved=True,
        resolved_by="operator",
    )


@when("the same commit is enqueued a second time")
def _enqueue_again(state: dict[str, Any]) -> None:
    """A second approval for the same commit, which is replay by any other name.

    Resolving the same approval twice cannot reach the enqueue: the second
    attempt is refused as `already_resolved` before the transaction writes
    anything. So the property under test is stated the way the queue actually
    guarantees it - one live request per commit, whatever produced it - which is
    what the partial unique index says and what a retried dispatcher would hit.
    """

    from local_first_agent_os.coordination.integration_queue import (
        admit_code_merge_approval,
        record_queued_request,
    )

    replay = submit_approval_request(
        state["saga_id"],
        "CODE_MERGE",
        payload=state["payload"],
        requested_by="dispatcher_runner",
    )
    admission = admit_code_merge_approval(
        str(replay["approval_id"]),
        state["payload"],
        enqueued_at=1_700_000_001.0,
    )
    assert isinstance(admission, EnqueueAdmitted), admission
    with store.tx() as connection:
        state["replay_outcome"] = record_queued_request(
            connection,
            admission.request,
            recorded_at=1_700_000_001.0,
        )


def _queued_requests(target_project_id: str = "target") -> tuple[Queued, ...]:
    with store.connect() as connection:
        requests = read_integration_requests(connection, target_project_id=target_project_id)
    return tuple(request for request in requests if isinstance(request, Queued))


def _approval_status(approval_id: str) -> str:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT status FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
    return str(row["status"])


@then("the approval is APPROVED and one request is queued for the project")
def _approved_and_queued(state: dict[str, Any]) -> None:
    assert state["resolution"]["ok"] is True, state["resolution"]
    assert _approval_status(state["approval_id"]) == "APPROVED"
    queued = _queued_requests()
    assert len(queued) == 1
    assert state["resolution"]["integration_request_id"] == queued[0].subject.request_id
    assert state["resolution"]["already_queued"] is False


@then("the queued request binds the exact commit the approval named")
def _queued_binds_the_commit(state: dict[str, Any]) -> None:
    repository: TargetRepository = state["repository"]
    subject = _queued_requests()[0].subject
    assert subject.commit_sha == repository.commit_sha
    assert subject.base_head_sha == repository.base_sha
    assert subject.approval_id == state["approval_id"]
    assert subject.branch_name == AGENT_BRANCH


@then("the project has exactly one queued request")
def _one_queued_request(state: dict[str, Any]) -> None:
    from local_first_agent_os.coordination.integration_queue import AlreadyQueued

    assert isinstance(state["replay_outcome"], AlreadyQueued), state["replay_outcome"]
    queued = _queued_requests()
    assert len(queued) == 1
    assert queued[0].subject.request_id == state["resolution"]["integration_request_id"]


@then("the resolution is refused naming the missing commit")
def _resolution_refused(state: dict[str, Any]) -> None:
    resolution = state["resolution"]
    assert resolution["ok"] is False
    assert resolution["error"] == "integration_enqueue_refused"
    assert resolution["refusal"] == "COMMIT_NOT_IN_REPOSITORY"
    assert _ABSENT_SHA in resolution["message"]


@then("the approval is still PENDING and nothing is queued")
def _still_pending(state: dict[str, Any]) -> None:
    assert _approval_status(state["approval_id"]) == "PENDING"
    assert _queued_requests() == ()


@then("the approval is DENIED and nothing is queued")
def _denied_and_empty(state: dict[str, Any]) -> None:
    assert state["resolution"]["ok"] is True, state["resolution"]
    assert _approval_status(state["approval_id"]) == "DENIED"
    assert _queued_requests() == ()


@then("listing the project's integration requests shows the queued commit")
def _listing_shows_it(state: dict[str, Any]) -> None:
    repository: TargetRepository = state["repository"]
    listed = list_integration_requests(target_project_id="target")
    assert listed["ok"] is True
    assert [row["request_id"] for row in listed["requests"]] == [
        state["resolution"]["integration_request_id"]
    ]
    row = listed["requests"][0]
    assert row["state"] == "QUEUED"
    assert row["commit_sha"] == repository.commit_sha
    assert row["branch_name"] == AGENT_BRANCH
    assert row["approval_id"] == state["approval_id"]


@then("listing a state nothing is in shows nothing")
def _listing_another_state_shows_nothing(state: dict[str, Any]) -> None:
    assert list_integration_requests(state="INTEGRATED")["requests"] == []
    refused = list_integration_requests(state="NOT_A_STATE")
    assert refused["ok"] is False
    assert refused["error"] == "invalid_state"


@then("the approval is APPROVED and nothing is queued")
def _approved_and_empty(state: dict[str, Any]) -> None:
    assert state["resolution"]["ok"] is True, state["resolution"]
    assert _approval_status(state["approval_id"]) == "APPROVED"
    assert "integration_request_id" not in state["resolution"]
    assert _queued_requests() == ()


# ---------------------------------------------------------------------------
# The row, which is the only place the five states are written down
# ---------------------------------------------------------------------------


def test_every_state_survives_a_round_trip_through_the_row(tmp_path: Path) -> None:
    """The codec is exhaustive or the table is a place requests go to be lost.

    Only `Queued` is written today, and writing the other four now is not
    speculation: the `state` column already names all five, the partial unique
    index already distinguishes the live two from the terminal three, and a codec
    that handled one of them would be a landmine for the driver that writes the
    rest. Round-tripping every variant is what makes `assert_never` in
    `_encode_state` and `_decode_state` mean something.
    """

    from local_first_agent_os.coordination.integration_queue import (
        _decode_state,
        _encode_state,
    )
    from local_first_agent_os.refinery.requests import (
        BisectedOut,
        GateFailed,
        InFlight,
        Integrated,
        IntegrationAttemptId,
        IntegrationBatchId,
        IntegrationSubject,
        MergeConflict,
        WithdrawalReason,
        Withdrawn,
        state_of,
    )

    subject = IntegrationSubject(
        request_id=IntegrationRequestId("request-round-trip"),
        target_project_id="target",
        branch_name="agent/round-trip",
        base_head_sha="a" * 40,
        commit_sha="b" * 40,
        approval_id="approval-round-trip",
        intent_id="intent-round-trip",
        pow_wow_id="pow-round-trip",
        milestone_key=None,
        changed_files=("feature.py",),
        enqueued_at=1_700_000_000.0,
    )
    batch_id = IntegrationBatchId("batch-1")
    variants = [
        Queued(subject=subject),
        InFlight(subject=subject, batch_id=batch_id, attempt_id=IntegrationAttemptId("attempt-1")),
        Integrated(
            subject=subject,
            batch_id=batch_id,
            integration_commit_sha="c" * 40,
            integrated_at=1_700_000_002.0,
        ),
        BisectedOut(
            subject=subject,
            batch_id=batch_id,
            cause=MergeConflict(conflicted_paths=("feature.py",)),
            stack_beneath=(IntegrationRequestId("request-earlier"),),
            stack_base_sha="d" * 40,
            evidence_artifact_id="artifact-1",
            bisected_at=1_700_000_003.0,
        ),
        BisectedOut(
            subject=subject,
            batch_id=batch_id,
            cause=GateFailed(command="pytest -q", exit_code=1, output_excerpt="1 failed"),
            stack_beneath=(),
            stack_base_sha="d" * 40,
            evidence_artifact_id="artifact-2",
            bisected_at=1_700_000_004.0,
        ),
        Withdrawn(
            subject=subject,
            reason=WithdrawalReason.APPROVAL_REVOKED,
            withdrawn_at=1_700_000_005.0,
        ),
    ]

    for variant in variants:
        encoded = json.loads(json.dumps(_encode_state(variant)))
        assert _decode_state(state_of(variant), subject, encoded) == variant


def test_a_second_live_request_for_one_commit_is_refused_by_the_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotency rule is a constraint, not a check somebody remembers.

    Driven through two distinct request ids rather than through the enqueue path,
    because what is being asserted is that the index refuses the second write
    even when the caller believes it is writing something new. A select-then-
    insert would pass a test that went through the enqueue and still lose this
    race.
    """

    from local_first_agent_os.coordination.integration_queue import (
        AlreadyQueued,
        IntegrationEnqueued,
        record_queued_request,
    )
    from local_first_agent_os.refinery.requests import IntegrationSubject

    store.set_root(str(tmp_path / "coordination"))
    saga_id = str(create_saga("Refuse a duplicate live request")["saga_id"])
    approval_id = str(
        submit_approval_request(saga_id, "CODE_MERGE", payload={}, requested_by="test")[
            "approval_id"
        ]
    )

    def request(request_id: str) -> Queued:
        return Queued(
            IntegrationSubject(
                request_id=IntegrationRequestId(request_id),
                target_project_id="target",
                branch_name="agent/duplicate",
                base_head_sha="a" * 40,
                commit_sha="b" * 40,
                approval_id=approval_id,
                intent_id="intent-duplicate",
                pow_wow_id="pow-duplicate",
                milestone_key=None,
                changed_files=(),
                enqueued_at=1_700_000_000.0,
            )
        )

    with store.tx() as connection:
        first = record_queued_request(connection, request(str(uuid.uuid4())), recorded_at=1.0)
    with store.tx() as connection:
        second = record_queued_request(connection, request(str(uuid.uuid4())), recorded_at=2.0)

    assert isinstance(first, IntegrationEnqueued)
    assert isinstance(second, AlreadyQueued)
    assert second.request.subject.request_id == first.request.subject.request_id

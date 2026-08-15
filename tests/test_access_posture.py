# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether access control refuses, or only records that it would have.

One decision variable per test: which posture is set, whether the capability is
one `OBSERVING` may relax, whether a relaxed refusal leaves a record, and whether
the process says which posture it is in.

The third of those is the one that makes the posture a posture. A permissive mode
that allows silently is a disabled feature with extra steps, and this repository
has spent its recent history removing exactly that shape.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from local_first_agent_os.access_posture import (
    ALWAYS_ENFORCED,
    AccessPosture,
    announce_posture,
    relaxable,
    resolve,
)
from local_first_agent_os.capabilities import Capability
from local_first_agent_os.capability_gate import (
    CapabilityDenied,
    CapabilityGranted,
    check_capability,
)
from local_first_agent_os.settings import Settings, get_settings

# `POLICIES.md` denies this to every principal through its default section, so
# it is a refusal that fires without any ledger setup.
DENIED_BY_THE_DOCUMENT = Capability.PUBLISH_DEPLOYMENT
_POW_WOW_ID = "pow-wow-access-posture-test"


def shipped() -> Settings:
    """Settings as the code declares them, with no environment override.

    `conftest._pin_access_posture` already forces the suite to run enforcing, so
    this exists for the narrower question these last tests ask: what does a fresh
    checkout get before anybody sets anything.
    """

    return Settings(access_posture=AccessPosture.ENFORCING)


@pytest.fixture()
def enforcing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit rather than inherited, so the test states its own premise."""

    monkeypatch.setenv("LOCAL_AGENT_ACCESS_POSTURE", "enforcing")
    get_settings.cache_clear()


@pytest.fixture()
def observing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the process in `OBSERVING`, the way an operator would.

    Through the environment and the settings cache rather than by patching the
    gate, so the test exercises the wiring an operator actually uses.
    """

    monkeypatch.setenv("LOCAL_AGENT_ACCESS_POSTURE", "observing")
    get_settings.cache_clear()


# Variable 1: which posture is set.
def test_enforcing_refuses_what_the_document_denies(
    work_unit_ledger: Path, enforcing: None
) -> None:
    verdict = check_capability(
        agent_name="claude",
        agent_role="implementer",
        capability=DENIED_BY_THE_DOCUMENT,
        pow_wow_id=_POW_WOW_ID,
    )

    assert isinstance(verdict, CapabilityDenied)
    assert "POLICIES.md" in verdict.reason


def test_observing_allows_the_same_refusal(work_unit_ledger: Path, observing: None) -> None:
    """The point of the posture, and the reason it exists at all.

    A rule that is right in general and wrong in this case costs an operator a
    manual test run, and an operator who loses an afternoon to one learns to
    route around the mechanism rather than to trust it.
    """

    verdict = check_capability(
        agent_name="claude",
        agent_role="implementer",
        capability=DENIED_BY_THE_DOCUMENT,
        pow_wow_id=_POW_WOW_ID,
    )

    assert isinstance(verdict, CapabilityGranted)


# Variable 2: whether `OBSERVING` is allowed to relax this capability.
@pytest.mark.parametrize("capability", sorted(ALWAYS_ENFORCED))
def test_observing_does_not_relax_an_irreversible_capability(
    work_unit_ledger: Path, observing: None, capability: Capability
) -> None:
    """A posture that yields on these is an off switch, not a posture.

    Money spent, a message sent, a credential read, a file destroyed. None can be
    taken back by reading a log afterwards, so "we recorded it" is not a remedy
    and the posture does not offer one.
    """

    verdict = check_capability(
        agent_name="claude",
        agent_role="implementer",
        capability=capability,
        pow_wow_id=_POW_WOW_ID,
    )

    assert isinstance(verdict, CapabilityDenied), capability


def test_the_reversible_gated_capabilities_stay_relaxable() -> None:
    """Deploying and merging are exactly what a full-path test needs to get through.

    Both are reversible and both already sit behind an approval gate, so keeping
    them in the always-enforced set would mean the posture could not do the one
    job it was added for.
    """

    assert relaxable(Capability.PUBLISH_DEPLOYMENT)
    assert relaxable(Capability.MERGE_TO_MAIN)
    assert relaxable(Capability.RUN_COMMAND)
    assert relaxable(Capability.WRITE_REPOSITORY)


def test_resolve_narrows_rather_than_widens() -> None:
    """`ENFORCING` is never weakened by asking about a particular capability."""

    for capability in Capability:
        assert resolve(AccessPosture.ENFORCING, capability) is AccessPosture.ENFORCING

    relaxed = {
        c for c in Capability if resolve(AccessPosture.OBSERVING, c) is AccessPosture.OBSERVING
    }
    assert relaxed == set(Capability) - ALWAYS_ENFORCED


# Variable 3: whether a relaxed refusal leaves a record.
def test_a_relaxed_refusal_is_recorded_with_the_reason_it_would_have_given(
    work_unit_ledger: Path,
    observing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without this the posture is a disabled feature with a nicer name.

    The record is what makes an `OBSERVING` run useful afterwards: it is the list
    of what `ENFORCING` would have stopped, which is the evidence needed to
    decide whether the rules are ready to be trusted.
    """

    with caplog.at_level(logging.WARNING, logger="local_first_agent_os.access_posture"):
        check_capability(
            agent_name="claude",
            agent_role="implementer",
            capability=DENIED_BY_THE_DOCUMENT,
            pow_wow_id=_POW_WOW_ID,
        )

    records = [r for r in caplog.records if r.message == "access_posture_observed_refusal"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].__dict__["capability"] == DENIED_BY_THE_DOCUMENT.value
    assert "POLICIES.md" in records[0].__dict__["refusal_reason"]


def test_enforcing_records_nothing_because_nothing_was_let_through(
    work_unit_ledger: Path,
    enforcing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="local_first_agent_os.access_posture"):
        check_capability(
            agent_name="claude",
            agent_role="implementer",
            capability=DENIED_BY_THE_DOCUMENT,
            pow_wow_id=_POW_WOW_ID,
        )

    assert [r for r in caplog.records if r.message == "access_posture_observed_refusal"] == []


# Variable 4: whether the process says which posture it is in.
def test_observing_announces_itself_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """The failure mode being designed against is forgetting the posture is on.

    So it is a warning, it names what is still enforced, and it says how to put
    the system back. Silence here is how a test posture survives into a real run.
    """

    with caplog.at_level(logging.INFO, logger="local_first_agent_os.access_posture"):
        announce_posture(AccessPosture.OBSERVING)

    (record,) = [r for r in caplog.records if r.message == "access_posture"]
    assert record.levelno == logging.WARNING
    detail = record.__dict__["detail"]
    assert "OBSERVING" in detail
    assert "LOCAL_AGENT_ACCESS_POSTURE=enforcing" in detail
    for capability in ALWAYS_ENFORCED:
        assert capability.value in detail


def test_enforcing_announces_itself_too(caplog: pytest.LogCaptureFixture) -> None:
    """Because no message is indistinguishable from a broken announcement."""

    with caplog.at_level(logging.INFO, logger="local_first_agent_os.access_posture"):
        announce_posture(AccessPosture.ENFORCING)

    (record,) = [r for r in caplog.records if r.message == "access_posture"]
    assert record.levelno == logging.INFO


# Variable 5: the shipped default.
def test_the_shipped_default_is_enforcing() -> None:
    """A permissive default would make every other test in this file theatre."""

    assert Settings.model_fields["access_posture"].default is AccessPosture.ENFORCING


# Variable 6: a bound the process clock never lets a milestone reach.
def test_no_milestone_budget_is_unreachable_under_the_shipped_process_cap() -> None:
    """A declared timeout the process is killed before reaching is not a timeout.

    `implement.code_change` declares 5400s. `saga_task_timeout_seconds` defaults
    to 3600. The supervisor's clock always wins, so the milestone parked BLOCKED
    with `dispatch_paused` at sixty minutes and its own ninety-minute budget
    could never fire - a bound that cannot be reached, which is the same shape as
    a check that cannot fail.

    `review.operator` is excluded because its 86400s is an operator's wait rather
    than a process's: it routes to no runtime and spawns nothing, so no process
    clock competes with it.
    """

    from local_first_agent_os.work_units.executors import EXECUTOR_REGISTRY

    cap = Settings.model_fields["saga_task_timeout_seconds"].default
    unreachable = {
        str(kind): declaration.timeout_seconds
        for kind, declaration in EXECUTOR_REGISTRY.items()
        if str(kind) != "review.operator"
        and getattr(declaration, "timeout_seconds", 0)
        and declaration.timeout_seconds > cap
    }

    assert unreachable == {}, (
        f"{unreachable} declare budgets above the {cap}s process cap, so the "
        "supervisor kills the run before the milestone's own bound can fire; "
        "raise LOCAL_AGENT_SAGA_TASK_TIMEOUT_SECONDS or lower the declaration"
    )

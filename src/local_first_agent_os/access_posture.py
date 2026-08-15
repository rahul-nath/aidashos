# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether access control refuses an action or only records that it would have.

The access-control machinery in this repository is young. It is worth having and
it is not yet worth trusting with an operator's afternoon: a refusal that is
correct in principle and wrong in this particular case costs a manual test run
and teaches the operator to route around the mechanism, which is worse than not
having it.

So enforcement is a *posture*, and the posture is a state the system is in rather
than a knob on a subsystem. ``OBSERVING`` is not "access control off". It is
"access control runs, decides, and writes down every refusal it declined to
make", which is the state that produces the evidence needed to make ``ENFORCING``
trustworthy. A run in ``OBSERVING`` should end with a list of exactly what would
have been blocked.

Two things this deliberately does not do.

It does not relax everything. ``ALWAYS_ENFORCED`` names the refusals that hold in
every posture, because relaxing them is not a false positive an operator wants to
push past - it is the behaviour the mechanism exists for. A posture that yields
on those is not a posture, it is an off switch.

And it does not hide. A process in ``OBSERVING`` says so at startup and on
``/health``, because the failure mode of a permissive mode is forgetting it is
on, and a silent hole is exactly what this repository has spent its recent
history removing.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import assert_never

from .capabilities import Capability

logger = logging.getLogger(__name__)


class AccessPosture(StrEnum):
    """How the system treats an access-control refusal.

    Named for the working state rather than the mechanism, so that neither value
    reads as a default and turning one on is a decision somebody made.
    """

    ENFORCING = "enforcing"
    """A refusal stops the action. The posture to ship."""

    OBSERVING = "observing"
    """A refusal is recorded and the action proceeds.

    For manual UX testing and for the period in which the rules are still being
    learned. Everything a run in this posture allowed-but-would-have-refused is
    recoverable afterwards.
    """


# Refusals that hold in every posture.
#
# The test is not "is this dangerous" - most of the enum is dangerous in the
# right hands. It is "would an operator ever want to push past this during a
# manual test run". Nobody debugging a stuck milestone wants the system to have
# spent their money or emailed somebody because a posture was left on, and
# discovering it afterwards from an observation log is not a remedy.
ALWAYS_ENFORCED: frozenset[Capability] = frozenset(
    {
        Capability.SPEND_MONEY,
        Capability.EXTERNAL_COMMUNICATIONS,
        Capability.ACCESS_CREDENTIALS,
        Capability.DESTRUCTIVE_FILE_OPERATIONS,
    }
)
"""Never relaxed, whatever the posture says.

Deliberately not `publish_deployment` or `merge_to_main`. Those are exactly the
things an operator testing the full path *needs* to get through, they are
reversible, and an approval gate already sits in front of them. The four here
are the irreversible ones: money spent, a message sent, a credential read, a file
destroyed. None of them can be taken back by noticing afterwards.
"""


def relaxable(capability: Capability) -> bool:
    """Whether ``OBSERVING`` is allowed to let this one through."""

    return capability not in ALWAYS_ENFORCED


def resolve(posture: AccessPosture, capability: Capability) -> AccessPosture:
    """The posture that actually applies to this capability.

    A single place to ask, so that no call site has to remember to consult
    `ALWAYS_ENFORCED` and none can forget to. `OBSERVING` narrows to `ENFORCING`
    for anything irreversible; every other combination is itself.
    """

    match posture:
        case AccessPosture.ENFORCING:
            return AccessPosture.ENFORCING
        case AccessPosture.OBSERVING:
            return AccessPosture.OBSERVING if relaxable(capability) else AccessPosture.ENFORCING
        case _:
            assert_never(posture)


def announce_posture(posture: AccessPosture) -> None:
    """Say which posture this process is in, once, at startup.

    A permissive posture that nobody remembers enabling is the failure mode
    worth designing against, so `OBSERVING` announces itself at WARNING and names
    what it will still refuse. `ENFORCING` says so at INFO rather than staying
    silent: "no message" is indistinguishable from "the announcement broke".
    """

    match posture:
        case AccessPosture.ENFORCING:
            logger.info(
                "access_posture",
                extra={"detail": "access posture: enforcing", "posture": posture.value},
            )
        case AccessPosture.OBSERVING:
            still = ", ".join(sorted(item.value for item in ALWAYS_ENFORCED))
            logger.warning(
                "access_posture",
                extra={
                    "detail": (
                        "access posture: OBSERVING - refusals are recorded, not applied. "
                        f"Still enforced: {still}. Set LOCAL_AGENT_ACCESS_POSTURE=enforcing "
                        "to restore."
                    ),
                    "posture": posture.value,
                },
            )
        case _:
            assert_never(posture)


def record_unenforced_refusal(
    *,
    capability: Capability,
    agent_name: str,
    pow_wow_id: str | None,
    reason: str,
) -> None:
    """Write down a refusal the posture declined to make.

    At WARNING, because this is the one thing about an `OBSERVING` run that must
    not be skimmed past: it is the record of what an `ENFORCING` run would have
    stopped, and it is the whole reason the posture is a posture rather than a
    disabled feature.

    Logging rather than a ledger row on purpose. A refusal that did not happen is
    not a fact about the work - the work proceeded - and writing it to the
    coordination ledger would put a non-event in the durable history that
    reconciliation and the crash reconciler both read.
    """

    logger.warning(
        "access_posture_observed_refusal",
        extra={
            "detail": (
                f"posture=observing allowed {capability.value!r} for agent "
                f"{agent_name!r}; enforcing would have refused it"
            ),
            "capability": capability.value,
            "agent_name": agent_name,
            "pow_wow_id": pow_wow_id or "",
            "refusal_reason": reason,
        },
    )


__all__ = [
    "ALWAYS_ENFORCED",
    "AccessPosture",
    "announce_posture",
    "record_unenforced_refusal",
    "relaxable",
    "resolve",
]

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The durable execution ledger in the coordination command shape.

Agents reason about execution history constantly and are wrong about it often:
"this has never run" is a claim about a durable record, not about the code, and
the record is right there. This is the command that reads it, so an agent can
check instead of infer.

It answers with tallies only, because the same table holds ingress payloads. See
``durable_execution_ledger`` for why that is a property of the module and of the
connection rather than a rule callers are asked to follow.
"""

from __future__ import annotations

from typing import Any

from ..durable_execution_ledger import (
    LedgerUnavailable,
)
from ..durable_execution_ledger import (
    read_execution_ledger as read_ledger,
)
from .store import err, ok


def read_execution_ledger(workflow_name: str | None = None) -> dict[str, Any]:
    """Tally every workflow and step DBOS has executed for this application.

    ``workflow_name`` narrows the workflow tallies and adds ``has_ever_executed``,
    which is the question this command exists to answer. Step tallies come back
    whole either way: they are the evidence that a workflow did something rather
    than merely starting, and there are few enough to read at a glance.
    """

    reading = read_ledger()
    if isinstance(reading, LedgerUnavailable):
        return err("execution_ledger_unavailable", message=reading.reason)
    if workflow_name is None:
        return ok(**reading.to_payload())
    return ok(
        workflow_name=workflow_name,
        has_ever_executed=reading.has_ever_executed(workflow_name),
        workflows=[tally.to_payload() for tally in reading.tallies_for(workflow_name)],
        steps=[tally.to_payload() for tally in reading.steps],
    )


__all__ = ["read_execution_ledger"]

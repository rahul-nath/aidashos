# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The integration queue: landing the branches that N parallel milestones produced.

This is integration and not selection. N agents did N different things and the
refinery combines them; it never picks a winner among N answers to one question,
which ``submit_dispatch_intent`` refuses today with "code fan-out has no merge
semantics" and which this package does not reopen.

What is here is the datatype, the pure rules, and the decision at the queue's
one entrance. The git plumbing beyond two read-only probes is deliberately not,
because a bisect rule that is wrong is worse when it is also moving branches
around.

Module map:

- ``requests``: what a request to land one commit is, at each point in its life,
  and the only transitions between those points.
- ``queue``: which requests form the next batch, and in what order.
- ``bisect``: which stack to build next, and who to blame when one is not green.
- ``enqueue``: whether a resolved ``CODE_MERGE`` may join the queue at all, and
  the sentence an operator reads when it may not.
- ``stack``: the throwaway integration worktree, the ordered merges by sha, the
  conflict abort, and the containment-and-provenance check that replaces
  tip-equality for a batch of more than one. It decides nothing.

Two modules in this package are deliberately **not** re-exported here:
``driver``, which takes one batch to a verdict, and ``loop``, which is the
resident process that drives it. Import them by their own paths.

That is a layering rule and not a style preference.
``coordination/integration_queue.py`` imports ``refinery.enqueue``, because the
rows are written beside the rest of the ledger's SQL rather than in here, which
is what keeps everything above a set of rules a test can drive without a
database. ``driver`` and ``loop`` sit on the far side of that import: they need
the rows. Re-exporting them from this file would make importing any rule pull in
the ledger, and importing the ledger pull in the driver, which is a cycle that
only shows up depending on which module a process happened to import first.

Not here at all, and named so the gap is legible rather than merely absent:

- the verification gate on a stack, and the one ``--ff-only`` advance of the
  integrated branch. Milestone 3 builds and proves the stack and stops; a stack
  that builds cleanly is abandoned under
  a typed gate verdict followed by one exact fast-forward.
- the ``MERGE_APPROVED -> MERGED`` transition that nothing performs.
"""

from .bisect import (
    AwaitingStack,
    IntegrationOutcome,
    IntegrationProgress,
    Isolation,
    RunAbandoned,
    RunCompleted,
    StackAbandoned,
    StackAbandonment,
    StackAttempt,
    StackGateRed,
    StackLanded,
    StackMergeConflict,
    StackOutcome,
    begin_integration,
    decided_request_ids,
    record_stack_outcome,
)
from .enqueue import (
    EnqueueAdmission,
    EnqueueAdmitted,
    EnqueueRefusal,
    EnqueueRefused,
    GitRepositoryProbe,
    ProjectRegistry,
    RepositoryProbe,
    admit_to_queue,
)
from .queue import BatchSelection, NothingToIntegrate, SelectedBatch, select_next_batch
from .requests import (
    BisectCause,
    BisectedOut,
    GateFailed,
    InFlight,
    Integrated,
    IntegrationAttemptId,
    IntegrationBatchId,
    IntegrationRequest,
    IntegrationRequestId,
    IntegrationRequestState,
    IntegrationSubject,
    MergeConflict,
    Queued,
    WithdrawalReason,
    Withdrawn,
    next_integration_request_states,
    require_integration_transition,
    state_of,
)
from .stack import (
    GitFailure,
    IntegrationWorkspace,
    ProvenanceBroken,
    ProvenanceHeld,
    ProvenanceVerdict,
    StackBuilder,
    StackBuildOutcome,
    StackBuilt,
    StackConflicted,
    StackUnbuildable,
)

__all__ = [
    "AwaitingStack",
    "BatchSelection",
    "BisectCause",
    "BisectedOut",
    "EnqueueAdmission",
    "EnqueueAdmitted",
    "EnqueueRefusal",
    "EnqueueRefused",
    "GateFailed",
    "GitFailure",
    "GitRepositoryProbe",
    "InFlight",
    "Integrated",
    "IntegrationAttemptId",
    "IntegrationBatchId",
    "IntegrationOutcome",
    "IntegrationProgress",
    "IntegrationRequest",
    "IntegrationRequestId",
    "IntegrationRequestState",
    "IntegrationSubject",
    "IntegrationWorkspace",
    "Isolation",
    "MergeConflict",
    "NothingToIntegrate",
    "ProjectRegistry",
    "ProvenanceBroken",
    "ProvenanceHeld",
    "ProvenanceVerdict",
    "Queued",
    "RepositoryProbe",
    "RunAbandoned",
    "RunCompleted",
    "SelectedBatch",
    "StackAbandoned",
    "StackAbandonment",
    "StackAttempt",
    "StackBuildOutcome",
    "StackBuilder",
    "StackBuilt",
    "StackConflicted",
    "StackGateRed",
    "StackLanded",
    "StackMergeConflict",
    "StackOutcome",
    "StackUnbuildable",
    "WithdrawalReason",
    "Withdrawn",
    "admit_to_queue",
    "begin_integration",
    "decided_request_ids",
    "next_integration_request_states",
    "record_stack_outcome",
    "require_integration_transition",
    "select_next_batch",
    "state_of",
]

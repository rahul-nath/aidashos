# Golden path design doc

Target project: local_first_agent_os

A DesignDoc whose milestones a real dispatch can actually finish.

The acceptance document beside this one cannot: milestones B and C require
`source_patch`, which the evidence gate grants only when the run reports non-empty
`changed_files`, and D requires `test_result`, granted only on verification
commands and output.
Those refusals are correct - an agent that changed nothing has not patched anything - and they make that document a test of the compiler and the lifecycle rather than of a live dispatch.
This one asks only for evidence a bounded advisory turn genuinely produces, plus the one artifact no run may ever produce.

## Requirements

- Drive one WorkUnit from a written document to SUCCEEDED through the resident loops.

## Constraints

- Every milestone's evidence must be something its executor can honestly produce.

## Acceptance criteria

- The plan, the review gate, and the delivery record are each recorded as artifacts.

## Non goals

- Changing any file in a target repository.

## Milestone A: plan the work

Phase: PLAN
Acceptance: a written implementation plan exists
Artifacts: implementation_plan

## Milestone B: operator review

Phase: REVIEW
Depends on: A
Executor: review.operator
Approval: required
Acceptance: an operator approved the plan
Artifacts: operator_approval

<!--
There is no DELIVER milestone, and that is a finding rather than an omission.
`deliver.artifact` routes to a `code` dispatch, which the decomposition planner
expands into an implementer plus a staff review, and the review gate fails closed
when the reviewer produces no typed `review_result.v1` evidence.
A mock model produces prose, so a DELIVER milestone here can only pass by
weakening that gate - which is the "check that cannot fail" defect the previous
handoff was written about.
The DELIVER phase is compiled with no milestones and skipped, which is a
legitimate compiled outcome and leaves the drive ending where it should: at the
operator's decision.
-->

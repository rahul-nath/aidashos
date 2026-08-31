# Acceptance design doc

Target project: local-first-agent-os

## Requirements

- Compile one DesignDoc revision into one immutable plan.

## Constraints

- A document may not supply executable code.

## Acceptance criteria

- The seven lifecycle phases occur in their fixed order.

## Non goals

- A general workflow language.

## Milestone A: plan the change

Phase: PLAN
Acceptance: a written implementation plan exists
Artifacts: implementation_plan

## Milestone B: implement the reader

Phase: IMPLEMENT
Depends on: A
Acceptance: the reader lands
Artifacts: source_patch

## Milestone C: implement the writer

Phase: IMPLEMENT
Depends on: A
Acceptance: the writer lands
Artifacts: source_patch

## Milestone D: verify the suite

Phase: VERIFY
Depends on: B, C
Acceptance: the suite passes
Artifacts: test_result

## Milestone E: staff review

Phase: REVIEW
Depends on: D
Executor: review.operator
Approval: required
Acceptance: an operator approved the change
Artifacts: operator_approval

## Milestone F: deliver the artifact

Phase: DELIVER
Depends on: E
Acceptance: the delivery record exists
Artifacts: delivery_record

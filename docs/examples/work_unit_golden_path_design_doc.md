# Golden path design doc

Target project: local_first_agent_os

An advisory WorkUnit that dispatches a plan, waits for operator approval and records delivery through the resident processes.
The plan can use deterministic model responses without claiming that source files changed or that a registered verification gate ran.
The operator supplies approval; the deterministic delivery executor records the resulting durable evidence.

## Requirements

- Drive one WorkUnit from a written document to SUCCEEDED through the resident loops.

## Constraints

- Every milestone's evidence must be something its executor can honestly produce.

## Acceptance criteria

- The plan, the review gate, and the delivery record are each recorded as artifacts.

## Non goals

- Changing any file in a target repository.
- Running registered code-verification commands or qualifying native containment.
- Certifying real model-provider compatibility.

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

## Milestone C: record delivery

Phase: DELIVER
Depends on: B
Executor: deliver.artifact
Acceptance: a delivery record binds this WorkUnit and its compiled plan to the plan and operator approval artifacts
Artifacts: delivery_record

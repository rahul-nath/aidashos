# Recovery postmortem intake

Use this as a provisional note convention, not a validated `TrainingRecord` schema.
Replace the field descriptions with observed facts or an explicit `unknown`, `unavailable` or `not executed` value.

## Identity

- Format: recovery-postmortem-intake.v1
- Incident key: stable root incident or operation identifier
- Capture time: UTC timestamp of this evidence revision
- Capture mode: contemporaneous or retrospective
- Origin: AiDashOS operation, imported host incident, or fixture execution
- Source revision and runtime: exact known code/model/tool identities
- Prior note SHA-256: prior immutable note hash, or none
- Operation/attempt/lease identifiers: observed IDs only; explain absence

## Trigger and evidence

- Observed failure or stall: symptom, recorded failure code and source
- Classification basis: recorded enum, sourced inference, or unknown
- Evidence references: bounded source event/row IDs or immutable sanitized artifacts with hashes
- Capture gaps: missing boundaries, timestamps, receipts or inaccessible sources

## Decision records

Repeat this block for each material diagnostic or repair decision.

- Decision identity and time: recorded ID/time or unavailable
- Evidence available before action: exact observations and source references
- Authority at the time: applicable user authorization and policy/permission scope
- Action selected: diagnostic or remedy, target identity and parameters
- Why selected: concise evidence-based justification, without private chain-of-thought
- Execution receipt: what actually ran, returned identity and outcome
- Later evidence: newly observed facts, kept out of the earlier decision input
- Alternatives: considered actions and comparison evidence, or no justified comparison

## Settlement and learning candidacy

- Requested operation: completed, incomplete or outcome unknown, with evidence
- Root cause: established, suspected or unresolved, with evidence
- Repair verification: verified, failed, inconclusive or not executed
- Verification predicate and result: actual check, scope and observed outcome
- Preserved invariants: duplicate protection, retained state and authority checks relevant to this action
- Applicability conditions: when the demonstrated action is supported
- Contradictions: competing explanations or observed counterexamples
- Demonstration candidacy: supported, needs assessment or insufficient evidence
- Preference candidacy: justified comparison, unresolved comparison or none
- Next action: a specific unresolved diagnostic or none when work is complete

## Privacy and storage

- Sanitization: excluded sensitive fields and remaining access restrictions
- Sealed note SHA-256: record outside the hashed note, in its filename or receipt, to avoid a self-referential hash
- Durable artifact link: existing owner receipt, or private intake only

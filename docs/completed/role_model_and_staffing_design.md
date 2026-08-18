# Role Model & Staffing — Design

Status: **implemented** (2026-07-05, starting at `b28cddd`). The original
proposal follows; the "As built" section records the implementation and the
one configuration-surface delta.

## Thesis: two models, one stack

The Ouroboros vocabulary (`Mind` / `Character` / `FunctionalRole`) and the
contractor-tier idea (junior/senior/staff → gemma/claude/codex, with allocation
counts) are **not alternatives**. They answer different questions, so they
compose as layers:

| Question | Answered by | Example |
|---|---|---|
| What *kind* of work is this — judgment or a mechanical check? | Ouroboros' judgment-vs-deterministic split (`Character` vs `FunctionalRole`) | "review" is judgment; "run the tests" is deterministic |
| What *capability class* does that judgment need? | **Tier** (junior/senior/staff) | a merge verdict needs staff-tier judgment |
| *Which* concrete runtime fills that tier, and how many? | **Bench** (tier → harness+model+capacity) | staff = `codex`, capacity 1 |
| What *cognitive stance* should the judge take? (optional) | Ouroboros' `Mind`, deferred | reviewer summons the `Evaluator` stance |

The single insight that makes them fit: **an engineer persona's seniority *is*
its tier.** `junior_engineer` / `senior_engineer` / `staff_engineer` (Character)
and junior/senior/staff (Tier) are the same axis seen twice. So we unify them
and stop maintaining two vocabularies for one idea.

What each model contributes, kept honest:
- **Ouroboros keeps**: the judgment-vs-deterministic distinction (the valuable
  part — it tells you `test_runner` needs a *shell*, not `claude`), and
  optionally the `Mind` cognitive-stance layer.
- **Tier model adds**: the *executable binding* Ouroboros never had — a tier
  resolves to a real `omni` dispatch target — plus capacity/allocation.
- **They meet at**: Character-seniority ≡ Tier.

## Datatypes (data trumps code)

Designed so illegal states are unrepresentable (Representable/Valid principle).
The load-bearing move is that a stage role is a **sum type** — a role is
*either* judgment *or* a deterministic check, never both, and only judgment
carries a tier:

```python
class Tier(StrEnum):            # the seniority axis (was Ouroboros AgentTier)
    JUNIOR = "junior"          # local, cheap, high-count  (gemma via pi)
    SENIOR = "senior"          # strong implementer        (claude)
    STAFF  = "staff"           # strongest / reviewer / finisher (codex)

class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX  = "codex"
    PI     = "pi"

@dataclass(frozen=True)
class BenchSlot:               # "who's on the bench at this tier"
    harness: Harness
    model: str | None          # None => harness/CLI default (subscription)
    capacity: int              # max concurrent instances (the NUM_* idea)

# The Bench is the ONE place that knows tier -> runtime. Single source of truth.
Bench = dict[Tier, BenchSlot]

# --- a stage role is a SUM TYPE: judgment OR a deterministic check ---
@dataclass(frozen=True)
class JudgmentRole:            # needs a thinking model
    name: str                  # "implementer", "reviewer", "realist"
    tier: Tier
    stance: Mind | None = None # optional Ouroboros cognitive move (deferred)

@dataclass(frozen=True)
class CheckRole:               # deterministic tool-with-a-hat (Ouroboros FunctionalRole)
    name: str                  # "test_runner", "linter", "typecheck"
    command: str               # the shell command that IS the check

StageRole = JudgmentRole | CheckRole   # sum type; py3.13 match/case dispatches

@dataclass(frozen=True)
class Roster:                  # the crew that staffs one stage
    judgment: tuple[JudgmentRole, ...]
    checks: tuple[CheckRole, ...] = ()
    consensus: tuple[JudgmentRole, ...] = ()   # optional review panel (majority vote)
```

Why this shape (tied to the Advanced-Software-Design notes):
- **Sum type / algebraic datatype**: you *cannot* ask a `test_runner` for its
  tier or a `reviewer` for its shell command — the variants don't have those
  fields. The current `role: str` + `"implement" in role.lower()` substring
  hack is exactly the "representable but invalid" state this removes.
- **Keep your secrets**: the executor never learns "codex is staff." It asks
  the `Bench` to resolve a tier and gets a `LaunchSpec`. The tier→runtime map
  is a secret of the staffing module — change codex→something else in one line.
- **Parameter objects**: dispatch takes a resolved `LaunchSpec`, not
  `run(harness, model, prompt, cwd, …)` positional soup.

## The binding chain (how a stage gets staffed at dispatch)

```
Stage ─▶ Roster ─▶ for each JudgmentRole: tier ─▶ Bench[tier] ─▶ LaunchSpec
                                                       (omni run --harness H --model M -p …)
              └─▶ for each CheckRole: run command (the existing verification capture)
              └─▶ if consensus: run the panel, majority vote ─▶ verdict ─▶ CODE_MERGE gate
```

Capacity governs parallelism: up to `Bench[tier].capacity` instances of a tier
run concurrently, each in its own leased worktree — the contractor-agency
allocation, bounded by the claim/lease layer so parallel juniors don't stomp.

## How it maps onto existing code (mostly reuse, little new)

- A launch spec per task already exists — a resolved `JudgmentRole` produces one.
  The `Bench` is the new (small) resolver in front of it.
  (Historical note: this was `OmnigentLaunchSpec`; the Omnigent backend is now
  archived in `potential_directions/omnigent_live_backend/` and the CLI executor
  resolves the bench slot to a harness binary directly.)
- `_is_implementation_task` (the substring hack) is **deleted**; the stage's
  `Roster` names its roles explicitly. No more string matching.
- `CheckRole`s are **the verification commands the executor already runs and
  captures** — we're just naming them, not adding machinery.
- The `consensus` panel is the existing `_consensus_eval` sketch, with the
  hardcoded `[Staff, QA, Realist]` list replaced by the stage's `consensus`
  roster (and its fail-open default fixed to fail-*closed* for a merge gate).
- `pow_wow_agents.role` (already in the schema) is where the *resolved* staffing
  is recorded in the durable Postgres ledger - config stays config, truth stays in Postgres.

## Config surface (stage → roster, as you wanted)

No flat per-role env vars such as a `..._REVIEW_HARNESS` setting. A TOML registry under
`configs/` (matching the repo convention that app-owned registries are TOML),
e.g. `configs/staffing.toml`:

```toml
[bench.junior]
harness = "pi"     ; model = "general"  ; capacity = 4
[bench.senior]
harness = "claude" ; capacity = 3
[bench.staff]
harness = "codex"  ; capacity = 1

[stage.IMPLEMENTATION]
judgment = [ { role = "implementer", tier = "senior" } ]   # claude
checks   = [ "test_runner", "linter" ]

[stage.REVIEW]
judgment  = [ { role = "reviewer", tier = "staff" } ]      # codex checks claude
checks    = [ "test_runner" ]
consensus = [ { role = "reviewer", tier = "staff" },
              { role = "qa",       tier = "senior" },
              { role = "realist",  tier = "senior" } ]      # optional panel
```

This is the "stage → who's rostered" model directly. Swapping which model plays
staff is a one-line bench edit; re-staffing a stage never touches code.

## Keep / unify / defer from Ouroboros

- **KEEP**: judgment-vs-deterministic split → `JudgmentRole` vs `CheckRole`.
- **UNIFY**: `CharacterName` seniority + `AgentTier` → one `Tier`.
- **DEFER (not delete)**: `Mind` cognitive stances → optional `stance` field on
  `JudgmentRole`, unused in v1. This is where the Ouroboros code actually earns
  a `potential_directions/` home: as the reference for the stance layer we add
  later. So Task 2 becomes "extract the *Mind/stance* material as reference,"
  not "delete a bad idea."

## Open decisions in the original proposal

1. **Tier names**: `JUNIOR/SENIOR/STAFF` (your framing) vs keep Ouroboros'
   `WEAK/STRONG/SPECIAL` as an under-the-hood capability class. I lean toward
   JUNIOR/SENIOR/STAFF as the domain enum.
2. **Mapping**: you wrote both "senior=codex/staff=claude" and later
   "claude=senior, codex=staff." Proposal uses **claude=senior, codex=staff,
   gemma=junior**. Confirm.
3. **Capacity semantics**: is `capacity` a hard concurrency cap (scheduler
   blocks past it) or advisory? Proposal: hard cap, enforced via worktree leases.
4. **Consensus scope**: panel on the review stage only, or any stage can declare
   one? Proposal: any stage may, review is the first to use it.
5. **Mind layer**: defer entirely (v2), or wire a single stance now (e.g.
   reviewer = `Evaluator`) to prove the seam?

## As built

- `src/local_first_agent_os/staffing.py` implements `Tier`, `Harness`,
  `BenchSlot`, the `JudgmentRole | CheckRole` sum, `Roster`, default rosters,
  and bench resolution.
- `configs/staffing.toml` is the single tier-to-runtime mapping and includes
  model, reasoning-effort, backup-model, and concurrency settings.
- Pow-wow task specifications carry typed judgment roles; the executor resolves
  those roles through the bench and enforces per-tier concurrency caps while
  dependency-scheduling tasks.
- The implemented configuration surface keeps stage rosters in typed Python
  task specifications/defaults rather than loading `[stage.*]` tables from
  TOML. This preserves the central design invariant: runtime identity belongs to
  the bench, while each workflow declares its roster, without making one global
  roster registry authoritative for every workflow.
- The optional `stance` seam is represented and the default reviewer uses
  `evaluator`; a broader Ouroboros `Mind` runtime remains deliberately deferred.
- Coverage lives in `tests/test_staffing.py` and the executor scheduling tests
  in `tests/test_pow_wow_executor.py`.

#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render the design-document pipeline table from the ledger, and check the roster.

Two different questions were being answered by the same hand-written table, and
only one of them is derivable.

**Derivable, and generated here:** whether a document has been compiled, what its
latest plan hash and validation status are, how many execution blockers it has,
and how many WorkUnits it has produced. Every one of those is a row in
``design_doc_revisions``, ``compiled_plan_revisions``, or ``work_units``.

**Human-judged, and transcribed rather than guessed:** whether the
implementation is *done*. The ledger cannot know that ``session_handoff`` is
nearly complete but unreferenced, or that ``parked_dispatch`` was built in a
different shape than proposed. That judgement is made by a human reading code,
and it lives in exactly one place: each document's own ``Status:`` line, plus
placement in ``docs/completed/``. The status table is generated from those two
signals, so this script assembles the judgement without ever making it. A
status line the closed vocabulary below cannot classify is a check failure
naming the document, never a silent bucket.

So this writes two generated sections and checks both for drift. The check is
the narrow, decidable part: every design document on disk appears in the
status table exactly once, and every name in the table exists on disk. That is
the drift class that actually bit - ``README.md`` and
``docs/gawd_drafts/completed/README.md`` disagreed about which drafts were live,
and nothing told anybody.

Checking is the default and writing takes ``--write``, matching
``dump_config_reference.py``: an accidental check costs a printed diagnosis,
while an accidental write launders real drift into "no changes".
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
_README_PATH = _REPO_ROOT / "README.md"
_DOCS_DIR = _REPO_ROOT / "docs"

_BEGIN = "<!-- BEGIN GENERATED: design-pipeline -->"
_END = "<!-- END GENERATED: design-pipeline -->"
_ROSTER_BEGIN = "<!-- BEGIN GENERATED: design-roster -->"
_ROSTER_END = "<!-- END GENERATED: design-roster -->"

# The status line's classification vocabulary, matched as a case-insensitive
# prefix of the declared value with markdown emphasis stripped. Closed on
# purpose, in the same spirit as the heading vocabulary in
# `work_units/design_doc.py`: a spelling outside the vocabulary is a named
# check failure, never a silent bucket, so a new way of saying "half built"
# gets added here as a deliberate decision.
_PARTIAL_STATUS_PREFIXES: Final = ("partially implemented", "accepted")
_NOT_STARTED_STATUS_PREFIXES: Final = ("draft", "proposed", "proposal", "design, not built")

# The banner form spec documents use, and the plain form prose notes use.
_BANNER_STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(?P<value>[^|\n]+)")
_PLAIN_STATUS_RE = re.compile(r"^Status:\s*(?P<value>.+)$", re.MULTILINE)

# Documents under docs/ that are not design documents. Everything else in the
# directory is one, which is the rule that makes the roster check possible: an
# exception list is auditable, and a naming convention nobody enforces is not.
_NOT_DESIGN_DOCS = frozenset(
    {
        "AGENT_MANUAL.md",
        "code_structure.md",
        "configuration.md",
        "design_principles.md",
        "design_tradeoffs.md",
        "cockpit_e2e_runbook.md",
        "demo_shooting_script.md",
        "first_real_e2e_run_plan.md",
        "kimi_k3_frontend_evaluation_instructions.md",
        "live_evaluation.md",
        "local_observability.md",
        "new_project_intake.md",
        "decomposition_dispatch.md",
        "doctrine_bump_recovery.md",
        "parallel_forks_example.md",
        "project_center.md",
        "public_release_checklist.md",
        "public_snapshot_sync.md",
        "saga_executor_modes.md",
        "work_unit_operator_walkthrough.md",
        "worktree_loss_postmortem.md",
        "frontier_prompts_chrome_live_checks_and_pest_merge.md",
    }
)


class LedgerUnavailable(RuntimeError):
    """The coordination ledger could not be read.

    Raised rather than returning an empty list on purpose. A generated table
    that silently empties when Postgres is down would report every document as
    never compiled, which is a confident lie in exactly the direction that costs
    an operator a re-run.
    """


# Executable specifications exempt from the compile invariant because they
# predate it. This list may only shrink, and the check also fails when an exempt
# document starts compiling, so an exemption cannot outlive its problem.
#
# All four remaining are malformed rather than unwritten: m7 has an "Execution Milestones"
# heading the parser finds nothing under, and the other three carry a numbered
# `## Milestones` list with no PLAN and no VERIFY milestone.
_COMPILE_EXEMPT: Final = frozenset(
    {
        "m7_conversion_grade_template_gawd",
        "privileged_capability_broker_design",
        "tamper_evident_ledger_design",
        "whiteboard_intent_design",
    }
)


def _design_doc_files() -> list[Path]:
    """Executable specifications: the documents that must compile.

    `docs/designs/` is excluded deliberately. That directory holds prose design
    notes, and asking them to declare milestones would be asking a document to
    be something it never intended to be.
    """

    return sorted(
        path
        for path in _DOCS_DIR.glob("*.md")
        if path.name not in _NOT_DESIGN_DOCS and not path.name.startswith("README")
    )


def _design_note_files() -> list[Path]:
    notes = _DOCS_DIR / "designs"
    if not notes.is_dir():
        return []
    return sorted(path for path in notes.glob("*.md") if not path.name.startswith("README"))


def _compile_problems() -> list[str]:
    """Executable specifications in docs/ that do not compile.

    Parsed and compiled in-process rather than through the service, so this
    needs no database. Compilability is a property of the text and the compiler,
    and making the check require Postgres would mean it ran rarely.
    """

    from local_first_agent_os.work_units.compiler import compile_design_doc
    from local_first_agent_os.work_units.design_doc import parse_design_doc

    problems: list[str] = []
    for path in _design_doc_files():
        stem = path.stem
        parsed = parse_design_doc(
            path.read_text(encoding="utf-8"),
            design_doc_id=stem,
            source_path=str(path),
        )
        outcome = compile_design_doc(parsed, design_doc_revision_id="design-status-check")
        valid = getattr(outcome, "validation_status", None) == "VALID"
        if valid and stem in _COMPILE_EXEMPT:
            problems.append(f"{stem} now compiles; remove it from _COMPILE_EXEMPT in this script")
        elif not valid and stem not in _COMPILE_EXEMPT:
            reasons = sorted(
                {
                    str(item.code)
                    for item in getattr(outcome, "diagnostics", ())
                    if getattr(item.severity, "name", "") == "ERROR"
                }
            )
            detail = ", ".join(reasons) or "no milestones the compiler recognises"
            problems.append(
                f"docs/{path.name} is an executable spec that does not compile ({detail}); "
                "fix it, or move it to docs/designs/ if it is a design note"
            )
    return problems


def _completed_doc_files() -> list[Path]:
    completed = _DOCS_DIR / "completed"
    if not completed.is_dir():
        return []
    return sorted(path for path in completed.glob("*.md") if not path.name.startswith("README"))


def _legacy_design_files() -> list[Path]:
    """The pre-split `docs/design/` directory, carried until its two files move."""

    legacy = _DOCS_DIR / "design"
    if not legacy.is_dir():
        return []
    return sorted(path for path in legacy.glob("*.md") if not path.name.startswith("README"))


def _declared_status(path: Path) -> str | None:
    """The document's own Status value, banner form first, else the plain line."""

    text = path.read_text(encoding="utf-8")
    match = _BANNER_STATUS_RE.search(text) or _PLAIN_STATUS_RE.search(text)
    if match is None:
        return None
    return match.group("value").strip()


def _classify_status(value: str) -> str | None:
    normalized = value.replace("*", "").replace("_", "").strip().lower()
    if normalized.startswith(_PARTIAL_STATUS_PREFIXES):
        return "partial"
    if normalized.startswith(_NOT_STARTED_STATUS_PREFIXES):
        return "not_started"
    return None


def _render_roster(problems: list[str]) -> str:
    """The status table, from placement plus each document's own Status line.

    Done is placement: `docs/completed/` is the done shelf, and a status line
    there is not consulted. Partial versus not-started is the document's own
    declaration, classified through the closed vocabulary above. The script
    never judges; it moves a human judgement from a hand-written table nobody
    remembered to update into the one file its author was already editing.
    """

    done = [path.stem for path in _completed_doc_files()]
    partial: list[str] = []
    not_started: list[str] = []
    labelled = [
        *((path, path.stem) for path in _design_doc_files()),
        *((path, path.stem) for path in _design_note_files()),
        *((path, f"design/{path.stem}") for path in _legacy_design_files()),
    ]
    for path, label in labelled:
        value = _declared_status(path)
        if value is None:
            problems.append(
                f"{path.relative_to(_REPO_ROOT)} declares no Status line, so the roster "
                "cannot place it; add one"
            )
            continue
        bucket = _classify_status(value)
        if bucket is None:
            problems.append(
                f"{path.relative_to(_REPO_ROOT)} declares Status {value!r}, which the "
                "roster vocabulary cannot classify; start it with one of: "
                + ", ".join((*_PARTIAL_STATUS_PREFIXES, *_NOT_STARTED_STATUS_PREFIXES))
            )
            continue
        (partial if bucket == "partial" else not_started).append(label)

    def _row(title: str, names: list[str]) -> str:
        rendered = ", ".join(f"`{name}`" for name in names) or "-"
        return f"| {title} | {len(names)} | {rendered} |"

    return "\n".join(
        [
            _ROSTER_BEGIN,
            "",
            "Generated by `scripts/dump_design_status.py`. Do not edit by hand.",
            "",
            "Done is placement in `docs/completed/`. Partial versus not-started is each",
            "document's own `Status:` line, transcribed rather than judged here.",
            "",
            "| Status | Count | Documents |",
            "| --- | --- | --- |",
            _row("**Done** (in `docs/completed/`)", done),
            _row("**Partial** (by own `Status:` line)", partial),
            _row("**Not started**", not_started),
            "",
            _ROSTER_END,
        ]
    )


def _ledger_rows() -> list[dict[str, Any]]:
    try:
        from local_first_agent_os.work_units import service
    except Exception as exc:  # pragma: no cover - import environment problem
        raise LedgerUnavailable(f"could not import the work_units service: {exc}") from exc
    try:
        return list(service.list_design_docs())
    except Exception as exc:
        raise LedgerUnavailable(f"could not read the coordination ledger: {exc}") from exc


def _render(rows: list[dict[str, Any]]) -> str:
    """Render only the documents the ledger has actually seen.

    A document with no revision row is absent from this table rather than
    present with empty cells. "Never compiled" is the overwhelmingly common
    state, so listing every one of them would bury the handful that carry real
    pipeline state under thirty rows of dashes.
    """

    lines = [
        _BEGIN,
        "",
        "Generated by `scripts/dump_design_status.py`. Do not edit by hand.",
        "",
        "Only documents the ledger has seen appear here. A design document absent from",
        "this table has never been compiled, which is the normal state for a document",
        "nobody has run yet.",
        "",
        "| Document | Revs | Latest validation | Blockers | WorkUnits | Latest run |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        validation = row["latest_validation_status"] or "never compiled"
        blockers = row["execution_blocker_count"]
        units = row["work_unit_count"]
        run = row["latest_work_unit_status"] or "-"
        if row["latest_work_unit_phase"] and row["latest_work_unit_status"]:
            run = f"{row['latest_work_unit_status']} ({row['latest_work_unit_phase']})"
        lines.append(
            f"| `{row['design_doc_id']}` | {row['latest_revision_number']} | {validation} "
            f"| {blockers} | {units} | {run} |"
        )
    lines.extend(["", _END])
    return "\n".join(lines)


def _replace_section(text: str, rendered: str, begin: str, end: str) -> str:
    if begin in text and end in text:
        pattern = re.compile(
            re.escape(begin) + ".*?" + re.escape(end),
            re.DOTALL,
        )
        return pattern.sub(lambda _: rendered, text, count=1)
    raise SystemExit(
        f"{_README_PATH} has no generated block. Add these two markers where the "
        f"table should go:\n{begin}\n{end}"
    )


def _names_document(text: str, stem: str) -> bool:
    """Whether the README refers to this document, under any of its usual spellings.

    The table drops a trailing ``_design`` from most entries, so a literal stem
    match reports two thirds of the roster as missing and the check becomes
    noise an operator learns to ignore. Both spellings count, and so does the
    filename, because links are written that way.
    """

    candidates = {stem, f"{stem}.md"}
    for suffix in ("_design", "_gawd", "_design_doc"):
        if stem.endswith(suffix):
            candidates.add(stem[: -len(suffix)])
    return any(candidate in text for candidate in candidates)


def _roster_problems() -> list[str]:
    """Names on disk that the hand-written status table does not mention, and vice versa.

    Only the roster is checked, never the done/partial/not-started judgement,
    because that judgement is not derivable. This catches a document that was
    added, renamed, or moved to ``completed/`` without the table being updated,
    which is every disagreement observed so far.
    """

    text = _README_PATH.read_text(encoding="utf-8")
    problems: list[str] = []

    on_disk = {path.stem for path in _design_doc_files()}
    notes = {path.stem for path in _design_note_files()}
    completed = {path.stem for path in _completed_doc_files()}

    for stem in sorted(on_disk):
        if not _names_document(text, stem):
            problems.append(f"docs/{stem}.md exists and README.md never names it")
    for stem in sorted(notes):
        if not _names_document(text, stem):
            problems.append(f"docs/designs/{stem}.md exists and README.md never names it")
    for stem in sorted(completed):
        if not _names_document(text, stem):
            problems.append(f"docs/completed/{stem}.md exists and README.md never names it")

    both = on_disk & completed
    for stem in sorted(both):
        problems.append(
            f"{stem} is in BOTH docs/ and docs/completed/; one of them is a leftover copy"
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the generated block in README.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the generated block is stale or the roster disagrees",
    )
    args = parser.parse_args()

    problems = _roster_problems() + _compile_problems()
    roster_rendered = _render_roster(problems)

    try:
        rows = _ledger_rows()
    except LedgerUnavailable as exc:
        print(f"design-status: {exc}", file=sys.stderr)
        print(
            "The pipeline table needs Postgres. Start it with "
            "`uv run python scripts/start_postgres_docker.py`.",
            file=sys.stderr,
        )
        if problems:
            print("\nRoster problems found without the ledger:", file=sys.stderr)
            for item in problems:
                print(f"  - {item}", file=sys.stderr)
        return 2

    current = _README_PATH.read_text(encoding="utf-8")
    updated = _replace_section(current, _render(rows), _BEGIN, _END)
    updated = _replace_section(updated, roster_rendered, _ROSTER_BEGIN, _ROSTER_END)

    if args.write:
        if updated != current:
            _README_PATH.write_text(updated, encoding="utf-8")
            print(f"design-status: rewrote the generated block in {_README_PATH}")
        else:
            print("design-status: generated block already current")
        for item in problems:
            print(f"design-status: roster: {item}")
        return 0

    failed = False
    if updated != current:
        print(
            "design-status: the generated block in README.md is stale.\n"
            "Run: uv run python scripts/dump_design_status.py --write",
            file=sys.stderr,
        )
        failed = True
    for item in problems:
        print(f"design-status: roster: {item}", file=sys.stderr)
        failed = True
    if failed:
        return 1
    print("design-status: generated block current, roster consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

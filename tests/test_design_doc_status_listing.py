# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The read path that made design-doc state a query instead of a memory.

Every other entity in the ledger had a lister and design documents had only a
getter, so "what state is this document in" was answered by a hand-maintained
table that had already drifted. These tests pin the two properties that make the
generated replacement trustworthy: the listing sees a document the moment it is
ingested, and it counts every run a document produced rather than only the last.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_unit_support import ACCEPTANCE_DESIGN_DOC

from local_first_agent_os.work_units import service


def test_a_document_appears_once_ingested_and_reports_no_compile_yet(
    runtime: Any,
    work_unit_ledger: Path,
) -> None:
    service.ingest_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="listed-doc")

    rows = {item["design_doc_id"]: item for item in service.list_design_docs()}
    assert "listed-doc" in rows

    row = rows["listed-doc"]
    assert row["revision_count"] == 1
    assert row["latest_revision_number"] == 1
    assert row["work_unit_count"] == 0
    # Ingested is not compiled. The distinction is the whole point of the
    # column: a document can exist in the ledger and have no plan at all.
    assert row["latest_validation_status"] is None
    assert row["latest_plan_hash"] is None


def test_compiling_fills_in_the_plan_columns(
    runtime: Any,
    work_unit_ledger: Path,
) -> None:
    revision = service.ingest_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="compiled-doc")
    service.compile_design_doc_revision(revision.design_doc_revision_id)

    row = next(
        item for item in service.list_design_docs() if item["design_doc_id"] == "compiled-doc"
    )
    assert row["latest_validation_status"] == "VALID"
    assert row["latest_plan_hash"]
    assert row["execution_blocker_count"] == 0


def test_a_second_revision_supersedes_the_first_in_the_listing(
    runtime: Any,
    work_unit_ledger: Path,
) -> None:
    service.ingest_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="revised-doc")
    service.ingest_design_doc(
        ACCEPTANCE_DESIGN_DOC + "\n\nAn added paragraph makes a new revision.\n",
        design_doc_id="revised-doc",
    )

    row = next(
        item for item in service.list_design_docs() if item["design_doc_id"] == "revised-doc"
    )
    assert row["revision_count"] == 2
    assert row["latest_revision_number"] == 2


def test_list_work_units_carries_the_provenance_back_to_its_document(
    runtime: Any,
    work_unit_ledger: Path,
) -> None:
    """Without this the work-unit list is a dead end.

    A reader could see that runs exist and had no way back to the documents that
    produced them, which is the specific reason design-doc state was tracked by
    hand somewhere else.
    """

    revision = service.ingest_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="provenance-doc")
    compiled = service.compile_design_doc_revision(revision.design_doc_revision_id)
    # A compile that produced no plan revision cannot be started, and asserting
    # that here keeps the failure at the compile rather than inside the start.
    assert compiled.compiled_plan_revision_id is not None
    service.start_work_unit(
        compiled.compiled_plan_revision_id,
        title="Provenance run",
        approved_plan_hash=compiled.plan_hash,
    )

    units = service.list_work_units()
    assert units, "starting a work unit should make it listable"
    unit = next(item for item in units if item["title"] == "Provenance run")
    assert unit["design_doc_revision_id"] == revision.design_doc_revision_id
    assert unit["compiled_plan_revision_id"] == compiled.compiled_plan_revision_id

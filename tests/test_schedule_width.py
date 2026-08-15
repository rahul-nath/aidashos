# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Demand-derived width: what the document asks for, bounded by what it may have.

The invariant these exist to hold is the one `AuthorityPolicy` states in its own
docstring: it is "the bounds the document could not widen even by asking". A
document declaring a pace is asking. The ceiling answers.
"""

from __future__ import annotations

from local_first_agent_os.work_units.design_doc import (
    DiagnosticSeverity,
    PhaseInference,
    apply_phase_inference,
    parse_design_doc,
)
from local_first_agent_os.work_units.lifecycle import LifecyclePhase
from local_first_agent_os.work_units.plan import (
    DELIVERY_PACE_WIDTH_REQUEST,
    AuthorityPolicy,
    CompiledWorkPlan,
    DeliveryPace,
)
from local_first_agent_os.work_units.scheduling import (
    WidthConstraint,
    resolve_schedule_width,
)

READY_EIGHT = tuple(f"m{index}" for index in range(8))


def _plan(pace: DeliveryPace, ceiling: int) -> CompiledWorkPlan:
    """A plan stub carrying only what width resolution reads."""

    class _Stub:
        authority_policy = AuthorityPolicy(
            max_parallel_milestones=ceiling,
            operator_approval_inferrable=False,
            document_may_define_phases=False,
            document_may_supply_code=False,
        )
        declared_delivery_pace = pace

    return _Stub()  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# The ceiling is a ceiling
# --------------------------------------------------------------------------- #


def test_a_pace_cannot_widen_past_the_authority_ceiling() -> None:
    """The whole point of AuthorityPolicy, asserted rather than assumed."""

    width = resolve_schedule_width(_plan(DeliveryPace.COMPRESSED, ceiling=2), READY_EIGHT)

    assert width.effective == 2
    assert width.binding_constraint is WidthConstraint.AUTHORITY_CEILING


def test_a_pace_narrower_than_the_ceiling_is_honoured() -> None:
    width = resolve_schedule_width(_plan(DeliveryPace.DELIBERATE, ceiling=4), READY_EIGHT)

    assert width.effective == 1
    assert width.binding_constraint is WidthConstraint.DECLARED_PACE


def test_the_graph_binds_when_it_is_narrower_than_either() -> None:
    """Two ready leaves and a ceiling of four is not a ceiling problem."""

    width = resolve_schedule_width(_plan(DeliveryPace.COMPRESSED, ceiling=4), ("a", "b"))

    assert width.effective == 2
    assert width.binding_constraint is WidthConstraint.READY_SET


def test_an_exhausted_ready_set_resolves_to_zero() -> None:
    """Zero is the caller's signal to stop, not an argument for bounded_batch."""

    width = resolve_schedule_width(_plan(DeliveryPace.STEADY, ceiling=4), ())

    assert width.effective == 0
    assert width.binding_constraint is WidthConstraint.READY_SET


# --------------------------------------------------------------------------- #
# Silence must schedule exactly as it did before the field existed
# --------------------------------------------------------------------------- #


def test_an_unspecified_pace_runs_at_the_ceiling() -> None:
    """The pre-existing behaviour: limit = max_parallel_milestones."""

    width = resolve_schedule_width(_plan(DeliveryPace.UNSPECIFIED, ceiling=4), READY_EIGHT)

    assert width.effective == 4


def test_every_pace_is_reconciled_rather_than_trusted() -> None:
    """A new pace added without a width request would KeyError at schedule time.

    Asserting total coverage here turns that into a failure at the point the
    enum is edited, which is where someone can still see why the mapping exists.
    """

    assert set(DELIVERY_PACE_WIDTH_REQUEST) == set(DeliveryPace)


# --------------------------------------------------------------------------- #
# The document side
# --------------------------------------------------------------------------- #

_DOC = """# Widen this

Target project: local_first_agent_os
{pace_line}

## Requirements

- something
"""


def test_a_preamble_pace_line_is_parsed() -> None:
    parsed = parse_design_doc(_DOC.format(pace_line="Pace: compressed"), design_doc_id="d1")

    assert parsed.declared_delivery_pace is DeliveryPace.COMPRESSED


def test_a_silent_document_is_unspecified() -> None:
    parsed = parse_design_doc(_DOC.format(pace_line=""), design_doc_id="d1")

    assert parsed.declared_delivery_pace is DeliveryPace.UNSPECIFIED


def test_an_unknown_pace_warns_and_falls_back_rather_than_failing_the_document() -> None:
    parsed = parse_design_doc(_DOC.format(pace_line="Pace: yesterday"), design_doc_id="d1")

    assert parsed.declared_delivery_pace is DeliveryPace.UNSPECIFIED
    # Not `not parsed.has_errors`: this stub declares no milestones, so the
    # document has an unrelated error and that assertion would pass for the
    # wrong reason the day the pace did start failing compilation.
    pace_diagnostics = [item for item in parsed.diagnostics if "pace" in item.code]
    assert [item.code for item in pace_diagnostics] == ["unknown_delivery_pace"]
    assert pace_diagnostics[0].severity is DiagnosticSeverity.WARNING


def test_timeline_is_accepted_as_a_spelling_of_pace() -> None:
    parsed = parse_design_doc(_DOC.format(pace_line="Timeline: deliberate"), design_doc_id="d1")

    assert parsed.declared_delivery_pace is DeliveryPace.DELIBERATE


def test_phase_inference_does_not_discard_the_declared_pace() -> None:
    """The two features meet on the document that needs both.

    `--classify-phases` exists for milestones written as prose, which is what the
    GAWD template produces, so a document declaring a pace and needing inference
    is the ordinary case rather than a corner. `apply_phase_inference` rebuilds
    the parsed document field by field, and the pace field carries a default, so
    omitting it reset the pace to `UNSPECIFIED` silently: the plan then ran at
    the authority ceiling the author had explicitly asked it not to use.
    """

    document = (
        "# Paced work\n"
        "Pace: steady\n"
        "\n## Requirements\n\n- Ship it.\n"
        "\n## 8. Execution Milestones\n\n1. Write the reader\n"
    )

    parsed = parse_design_doc(document, design_doc_id="doc")
    assert parsed.declared_delivery_pace is DeliveryPace.STEADY
    candidate = parsed.milestone_candidates[0]
    assert candidate.declared_phase is None

    inferred = apply_phase_inference(
        parsed,
        [
            PhaseInference(
                milestone_key=candidate.declared_key,
                phase=LifecyclePhase.IMPLEMENT,
                confidence=1.0,
                reasoning="an operator confirmed this classification",
            )
        ],
    )

    assert inferred.declared_delivery_pace is DeliveryPace.STEADY

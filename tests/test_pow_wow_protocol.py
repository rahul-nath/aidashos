# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from local_first_agent_os.pow_wow.protocol import (
    ReviewDisposition,
    ReviewFindingSeverity,
    ReviewVerdict,
    TaskPurpose,
    classify_finding_severity,
    infer_legacy_task_purpose,
)
from local_first_agent_os.pow_wow.views import (
    TruncatedViewBlock,
    VerbatimViewBlock,
    build_bounded_view_block,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("APPROVE\nLooks good.", ReviewDisposition.APPROVE),
        ("BLOCK: verification is missing", ReviewDisposition.REQUEST_CHANGES),
        ("REQUEST_CHANGES add a regression test", ReviewDisposition.REQUEST_CHANGES),
        ("REJECT unsafe behavior", ReviewDisposition.REJECT),
        ("ESCALATE architectural disagreement", ReviewDisposition.ESCALATE),
        ("Looks reasonable.", ReviewDisposition.UNCLASSIFIED),
        ("", ReviewDisposition.UNCLASSIFIED),
    ],
)
def test_review_verdict_parses_first_line_into_enum(
    text: str,
    expected: ReviewDisposition,
) -> None:
    assert ReviewVerdict.parse(text).disposition is expected


def test_review_verdict_does_not_sniff_later_prose() -> None:
    verdict = ReviewVerdict.parse("APPROVE\nThis previously blocked the workflow.")
    assert verdict.disposition is ReviewDisposition.APPROVE
    assert not verdict.disposition.requests_changes


def test_a_labeled_verdict_line_counts_wherever_it_sits() -> None:
    """The 2026-08-10 dispatches, verbatim in shape.

    Both frontier reviewers opened with a markdown heading and put the verdict
    two lines down; first-line-only parsing read the title, classified both
    reviews UNCLASSIFIED, and failed two substantively approved dispatches
    closed. A line that names itself the verdict is the reviewer keeping the
    contract in a dialect, and the parser's job is the meaning.
    """

    approved = ReviewVerdict.parse(
        "## Staff review: `dispatch_0bcebb9c` Milestone 1 (agent ACL scoping)\n"
        "\n"
        "**Verdict: APPROVE.** The three acceptance criteria are satisfied in "
        "the leased worktree.\n"
    )
    assert approved.disposition is ReviewDisposition.APPROVE
    assert approved.decision_line is not None
    assert approved.decision_line.startswith("**Verdict")

    blocked = ReviewVerdict.parse(
        "Ledger reads are not permitted in this session either, and that is fine.\n"
        "\n"
        "## Verdict: BLOCK\n"
        "\n"
        "### Primary reason: there is nothing to review\n"
    )
    assert blocked.disposition is ReviewDisposition.REQUEST_CHANGES


def test_unlabeled_prose_mentioning_a_verdict_word_stays_unclassified() -> None:
    """The guard the label exists for: mentioning approval is not approving."""

    verdict = ReviewVerdict.parse(
        "## Review notes\n"
        "\n"
        "The senior asked me to approve quickly, which I decline to do without "
        "reading the diff.\n"
        "More reading follows.\n"
    )
    assert verdict.disposition is ReviewDisposition.UNCLASSIFIED


def test_the_first_line_contract_still_beats_a_later_labeled_line() -> None:
    verdict = ReviewVerdict.parse("APPROVE\n\nVerdict: BLOCK (a quote of the last round)\n")
    assert verdict.disposition is ReviewDisposition.APPROVE


def test_review_finding_severity_is_finite_and_fail_closed() -> None:
    assert (
        classify_finding_severity(ReviewDisposition.APPROVE) is ReviewFindingSeverity.NON_BLOCKING
    )
    assert (
        classify_finding_severity(ReviewDisposition.REQUEST_CHANGES)
        is ReviewFindingSeverity.BLOCKING
    )
    assert (
        classify_finding_severity(ReviewDisposition.UNCLASSIFIED) is ReviewFindingSeverity.UNKNOWN
    )


def test_legacy_task_purpose_is_parsed_at_the_boundary() -> None:
    assert (
        infer_legacy_task_purpose(
            task_name="staff_review",
            role="agent",
            judgment_name=None,
            dispatch_kind="advisory",
        )
        is TaskPurpose.REVIEW
    )
    assert (
        infer_legacy_task_purpose(
            task_name="build",
            role="agent",
            judgment_name=None,
            dispatch_kind="code",
        )
        is TaskPurpose.IMPLEMENTATION
    )


def test_build_bounded_view_block_makes_truncation_explicit() -> None:
    short = build_bounded_view_block(source="artifact:a1", content="abc", char_limit=3)
    long = build_bounded_view_block(source="artifact:a2", content="abcdef", char_limit=3)

    assert isinstance(short, VerbatimViewBlock)
    assert short.render() == "abc"
    assert isinstance(long, TruncatedViewBlock)
    assert long.render() == (
        "[truncated view from artifact:a2; original_chars=6; omitted_chars=3]\nabc"
    )

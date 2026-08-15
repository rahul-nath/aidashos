# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The audit artifact's one property is falsifiability.

Anchoring beats summarizing because a summary's staleness is undetectable and
an anchor's is a diff. These tests pin the partition semantics that make the
artifact safe to hand to a successor: nothing unanchored ever arrives as
verified, and nothing a diff touched does either.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.pow_wow.repo_audit import (
    REPO_AUDIT_SCHEMA_VERSION,
    AuditClaim,
    RepoAudit,
    RepoAuditError,
    extract_repo_audit,
    render_audit_context_block,
)

_SHA = "28795c9aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _audit() -> RepoAudit:
    return RepoAudit(
        target_project_id="local_first_agent_os",
        commit_sha=_SHA,
        claims=(
            AuditClaim(
                claim="AgentCaller requires a pow-wow scope",
                file="src/local_first_agent_os/capability_gate.py",
                line_start=65,
            ),
            AuditClaim(
                claim="the spawn path forwards the scope",
                file="src/local_first_agent_os/pow_wow/executor.py",
                line_start=536,
                line_end=550,
                assumption_files=("src/local_first_agent_os/capability_gate.py",),
            ),
            AuditClaim(claim="the suite takes about three minutes"),
        ),
    )


def test_partition_demotes_exactly_what_the_diff_or_missing_anchor_condemns() -> None:
    partition = _audit().partition(["src/local_first_agent_os/capability_gate.py"])

    assert [claim.claim for claim in partition.verified] == []
    # The first claim's own anchor changed; the second's assumption file did;
    # the third was never anchored. All three demote, each for its own reason.
    assert len(partition.demoted) == 3


def test_untouched_anchored_claims_survive_and_unanchored_never_do() -> None:
    partition = _audit().partition([])

    assert [claim.claim for claim in partition.verified] == [
        "AgentCaller requires a pow-wow scope",
        "the spawn path forwards the scope",
    ]
    assert [claim.claim for claim in partition.demoted] == ["the suite takes about three minutes"]


def test_payload_round_trip_preserves_every_anchor() -> None:
    audit = _audit()
    rebuilt = RepoAudit.from_payload(audit.to_payload())
    assert rebuilt == audit
    assert audit.to_payload()["schema_version"] == REPO_AUDIT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": "repo_audit.v2"},
        {"commit_sha": "not-a-sha"},
        {"claims": []},
        {"target_project_id": ""},
    ],
)
def test_a_payload_that_lies_about_being_an_audit_raises(mutation: dict[str, object]) -> None:
    payload = _audit().to_payload()
    payload.update(mutation)
    with pytest.raises(RepoAuditError):
        RepoAudit.from_payload(payload)


def test_extract_takes_identity_from_the_host_not_the_agent() -> None:
    """The worktree's HEAD is the host's fact; a self-reported sha is a claim."""

    output = (
        "I read the repository carefully. Findings follow.\n"
        "```repo_audit.v1\n"
        '{"commit_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",\n'
        ' "target_project_id": "something-else",\n'
        ' "claims": [{"claim": "the gate strips control-plane variables",\n'
        '             "file": "src/local_first_agent_os/pow_wow/executor.py"}]}\n'
        "```\n"
        "That is all.\n"
    )
    audit = extract_repo_audit(output, target_project_id="local_first_agent_os", commit_sha=_SHA)

    assert audit is not None
    assert audit.commit_sha == _SHA
    assert audit.target_project_id == "local_first_agent_os"
    assert audit.claims[0].file == "src/local_first_agent_os/pow_wow/executor.py"


def test_extract_returns_none_without_a_block_and_raises_on_a_broken_one() -> None:
    assert (
        extract_repo_audit("pure prose, no block", target_project_id="p", commit_sha=_SHA) is None
    )
    with pytest.raises(RepoAuditError):
        extract_repo_audit(
            "```repo_audit.v1\n{not json}\n```",
            target_project_id="p",
            commit_sha=_SHA,
        )


def test_render_states_both_shas_and_separates_pointers_from_hypotheses() -> None:
    block = render_audit_context_block(
        _audit(),
        head_sha="64b42ccbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        changed_files=["src/local_first_agent_os/capability_gate.py"],
    )

    assert _SHA in block
    assert "64b42ccbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in block
    assert "Hypotheses" in block
    assert "re-verify before relying" in block
    # Every claim demoted here, so no pointer section may appear: an empty
    # "verified" heading would read as an audit vouching for nothing.
    assert "Verified pointers" not in block

    clean = render_audit_context_block(
        _audit(),
        head_sha="64b42ccbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        changed_files=[],
    )
    assert "Verified pointers" in clean
    assert "capability_gate.py:65" in clean
    assert "executor.py:536-550" in clean
    assert "assumes src/local_first_agent_os/capability_gate.py" in clean

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The operator's written statement of who may do what.

`POLICIES.md` is the authority and `policy_document.py` is how the code reads it.
One decision variable per test: the verb on the line, whether the principal has a
section, whether the name is real, which of the two vocabularies it is written
in, and whether the document outranks the ledger.

The last of those is the one that makes writing a policy worth the trouble. A
`Never:` line that a runtime grant could lift would make the document advisory,
and an advisory policy is a comment.

The vocabulary variable is the one with a trap in it. An operator action can
expand to several capabilities, so two lines can contradict each other without
sharing a word, and the error has to name what the operator typed rather than
what it expanded to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.capabilities import Capability, parse_capability
from local_first_agent_os.capability_gate import (
    CapabilityDenied,
    check_capability,
    policy_principal,
)
from local_first_agent_os.policy_document import (
    POLICY_CONTENT_HASH,
    CompiledPolicy,
    PolicyDocumentError,
    load_policy_document,
    parse_policy_document,
    policy_document_content_hash,
    policy_document_path,
)
from local_first_agent_os.settings import get_settings
from local_first_agent_os.staffing import load_bench
from local_first_agent_os.vocabulary import DispatchTier
from local_first_agent_os.work_units.permissions import (
    PermissionAction,
    capabilities_for_actions,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POW_WOW_ID = "pow-wow-written-policy-test"

scenarios("features/written_policy.feature")


def _document(body: str) -> CompiledPolicy:
    return parse_policy_document(body)


# Variable 1: the verb on the line.
def test_a_may_line_is_an_allowlist() -> None:
    policy = _document("## Principal: claude\nMay: read_repository, invoke_model\n")

    assert policy.permits("claude", Capability.READ_REPOSITORY)
    assert not policy.permits("claude", Capability.WRITE_REPOSITORY)


def test_a_never_line_is_a_denial() -> None:
    policy = _document("## Principal: claude\nNever: publish_deployment\n")

    assert not policy.permits("claude", Capability.PUBLISH_DEPLOYMENT)
    assert policy.permits("claude", Capability.WRITE_REPOSITORY)


def test_a_capability_on_both_lines_is_a_compile_error() -> None:
    """The document refuses to resolve a contradiction it was not asked to resolve.

    Silently picking the denial would be safe at runtime and still wrong: the
    operator would be left with a `May:` line that does nothing and no sign that
    it does nothing. Refusing to load is the answer that reaches a person.
    """

    with pytest.raises(PolicyDocumentError, match="write_repository"):
        _document("## Principal: claude\nMay: write_repository\nNever: write_repository\n")


# Variable 2: whether the principal has a section.
def test_an_unlisted_principal_falls_back_to_the_default_section() -> None:
    policy = _document(
        "## Principal: default\nNever: publish_deployment\n"
        "## Principal: claude\nMay: read_repository\n"
    )

    assert not policy.permits("some-new-agent", Capability.PUBLISH_DEPLOYMENT)
    assert policy.permits("some-new-agent", Capability.WRITE_REPOSITORY)


def test_a_document_that_says_nothing_permits_everything() -> None:
    """This is a further restriction on the compiled plan, not a replacement.

    A machine with no `POLICIES.md` must behave exactly as it did before the file
    existed, or adding the feature would be a silent lockout.
    """

    policy = _document("# Agent privileges\n\nNo principals here.\n")

    assert policy.permits("claude", Capability.WRITE_REPOSITORY)


def test_a_principal_section_outranks_the_default() -> None:
    policy = _document(
        "## Principal: default\nNever: run_command\n## Principal: claude\nMay: run_command\n"
    )

    assert policy.permits("claude", Capability.RUN_COMMAND)
    assert not policy.permits("codex", Capability.RUN_COMMAND)


# Variable 3: whether the document can be read at all.
def test_a_name_no_capability_answers_to_is_a_compile_error() -> None:
    """A permission line that quietly does nothing reads as protection."""

    with pytest.raises(PolicyDocumentError, match="become_root"):
        _document("## Principal: claude\nMay: read_repository, become_root\n")


def test_a_principal_declared_twice_is_a_compile_error() -> None:
    """One section per principal, so there is one place to look."""

    with pytest.raises(PolicyDocumentError, match="twice"):
        _document(
            "## Principal: claude\nMay: read_repository\n"
            "## Principal: claude\nNever: read_repository\n"
        )


def test_an_empty_rule_line_is_a_compile_error() -> None:
    with pytest.raises(PolicyDocumentError, match="empty"):
        _document("## Principal: claude\nMay:   \n")


# Variable 4: which vocabulary the line is written in.
def test_an_operator_action_means_what_it_means_in_a_permission_envelope() -> None:
    """The whole point of accepting the authoring vocabulary.

    `code_worktree_write` is the word an operator approves in an intake document.
    If it meant something else here, the file would be a second, drifting
    definition of the same decision.
    """

    policy = _document("## Principal: claude\nMay: code_worktree_write\n")

    assert policy.permits("claude", Capability.WRITE_REPOSITORY)
    assert not policy.permits("claude", Capability.RUN_COMMAND)


def test_one_action_can_deny_several_capabilities() -> None:
    """An install is a command and network egress, so denying it denies both."""

    policy = _document("## Principal: claude\nNever: dependency_install\n")

    assert not policy.permits("claude", Capability.RUN_COMMAND)
    assert not policy.permits("claude", Capability.NETWORK_ACCESS)


def test_the_two_vocabularies_mix_on_one_line() -> None:
    """Nobody should have to look up which register a name belongs to."""

    policy = _document("## Principal: claude\nMay: code_worktree_write, read_repository\n")

    assert policy.permits("claude", Capability.WRITE_REPOSITORY)
    assert policy.permits("claude", Capability.READ_REPOSITORY)


def test_an_action_that_governs_no_capability_is_a_compile_error() -> None:
    """`prepare_isolated_worktrees` is real authoring vocabulary and enforces nothing.

    Writing it here would look like protection and be none, which is the same
    failure as an unknown name and gets the same answer.
    """

    with pytest.raises(PolicyDocumentError, match="prepare_isolated_worktrees"):
        _document("## Principal: claude\nNever: prepare_isolated_worktrees\n")


def test_an_expansion_conflict_names_the_words_the_operator_wrote() -> None:
    """`run_command` is on both lines, but neither line says `run_command`.

    Reporting the capability alone would send the operator looking for a word
    that is not in the file. This is a real choice - allow the test command and
    deny the install, or the reverse - so the document refuses to guess.
    """

    with pytest.raises(PolicyDocumentError) as caught:
        _document("## Principal: claude\nMay: test_command_execution\nNever: dependency_install\n")

    message = str(caught.value)
    assert "test_command_execution" in message
    assert "dependency_install" in message
    assert "run_command" in message


def test_a_never_line_still_denies_when_there_is_no_allowlist() -> None:
    """Why `denied` is a separate set rather than an absence from `allowed`.

    The two sets are disjoint by construction, so their precedence is not what
    makes this work. A section with only a `Never:` line has an empty allowlist,
    which means defer-to-the-plan, and without the denial set that section would
    permit exactly what it was written to forbid.
    """

    policy = _document("## Principal: claude\nNever: run_command\n")

    assert not policy.permits("claude", Capability.RUN_COMMAND)
    assert policy.permits("claude", Capability.WRITE_REPOSITORY)


# Variable 5: whether the document outranks the ledger.
def test_the_written_policy_outranks_a_runtime_grant(work_unit_ledger: Path) -> None:
    """The property that makes writing a policy worth the trouble.

    `POLICIES.md` denies `publish_deployment` to every principal through its
    default section. No grant, and no absence of a revocation, can lift that.
    """

    verdict = check_capability(
        agent_name="claude",
        agent_role="implementer",
        capability=Capability.PUBLISH_DEPLOYMENT,
        pow_wow_id=_POW_WOW_ID,
    )

    assert isinstance(verdict, CapabilityDenied)
    assert "POLICIES.md" in verdict.reason


def test_the_shipped_document_denies_deployment_to_everyone(
    work_unit_ledger: Path,
) -> None:
    """Deploying is an operator action.

    The repository's own doctrine is that nothing auto-merges, deploys,
    purchases, or sends external communication without the approval gate, and an
    agent role that could carry it would be that gate in the wrong place.
    """

    policy = load_policy_document()

    for principal in ("claude", "codex", "pi", "some-agent-invented-later"):
        assert not policy.permits(principal, Capability.PUBLISH_DEPLOYMENT), principal


def test_the_shipped_document_denies_every_operator_gated_action_to_everyone() -> None:
    """The six actions the approval gate exists for.

    Each is denied by the default section, so an agent invented tomorrow with no
    section of its own inherits the denial rather than the absence of one. This
    is the list to change when the gate's meaning changes, and the test that
    notices when somebody changes it.
    """

    policy = load_policy_document()
    gated = capabilities_for_actions(
        (
            PermissionAction.DEPLOY,
            PermissionAction.MERGE_TO_MAIN,
            PermissionAction.SPEND_MONEY,
            PermissionAction.EXTERNAL_COMMUNICATIONS,
            PermissionAction.SECRET_OR_CREDENTIAL_ACCESS,
            PermissionAction.DESTRUCTIVE_FILE_OPERATIONS,
        )
    )

    for principal in ("claude", "codex", "pi", "some-agent-invented-later"):
        for capability in gated:
            assert not policy.permits(principal, capability), f"{principal}/{capability.value}"


def test_the_shipped_document_keeps_a_reviewer_read_only() -> None:
    """The read-only sandbox says this at the process level.

    Saying it here too is not duplication: one is what the operator decided and
    the other is how it is enforced, and a reader should be able to find the
    decision without reading the enforcement.
    """

    policy = load_policy_document()
    staffing_bench = load_bench(_REPO_ROOT / "configs" / "staffing.toml")
    reviewer = staffing_bench[DispatchTier.STAFF].harness.value
    principal = policy_principal(reviewer, DispatchTier.STAFF.value, policy)

    assert not policy.permits(principal, Capability.WRITE_REPOSITORY)
    assert not policy.permits(principal, Capability.RUN_COMMAND)
    assert policy.permits(principal, Capability.READ_REPOSITORY)


# Variable 5: the tripwire.
def test_the_pinned_policy_hash_matches_the_document_on_disk() -> None:
    """An edit to who-may-do-what cannot pass unnoticed.

    A tripwire rather than a lock. There is no portable way to make one file on
    this machine writable by one person and not another, so this does not pretend
    to; it refuses to let a change through silently, which is the same bargain
    `SCHEMA_CONTENT_HASH` makes and the strongest honest guarantee available
    locally.

    Two answers are correct when this fires, and the point is that somebody
    chooses: the edit was intended, so update the pin in the same commit; or it
    was not, so revert the file.
    """

    assert policy_document_content_hash() == POLICY_CONTENT_HASH, (
        "POLICIES.md changed. If that was intended, update POLICY_CONTENT_HASH "
        "in the same commit; if it was not, revert the file."
    )


def test_the_document_is_not_relocatable_by_configuration() -> None:
    """A policy you can point elsewhere with an environment variable is not one.

    `config_dir` is exactly that variable, and the golden-path test repoints it
    at a temp directory without meaning to change anybody's permissions.
    """

    path = policy_document_path()

    assert path.name == "POLICIES.md"
    assert path.parent.name == "local_first_agent_os" or (path.parent / "pyproject.toml").exists()


# --- Scenario steps for `features/written_policy.feature` ------------------


class _Board:
    """What one scenario has written down so far.

    A compiled policy or the complaint that stopped it from compiling, never
    both. The scenarios that expect a refusal read `complaint`; the ones that
    expect an answer read `policy`, and get an attribute error rather than a
    confusing assertion if the document they wrote did not compile.
    """

    def __init__(self) -> None:
        self.policy: CompiledPolicy | None = None
        self.complaint: str | None = None
        self.verdict: CapabilityDenied | object | None = None

    def compile(self, body: str) -> None:
        try:
            self.policy = parse_policy_document(body)
        except PolicyDocumentError as exc:
            self.complaint = str(exc)


@pytest.fixture()
def board() -> _Board:
    return _Board()


@given(parsers.parse('a policy that denies "{written}" to claude'))
def _deny_one(board: _Board, written: str) -> None:
    board.compile(f"## Principal: claude\nNever: {written}\n")


@given(parsers.parse('a policy that allows "{allowed}" and denies "{denied}" to claude'))
def _allow_and_deny(board: _Board, allowed: str, denied: str) -> None:
    board.compile(f"## Principal: claude\nMay: {allowed}\nNever: {denied}\n")


@given("nothing has been revoked for this pow-wow")
def _no_revocations(work_unit_ledger: Path) -> None:
    """The ledger is empty, so only the written document can refuse."""


@when(parsers.parse('the policy is asked whether claude may "{capability}"'))
def _ask_claude(board: _Board, capability: str) -> None:
    assert board.policy is not None, f"the document did not compile: {board.complaint}"
    board.verdict = board.policy.permits("claude", parse_capability(capability))


@when(parsers.parse('the policy is asked whether "{principal}" may "{capability}"'))
def _ask_principal(board: _Board, principal: str, capability: str) -> None:
    board.policy = load_policy_document()
    board.verdict = board.policy.permits(principal, parse_capability(capability))


@when(parsers.parse('the gate is asked whether claude may "{capability}"'))
def _ask_gate(board: _Board, capability: str) -> None:
    board.verdict = check_capability(
        agent_name="claude",
        agent_role="implementer",
        capability=parse_capability(capability),
        pow_wow_id=_POW_WOW_ID,
    )


@then(parsers.parse('the policy says "{verdict}"'))
def _policy_says(board: _Board, verdict: str) -> None:
    assert board.verdict is (verdict == "yes")


@then(parsers.parse('the gate says "{verdict}"'))
def _gate_says(board: _Board, verdict: str) -> None:
    assert isinstance(board.verdict, CapabilityDenied) is (verdict == "denied")


@then(parsers.parse('the denial names "{fragment}"'))
def _denial_names(board: _Board, fragment: str) -> None:
    assert isinstance(board.verdict, CapabilityDenied)
    assert fragment in board.verdict.reason


@then("the document refuses to compile")
def _refused(board: _Board) -> None:
    assert board.complaint is not None, "the document compiled when it should not have"


@then(parsers.parse('the complaint names "{fragment}"'))
def _complaint_names(board: _Board, fragment: str) -> None:
    assert board.complaint is not None
    assert fragment in board.complaint


def test_the_written_policy_follows_the_bench_seating() -> None:
    """A grant belongs to the seat, and now says so directly.

    The document used to be keyed on the vendor, so it described a seat only by
    coincidence and the coincidence was maintained by hand. Swapping the bench on
    2026-08-09 silently inverted both halves: the implementer lost
    `code_worktree_write` and every code task began failing the ACL, while the
    reviewer gained write and execute against the tree it was reviewing. Neither
    showed up as a policy error, because the document stayed internally
    consistent - about the wrong seating. It happened again on 2026-08-11 and
    blocked a live milestone, which is what moved the key to the seat.

    Now the same assertions run through `policy_principal`, which is what the
    gate itself calls, so this covers the resolution rather than only the
    document. A staffing swap needs no edit here at all; what would fail is a
    seat losing the grant its work requires.
    """

    bench = load_bench(_REPO_ROOT / "configs" / "staffing.toml")
    implementer = bench[DispatchTier.SENIOR].harness.value
    reviewer = bench[DispatchTier.STAFF].harness.value
    policy = load_policy_document()
    implements = policy_principal(implementer, DispatchTier.SENIOR.value, policy)
    reviews = policy_principal(reviewer, DispatchTier.STAFF.value, policy)

    assert policy.permits(implements, Capability.WRITE_REPOSITORY), (
        f"the implementer resolves to principal {implements!r} and POLICIES.md does "
        "not permit it to write; every code task fails the capability gate"
    )
    assert policy.permits(implements, Capability.RUN_COMMAND), (
        f"{implements!r} implements but may not run commands, so nothing it writes is verified"
    )
    assert not policy.permits(reviews, Capability.WRITE_REPOSITORY), (
        f"the reviewer resolves to principal {reviews!r} and POLICIES.md permits it "
        "to write; a reviewer that may mutate what it reviews is the property the read-only "
        "sandbox exists to hold"
    )
    assert not policy.permits(reviews, Capability.RUN_COMMAND)


def test_one_vendor_holding_both_seats_is_still_two_principals() -> None:
    """An outage staffing is the case the vendor key could not express.

    When a quota outage seats one vendor as both implementer and reviewer, a
    harness-keyed lookup has to answer with one section for two seats, and
    whichever it picks is wrong for the other. The role decides it, so the same
    vendor writes as the implementer and stays read-only as the reviewer.
    """

    policy = load_policy_document()

    implements = policy_principal("claude", DispatchTier.SENIOR.value, policy)
    reviews = policy_principal("claude", DispatchTier.STAFF.value, policy)

    assert (implements, reviews) == (DispatchTier.SENIOR.value, DispatchTier.STAFF.value)
    assert policy.permits(implements, Capability.WRITE_REPOSITORY)
    assert not policy.permits(reviews, Capability.WRITE_REPOSITORY)


def _pin_cross_vendor_seating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The static file the gate reads, pinned to the cross-vendor pairing.

    The 2026-08-29 premise: a fallback seating has claude implementing while
    the file on disk still declares claude as the reviewer. Written here rather
    than read from the repo config so the premise holds whatever pairing an
    operator has seated today.
    """

    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    (configs / "staffing.toml").write_text(
        'seated_pairing = "cross-vendor"\n'
        "\n"
        "[pairings.cross-vendor.senior]\n"
        'harness = "codex"\n'
        'model = "gpt-test"\n'
        "capacity = 2\n"
        "\n"
        "[pairings.cross-vendor.staff]\n"
        'harness = "claude"\n'
        'model = "claude-test"\n'
        "capacity = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(configs))
    get_settings.cache_clear()


def test_an_outage_seated_implementer_resolves_to_the_senior_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 2026-08-29 denial, reproduced and then resolved by the declared seat.

    A codex quota outage seated claude as the implementer while the static
    staffing file still declared the cross-vendor pairing. The dispatch path
    sent only the role, `implementer`, which names no section, so resolution
    fell to the bench - and the bench answered with claude's static seat,
    `staff`, denying `run_command` to a live implementation milestone (work
    unit e5d41f8805f4f955d7b1e832cc7fd4ee). The compiled plan knew the seat all
    along; the dispatch path now declares it, and the seat outranks the bench.
    """

    _pin_cross_vendor_seating(monkeypatch, tmp_path)
    policy = load_policy_document()

    seat_blind = policy_principal("claude", "implementer", policy)
    seated = policy_principal("claude", "implementer", policy, seat=DispatchTier.SENIOR)

    # The trap, still real for a caller that has no compiled plan in scope.
    assert seat_blind == DispatchTier.STAFF.value
    assert not policy.permits(seat_blind, Capability.RUN_COMMAND)
    # The seat the plan bound to the task outranks the bench's guess.
    assert seated == DispatchTier.SENIOR.value
    assert policy.permits(seated, Capability.RUN_COMMAND)
    assert policy.permits(seated, Capability.WRITE_REPOSITORY)


def test_a_role_with_its_own_section_outranks_the_declared_seat() -> None:
    """A cast stance keeps its section on whatever seat it is staffed.

    `pow_wow/cast.py` promises that a member named `marketing` is governed by
    `## Principal: marketing` when that section exists. The declared seat slots
    in after that promise, so handing the gate a seat cannot re-file a stance
    under the tier that happens to run it.
    """

    policy = _document(
        "## Principal: marketing\nNever: run_command\n\n## Principal: senior\nMay: run_command\n"
    )

    principal = policy_principal("claude", "marketing", policy, seat=DispatchTier.SENIOR)

    assert principal == "marketing"
    assert not policy.permits(principal, Capability.RUN_COMMAND)


def test_a_vendor_named_section_still_wins_when_an_operator_writes_one() -> None:
    """Migration is one principal at a time, and a deliberate pin still holds."""

    policy = parse_policy_document(
        "## Principal: senior\n"
        "May: read_repo_context, code_worktree_write\n"
        "\n"
        "## Principal: someharness\n"
        "Never: code_worktree_write\n"
    )

    pinned = policy_principal("someharness", "someharness", policy)

    assert pinned == "someharness"
    assert not policy.permits(pinned, Capability.WRITE_REPOSITORY)

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The resident loops are singletons over the database, not over a directory.

The bug these cover: `start-agent-runtime.sh` guarded the enqueue drainer and
the ledger dispatcher with a pid file under `$ROOT/.local_agent/run`, which is
per-checkout, so running the script in a second git worktree started a second
pair against the same coordination database. `FOR UPDATE SKIP LOCKED` kept
claiming correct, so the symptom was not a crash or a duplicated WorkUnit: it
was that the checkout whose code executed a given WorkUnit became a race between
two branches.

The guard is now an advisory lock on the coordination database itself, which is
the thing actually being contended.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from local_first_agent_os.coordination import resident_loop
from local_first_agent_os.coordination.dispatcher_loop import run_ledger_dispatcher
from local_first_agent_os.coordination.resident_loop import (
    ResidentLoop,
    ResidentLoopBusy,
    ResidentLoopHeld,
    describe_resident_loops,
    hold_resident_loop,
)
from local_first_agent_os.work_units.commands import run_enqueue_drainer


def test_a_second_holder_of_the_same_loop_is_refused() -> None:
    with (
        hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as first,
        hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as second,
    ):
        assert isinstance(first, ResidentLoopHeld)
        assert isinstance(second, ResidentLoopBusy)


def test_the_refusal_names_the_process_and_checkout_that_holds_it() -> None:
    """Two worktrees is the case this exists for, so 'busy' is not enough.

    The checkout is the one variable-width field in an `application_name`
    Postgres caps at 63 bytes, so a long worktree directory is reported as the
    trailing fragment that survived, flagged. Asserting raw equality here made
    this test pass or fail on the *length of the directory it was run from*:
    green in `local_first_agent_os`, red in a worktree named after its branch.
    What the refusal owes a reader is a name that still tells two worktrees
    apart, which is the tail, so that is what this asserts.
    """

    with (
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER),
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER) as second,
    ):
        assert isinstance(second, ResidentLoopBusy)
        assert second.owner is not None
        assert second.owner.pid == os.getpid()
        assert second.owner.is_on_this_host

        checkout = resident_loop.runtime_checkout().name
        if second.owner.checkout_truncated:
            assert second.owner.checkout
            assert checkout.endswith(second.owner.checkout)
        else:
            assert second.owner.checkout == checkout


def test_releasing_hands_the_loop_to_the_next_process() -> None:
    with hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as first:
        assert isinstance(first, ResidentLoopHeld)

    with hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as second:
        assert isinstance(second, ResidentLoopHeld)


def test_the_two_loops_do_not_lock_each_other_out() -> None:
    with (
        hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as drainer,
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER) as dispatcher,
    ):
        assert isinstance(drainer, ResidentLoopHeld)
        assert isinstance(dispatcher, ResidentLoopHeld)


def test_deliberate_per_tier_dispatchers_still_run_alongside_each_other() -> None:
    """`--tier` exists to fan out one dispatcher per tier; those are not duplicates.

    Only a dispatcher claiming the same tiers as another is a duplicate, so the
    scope has to be part of the lock's identity rather than the loop alone.
    """

    with (
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER, scope="senior") as senior,
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER, scope="staff") as staff,
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER, scope="senior") as again,
    ):
        assert isinstance(senior, ResidentLoopHeld)
        assert isinstance(staff, ResidentLoopHeld)
        assert isinstance(again, ResidentLoopBusy)


def test_an_unscoped_dispatcher_does_not_block_a_tier_scoped_one() -> None:
    with (
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER) as unscoped,
        hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER, scope="senior") as senior,
    ):
        assert isinstance(unscoped, ResidentLoopHeld)
        assert isinstance(senior, ResidentLoopHeld)


@pytest.mark.parametrize(
    ("command", "loop"),
    [
        (lambda: run_enqueue_drainer(max_polls=1), ResidentLoop.ENQUEUE_DRAINER),
        (lambda: run_ledger_dispatcher(max_polls=1), ResidentLoop.LEDGER_DISPATCHER),
    ],
    ids=["enqueue_drainer", "ledger_dispatcher"],
)
def test_the_loop_commands_refuse_to_start_when_the_loop_is_owned(
    command: Any,
    loop: ResidentLoop,
) -> None:
    """The refusal has to happen before the loop builds a runtime and polls."""

    with hold_resident_loop(loop):
        result = command()

    assert result["ok"] is False
    assert result["error"] == "resident_loop_busy"
    assert result["loop"] == loop.value
    assert result["owner"]["pid"] == os.getpid()


def test_describe_resident_loops_reports_every_loop_and_its_owner() -> None:
    with hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER):
        payload = describe_resident_loops()

    by_name = {entry["loop"]: entry for entry in payload["loops"]}
    assert set(by_name) == {loop.value for loop in ResidentLoop}
    assert by_name[ResidentLoop.LEDGER_DISPATCHER.value]["owned"] is True
    assert by_name[ResidentLoop.LEDGER_DISPATCHER.value]["description"]
    assert by_name[ResidentLoop.ENQUEUE_DRAINER.value]["owned"] is False
    assert by_name[ResidentLoop.ENQUEUE_DRAINER.value]["owner"] is None


def test_the_lock_is_scoped_to_the_coordination_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test schema's dispatcher must not lock out the real one, or vice versa.

    Advisory lock keys are cluster-wide, so this is not automatic: without the
    schema in the key, running the suite would silently stop the machine's
    running runtime from taking its own locks.
    """

    with hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as first:
        assert isinstance(first, ResidentLoopHeld)
        monkeypatch.setenv("AGENT_COORDINATION_SCHEMA", "some_other_namespace")
        with hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as elsewhere:
            assert isinstance(elsewhere, ResidentLoopHeld)


def _worktree(name: str) -> Path:
    return Path("/tmp") / name


class TestOwnerEncoding:
    """`application_name` is capped at 63 bytes and truncates rather than fails."""

    def test_a_round_trip_preserves_the_owner(self) -> None:
        encoded = resident_loop._encode_owner(4242, "0123456789abcdef", _worktree("my-worktree"))
        decoded = resident_loop._decode_owner(encoded)

        assert decoded is not None
        assert decoded.pid == 4242
        assert decoded.revision == "0123456"
        assert decoded.checkout == "my-worktree"
        assert decoded.checkout_truncated is False
        assert decoded.is_on_this_host

    def test_a_long_checkout_name_fits_and_says_it_was_truncated(self) -> None:
        checkout = _worktree("a-very-long-worktree-directory-name" * 3)
        encoded = resident_loop._encode_owner(4242, "0123456789abcdef", checkout)

        assert len(encoded.encode("utf-8")) <= resident_loop._APPLICATION_NAME_LIMIT
        decoded = resident_loop._decode_owner(encoded)
        assert decoded is not None
        assert decoded.checkout_truncated is True
        assert checkout.name.endswith(decoded.checkout)

    def test_the_real_worktree_names_in_this_repo_are_not_truncated(self) -> None:
        """The names this repo actually generates should survive whole.

        Truncation is a correctness backstop, not the expected path: an
        operator reading "already owned by ..." needs to recognize the
        directory, and `<topic>-<hash>` is how worktrees here are named.
        """

        checkout = _worktree("agent-acl-gawd-compilation-a396d3")
        decoded = resident_loop._decode_owner(
            resident_loop._encode_owner(999999, "28795c9abcdef", checkout)
        )

        assert decoded is not None
        assert decoded.checkout == checkout.name
        assert decoded.checkout_truncated is False

    def test_an_unrelated_application_name_decodes_to_no_owner(self) -> None:
        assert resident_loop._decode_owner("psql") is None
        assert resident_loop._decode_owner("") is None
        assert resident_loop._decode_owner(None) is None

    def test_a_foreign_host_is_never_reported_as_signalable(self) -> None:
        """`stop-agent-runtime.sh` signals this pid, so the host must gate it."""

        encoded = resident_loop._encode_owner(4242, "0123456", _worktree("w"))
        foreign = encoded.replace(resident_loop._host_fingerprint(), "ffffffff")
        decoded = resident_loop._decode_owner(foreign)

        assert decoded is not None
        assert decoded.is_on_this_host is False
        assert "on another host" in decoded.describe()


def test_the_lock_key_stays_inside_a_signed_bigint() -> None:
    """`pg_locks` decomposes the key into two oids and the holder query re-adds them.

    A key with the high bit set would overflow `bigint` in that arithmetic and
    the holder lookup would error instead of returning a row, which is exactly
    the path that turns a useful refusal into an unexplained one.
    """

    for loop in ResidentLoop:
        for scope in (None, "senior", "staff"):
            key = resident_loop._lock_key(loop, scope)
            assert 0 <= key <= 0x7FFF_FFFF_FFFF_FFFF

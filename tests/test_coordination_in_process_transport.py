# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The in-process transport, against the same live ledger as the subprocess one.

The interesting assertions here are all seam assertions. A fake ledger core
would be written from the same mental model as the transport and could only
agree with it, so every test below drives the real packaged CLI against the
test's own Postgres schema and compares the two transports' answers.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from local_first_agent_os.coordination import transport as transport_module
from local_first_agent_os.coordination.contracts import (
    CreateSaga,
    ListSagas,
)
from local_first_agent_os.coordination.ledger_selection import (
    ConflictingLedgerSelection,
    CoordinationLedgerSelection,
    applied_ledger_selection,
)
from local_first_agent_os.coordination.store import repo_root
from local_first_agent_os.coordination.transport import (
    CoordinationTransportFactory,
    InProcessCoordinationTransport,
    PackagedCoordinationCore,
    SubprocessCoordinationTransport,
    legacy_command,
)
from local_first_agent_os.settings import CoordinationTransportKind, Settings


def _selection(root: Path) -> CoordinationLedgerSelection:
    """The ledger this test's autouse fixture already created, at `root`."""

    resolved = CoordinationLedgerSelection.resolve()
    return CoordinationLedgerSelection(
        root=root,
        backend=resolved.backend,
        database_url=resolved.database_url,
        schema=resolved.schema,
    )


def _both_transports(
    root: Path,
) -> tuple[InProcessCoordinationTransport, SubprocessCoordinationTransport]:
    selection = _selection(root)
    return (
        InProcessCoordinationTransport(PackagedCoordinationCore(selection)),
        SubprocessCoordinationTransport(selection=selection),
    )


def test_both_transports_return_the_same_payload_for_the_same_command(tmp_path: Path) -> None:
    in_process, subprocess_transport = _both_transports(tmp_path)

    through_subprocess = subprocess_transport.execute(CreateSaga(goal="parity: subprocess"))
    through_memory = in_process.execute(CreateSaga(goal="parity: in process"))

    assert sorted(through_subprocess) == sorted(through_memory)
    assert through_memory["ok"] is True

    # Each transport can read the row the other one wrote, which is the only
    # proof that they reached the same database rather than two consistent ones.
    saga_id = str(through_memory["saga_id"])
    read_back = subprocess_transport.execute(legacy_command(["get_saga", saga_id]))
    assert cast(Mapping[str, object], read_back["saga"])["goal"] == "parity: in process"

    listed = in_process.execute(ListSagas())
    goals = {
        cast(Mapping[str, object], saga)["goal"] for saga in cast(list[object], listed["sagas"])
    }
    assert {"parity: subprocess", "parity: in process"} <= goals


def test_the_in_process_transport_starts_no_child_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the change, asserted rather than assumed.

    `subprocess.run` is the one call the old transport could not do without, so
    replacing it with a detonator is a check that fails if the in-process lane
    ever quietly routes back through the CLI script.
    """

    in_process, _ = _both_transports(tmp_path)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the in-process transport forked a child")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(transport_module.subprocess, "run", refuse)

    payload = in_process.execute(CreateSaga(goal="no child process"))

    assert payload["ok"] is True


def test_a_failing_command_raises_the_same_error_contract(tmp_path: Path) -> None:
    in_process, subprocess_transport = _both_transports(tmp_path)

    with pytest.raises(RuntimeError) as through_memory:
        in_process.execute(legacy_command(["get_saga", "saga-that-does-not-exist"]))
    with pytest.raises(RuntimeError) as through_subprocess:
        subprocess_transport.execute(legacy_command(["get_saga", "saga-that-does-not-exist"]))

    assert "get_saga" in str(through_memory.value)
    assert str(through_memory.value) == str(through_subprocess.value)


def test_an_unparseable_command_is_an_error_rather_than_an_interpreter_exit(
    tmp_path: Path,
) -> None:
    """argparse exits the process on a bad command line.

    In a child that is a failed subprocess the parent reports. In this process it
    would take the daemon down, so the parser's `SystemExit` has to stop here.
    """

    from local_first_agent_os.coordination.cli import execute_argv

    with applied_ledger_selection(_selection(tmp_path)):
        payload = execute_argv(["get_saga"])  # the id is required

    assert payload["ok"] is False
    assert payload["error"] == "ArgumentError"


def test_serve_is_refused_in_process(tmp_path: Path) -> None:
    from local_first_agent_os.coordination.cli import execute_argv

    with (
        applied_ledger_selection(_selection(tmp_path)),
        pytest.raises(ValueError, match="serve"),
    ):
        execute_argv(["serve"])


def test_applying_a_selection_points_the_process_at_its_root(tmp_path: Path) -> None:
    selection = _selection(tmp_path)

    with applied_ledger_selection(selection):
        assert repo_root() == tmp_path.resolve()

    # Deliberately not restored: the readers of this are the ledger's own path
    # helpers, and the ledger is where the command sent them.
    assert repo_root() == tmp_path.resolve()


def test_two_threads_sharing_a_selection_hold_it_at_the_same_time(tmp_path: Path) -> None:
    """Same selection must not serialize.

    A lock held for the duration of every command would deadlock the first
    resident loop that ran through this transport, because `run_ledger_dispatcher`
    never returns.
    """

    selection = _selection(tmp_path)
    both_inside = threading.Barrier(2, timeout=5)
    failures: list[BaseException] = []

    def hold() -> None:
        try:
            with applied_ledger_selection(selection):
                both_inside.wait()
        except BaseException as failure:  # noqa: BLE001 - reported to the assertion below
            failures.append(failure)

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert not any(thread.is_alive() for thread in threads)


def test_a_second_selection_waits_for_the_first_to_drain(tmp_path: Path) -> None:
    first = _selection(tmp_path / "first")
    second = _selection(tmp_path / "second")
    first_is_held = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def hold_first() -> None:
        with applied_ledger_selection(first):
            first_is_held.set()
            release_first.wait(timeout=5)

    def take_second() -> None:
        with applied_ledger_selection(second):
            second_entered.set()

    holder = threading.Thread(target=hold_first)
    taker = threading.Thread(target=take_second)
    holder.start()
    assert first_is_held.wait(timeout=5)
    taker.start()

    assert not second_entered.wait(timeout=0.5), "a different selection switched mid-command"
    assert repo_root() == (tmp_path / "first").resolve()

    release_first.set()
    holder.join(timeout=5)
    taker.join(timeout=5)
    assert second_entered.is_set()
    assert repo_root() == (tmp_path / "second").resolve()


def test_nesting_a_different_selection_is_a_named_error_rather_than_a_deadlock(
    tmp_path: Path,
) -> None:
    with (
        applied_ledger_selection(_selection(tmp_path / "outer")),
        pytest.raises(ConflictingLedgerSelection),
        applied_ledger_selection(_selection(tmp_path / "inner")),
    ):
        pass


def test_nesting_the_same_selection_is_allowed(tmp_path: Path) -> None:
    selection = _selection(tmp_path)

    with applied_ledger_selection(selection), applied_ledger_selection(selection):
        assert repo_root() == tmp_path.resolve()


def test_a_selection_renders_the_environment_a_child_needs() -> None:
    selection = CoordinationLedgerSelection(
        root=Path("/ledger"),
        backend="postgres",
        database_url="postgresql://host/db",
        schema="test_schema",
    )

    environment = selection.child_environment({"UNRELATED": "kept"})

    assert environment["UNRELATED"] == "kept"
    assert environment["AGENT_COORDINATION_BACKEND"] == "postgres"
    assert environment["AGENT_COORDINATION_DATABASE_URL"] == "postgresql://host/db"
    assert environment["AGENT_COORDINATION_SCHEMA"] == "test_schema"


def test_a_selection_without_a_url_removes_an_inherited_one() -> None:
    """An inherited URL would silently contradict a selection that has none."""

    selection = CoordinationLedgerSelection(
        root=Path("/ledger"),
        backend="postgres",
        database_url=None,
        schema=None,
    )

    environment = selection.child_environment(
        {"AGENT_COORDINATION_DATABASE_URL": "postgresql://inherited/db"}
    )

    assert "AGENT_COORDINATION_DATABASE_URL" not in environment


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (CoordinationTransportKind.IN_PROCESS, InProcessCoordinationTransport),
        (CoordinationTransportKind.SUBPROCESS, SubprocessCoordinationTransport),
    ],
)
def test_the_factory_builds_the_transport_the_settings_name(
    kind: CoordinationTransportKind,
    expected: type,
) -> None:
    settings = Settings.model_validate({"coordination_transport": kind, "mock_models": True})

    assert isinstance(CoordinationTransportFactory.create(settings=settings), expected)


def test_the_default_transport_is_in_process() -> None:
    """The setting names a working state, and this is the one that ships.

    A subprocess per coordination command cost about 0.42s of interpreter start
    against 0.010s in process, measured on 2026-07-31 against the live Postgres.
    """

    assert Settings.model_validate({"mock_models": True}).coordination_transport is (
        CoordinationTransportKind.IN_PROCESS
    )

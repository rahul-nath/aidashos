# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression coverage for coordination child-process stdio.

A resident daemon can hold a revoked tty as fd 0 (macOS revokes the launching
terminal's descriptor when that terminal exits). A coordination child that
inherits it dies at interpreter startup with ``init_sys_streams`` / EBADF
before printing JSON. The revoked state cannot be fabricated portably, so the
tests pin the contract instead: the child's stdin is ``os.devnull`` no matter
what the parent's fd 0 is.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from local_first_agent_os.coordination import transport as transport_module
from local_first_agent_os.coordination.contracts import CoordinationCommandName
from local_first_agent_os.coordination.ledger_selection import CoordinationLedgerSelection
from local_first_agent_os.coordination.transport import (
    CoordinationCommandRefused,
    InProcessCoordinationTransport,
    SubprocessCoordinationTransport,
    command_from_argv,
)
from local_first_agent_os.daemon_stdio import detach_inherited_stdin


@pytest.mark.parametrize("ok", [False, None, 1, "true"])
def test_owner_refusal_retains_typed_command_and_payload(ok: object) -> None:
    command = command_from_argv([CoordinationCommandName.LIST_LEDGER_EVENTS.value])
    transport = InProcessCoordinationTransport(
        lambda _: {"ok": ok, "error": "fixture_owner_refusal"}
    )
    with pytest.raises(CoordinationCommandRefused) as refused:
        transport.execute(command)
    assert isinstance(refused.value, RuntimeError)
    assert refused.value.command is command.name
    assert refused.value.payload == {"ok": ok, "error": "fixture_owner_refusal"}


def test_owner_success_requires_an_actual_boolean_true() -> None:
    command = command_from_argv([CoordinationCommandName.LIST_LEDGER_EVENTS.value])
    assert InProcessCoordinationTransport(lambda _: {"ok": True}).execute(command) == {"ok": True}


def _is_devnull(stat_result: os.stat_result) -> bool:
    devnull = os.stat(os.devnull)
    return (stat_result.st_dev, stat_result.st_ino) == (devnull.st_dev, devnull.st_ino)


@pytest.fixture
def parent_stdin_regular_file(tmp_path: Path) -> Iterator[None]:
    """Point this process's fd 0 at a regular file for the test's duration.

    Pytest often leaves fd 0 on ``os.devnull`` already, which would make the
    assertions pass vacuously without this fixture.
    """
    saved_stdin = os.dup(0)
    with (tmp_path / "parent-stdin").open("w+b") as handle:
        os.dup2(handle.fileno(), 0)
        try:
            yield
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)


def test_subprocess_transport_gives_children_devnull_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_stdin_regular_file: None,
) -> None:
    stub_script = tmp_path / "stub_coordination_cli.py"
    stub_script.write_text(
        textwrap.dedent(
            """
            import json
            import os

            devnull = os.stat(os.devnull)
            stdin = os.fstat(0)
            print(json.dumps({
                "ok": True,
                "stdin_is_devnull": (
                    (stdin.st_dev, stdin.st_ino) == (devnull.st_dev, devnull.st_ino)
                ),
            }))
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(transport_module, "coordination_script_path", lambda: stub_script)
    transport = SubprocessCoordinationTransport(
        selection=CoordinationLedgerSelection(
            root=tmp_path,
            backend="postgres",
            database_url=None,
            schema=None,
        )
    )

    payload = transport.execute(command_from_argv(["get_gawd_doc", "any-doc-id"]))

    assert payload["stdin_is_devnull"] is True


def test_detach_inherited_stdin_rebinds_fd0_to_devnull(
    parent_stdin_regular_file: None,
) -> None:
    assert not _is_devnull(os.fstat(0))
    detach_inherited_stdin()
    assert _is_devnull(os.fstat(0))

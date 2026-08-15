# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pins on what importing pow-wow modules costs.

The package index once star-imported the executor, so importing any leaf
module - ``protocol`` for one enum, ``types`` for a dataclass - executed the
full executor and its transitive imports. Besides the load cost, that created
an import cycle with ``spawn_authority`` that only fired in processes which
imported ``spawn_authority`` before ``pow_wow``: ``pytest tests/test_staffing.py``
alone failed collection while the full suite passed, because the suite happened
to import in the safe order. These tests hold the fixed shape in place, in
fresh subprocesses, so import order inside any one test process cannot mask a
regression the way it masked the original bug.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import local_first_agent_os.pow_wow as pow_wow_package

_EXECUTOR_MODULE = "local_first_agent_os.pow_wow.executor"

_LEAF_MODULES = sorted(
    path.stem
    for path in Path(pow_wow_package.__file__).parent.glob("*.py")
    if path.stem not in {"__init__", "executor"}
)


def _run_probe(code: str) -> None:
    probe = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"


@pytest.mark.parametrize("leaf", _LEAF_MODULES)
def test_leaf_module_imports_without_executing_the_executor(leaf: str) -> None:
    """A new leaf module is covered by the directory glob the day it appears."""

    _run_probe(
        "import sys;"
        f"import local_first_agent_os.pow_wow.{leaf};"
        f"assert {_EXECUTOR_MODULE!r} not in sys.modules, 'leaf import pulled the executor';"
        "print('ok')"
    )


def test_package_import_does_not_execute_the_executor() -> None:
    _run_probe(
        "import sys;"
        "import local_first_agent_os.pow_wow;"
        f"assert {_EXECUTOR_MODULE!r} not in sys.modules, 'package import pulled the executor';"
        "print('ok')"
    )


def test_spawn_authority_first_import_order_no_longer_cycles() -> None:
    """The original reproduction: ``spawn_authority`` imported before ``pow_wow``.

    With the eager index this order crashed on a half-initialized module,
    because ``spawn_authority`` needs ``pow_wow.protocol``, the index dragged in
    the executor, and the executor imports ``spawn_authority`` back.
    """

    _run_probe(
        "from local_first_agent_os.spawn_authority import authority_for_purpose;"
        "from local_first_agent_os.pow_wow.protocol import TaskPurpose;"
        "assert authority_for_purpose(TaskPurpose.REVIEW) is not None;"
        "print('ok')"
    )


def test_every_package_export_resolves_lazily() -> None:
    """Each name in ``__all__`` must resolve from the module mapped as its home.

    A typo in the lazy export map fails only on attribute access, not at import
    time, so this walks the whole surface. Executor-backed names are exercised
    too; the laziness pins above run in subprocesses this loop cannot pollute.
    """

    for name in pow_wow_package.__all__:
        assert getattr(pow_wow_package, name) is not None
    assert set(pow_wow_package.__all__) <= set(dir(pow_wow_package))
    # `__all__` is a literal so pyright can evaluate it; the map is what the
    # runtime serves. Neither may drift from the other.
    assert set(pow_wow_package.__all__) == set(pow_wow_package._EXPORT_HOMES)

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The launch boundary owns either a process group or a broker connection."""

from __future__ import annotations

import json
import os
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ProcessOwnership(StrEnum):
    GROUP = "group"
    BROKER_PROXY = "broker_proxy"

    @property
    def start_new_session(self) -> bool:
        return self is ProcessOwnership.GROUP

    def send_signal(self, pid: int, value: signal.Signals) -> None:
        if self is ProcessOwnership.GROUP:
            os.killpg(pid, value)
        else:
            # Closing this proxy's connection revokes its host-owned native group.
            os.kill(pid, value)


@dataclass(frozen=True)
class ProcessLaunch:
    command: tuple[str, ...]
    environment: Mapping[str, str]
    ownership: ProcessOwnership


def prepare_process_launch(
    command: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> ProcessLaunch:
    """A verifier never asks its already-sandboxed children to detach themselves."""
    from .native_verification_broker import BROKER_ENV, broker_command

    configuration = os.environ.get(BROKER_ENV)
    if configuration is None:
        return ProcessLaunch(tuple(command), environment, ProcessOwnership.GROUP)
    broker = json.loads(configuration)
    if len(command) == 3 and command[1] == broker["proxy"]:
        delegated = tuple(command)
    else:
        # The gate owns this scratch directory and removes it with all other outputs.
        scratch = Path(tempfile.mkdtemp(prefix="native-capture-", dir=os.environ["TMPDIR"]))
        delegated = broker_command(
            command, cwd, scratch, environment, posture="supervised_commands", harness="codex"
        )
        if delegated is None:
            raise RuntimeError("an active verifier lost its native launch authority")
    # Generic gate environments strip inherited control-plane variables.
    # This boundary restores only its current, kernel-authenticated transport.
    return ProcessLaunch(
        delegated, {**environment, BROKER_ENV: configuration}, ProcessOwnership.BROKER_PROXY
    )

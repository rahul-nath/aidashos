from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from test_agent_execution_supervisor import _Artifacts, _coord, _lease

from local_first_agent_os.agent_execution_supervisor import StreamingCommandSupervisor
from local_first_agent_os.operator_identity import OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV
from local_first_agent_os.pow_wow.process import run_captured_command

_PROBE = (
    "import json,os; print(json.dumps({"
    "'privileged':os.environ.get('LOCAL_AGENT_OPERATOR_TOKEN'),"
    "'locator':os.environ.get('LOCAL_AGENT_OPERATOR_TOKEN_FILE'),"
    "'ordinary':os.environ.get('ORDINARY_WORKER_SETTING')}))"
)


@pytest.mark.parametrize("complete_environment", [False, True])
def test_captured_child_cannot_receive_operator_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete_environment: bool
) -> None:
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "host-canary-secret")
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, "/host/credential")
    capture = run_captured_command(
        (sys.executable, "-c", _PROBE),
        tmp_path,
        timeout_seconds=5,
        env={
            OPERATOR_TOKEN_ENV: "explicit-canary-secret",
            OPERATOR_TOKEN_FILE_ENV: "/override/credential",
            "ORDINARY_WORKER_SETTING": "kept",
        },
        complete_environment=complete_environment,
    )
    assert capture.exit_code == 0
    assert json.loads(capture.stdout) == {"privileged": None, "locator": None, "ordinary": "kept"}


@pytest.mark.parametrize("complete_environment", [False, True])
def test_supervised_child_cannot_receive_operator_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete_environment: bool
) -> None:
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "host-canary-secret")
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, "/host/credential")
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord, artifact_writer=_Artifacts(), heartbeat_seconds=0.02
    )
    result = asyncio.run(
        supervisor.run(
            (sys.executable, "-c", _PROBE),
            tmp_path,
            lease=_lease(tmp_path),
            harness="codex",
            timeout_seconds=5,
            env={
                OPERATOR_TOKEN_ENV: "explicit-canary-secret",
                OPERATOR_TOKEN_FILE_ENV: "/override/credential",
                "ORDINARY_WORKER_SETTING": "kept",
            },
            complete_environment=complete_environment,
        )
    )
    assert result.capture.exit_code == 0
    assert json.loads(result.capture.stdout) == {
        "privileged": None,
        "locator": None,
        "ordinary": "kept",
    }

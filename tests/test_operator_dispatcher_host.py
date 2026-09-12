from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_first_agent_os import operator_dispatcher_host as host
from local_first_agent_os.coordination import cli
from local_first_agent_os.operator_identity import OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV


def test_explicit_host_bootstrap_authenticates_only_the_dispatcher_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "operator.token"
    credential.write_text("local-host-canary")
    credential.chmod(0o600)
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, str(credential))
    monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
    calls: list[list[str]] = []

    def run(argv: list[str]) -> int:
        assert os.environ[OPERATOR_TOKEN_ENV] == "local-host-canary"
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "main", run)
    assert host.main(["--root", str(tmp_path)]) == 0
    assert calls == [
        ["--root", str(tmp_path), "run_ledger_dispatcher", "--interval-seconds", "2.0"]
    ]
    assert OPERATOR_TOKEN_ENV not in os.environ


def test_host_forwards_configured_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credential = tmp_path / "operator.token"
    credential.write_text("local-host-canary")
    credential.chmod(0o600)
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, str(credential))
    calls: list[list[str]] = []

    def run(argv: list[str]) -> int:
        assert os.environ[OPERATOR_TOKEN_ENV] == "local-host-canary"
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "main", run)
    assert host.main(["--root", str(tmp_path), "--interval-seconds", "0.5"]) == 0
    assert calls == [
        ["--root", str(tmp_path), "run_ledger_dispatcher", "--interval-seconds", "0.5"]
    ]


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf"])
def test_invalid_interval_refuses_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interval: str
) -> None:
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, str(tmp_path / "absent.token"))
    with pytest.raises(SystemExit) as refused:
        host.main(["--root", str(tmp_path), "--interval-seconds", interval])
    assert refused.value.code == 2


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_host_bootstrap_refuses_exposed_credential_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    credential = tmp_path / "operator.token"
    credential.write_text("local-host-canary")
    credential.chmod(mode)
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, str(credential))
    monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
    with pytest.raises(PermissionError):
        host.provision_dispatcher_host()
    assert OPERATOR_TOKEN_ENV not in os.environ


def test_host_bootstrap_refuses_symlink_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "operator.token"
    credential.write_text("local-host-canary")
    credential.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(credential)
    monkeypatch.setenv(OPERATOR_TOKEN_FILE_ENV, str(link))
    monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
    with pytest.raises(OSError):
        host.provision_dispatcher_host()
    assert OPERATOR_TOKEN_ENV not in os.environ

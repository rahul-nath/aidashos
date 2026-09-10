# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("docker_ready", [True, False])
def test_runtime_relaunch_initializes_identity_then_starts_only_installed_verification_service(
    tmp_path: Path, docker_ready: bool
) -> None:
    """Exercise actual startup wiring, stopping before database or model runtime mutation."""

    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "start-agent-runtime.sh",
        "initialize-operator-identity.sh",
        "start-verification-database.sh",
    ):
        shutil.copy2(source / "scripts" / name, scripts / name)
    core = scripts / "start-docker-compose-infra.sh"
    core.write_text('#!/bin/sh\necho "core-infrastructure" >> "$STARTUP_TRACE"\n')
    core.chmod(0o755)
    commands = tmp_path / "commands"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        "sync) exit 0 ;;\n"
        '"run --offline --no-sync python -m '
        'local_first_agent_os.operator_credentials initialize")\n'
        '  echo identity >> "$STARTUP_TRACE"\n'
        '  exec "$STARTUP_PYTHON" -m local_first_agent_os.operator_credentials initialize ;;\n'
        '"run local-agent init-db") exit 91 ;;\n'
        '*) echo "undeclared uv startup command" >&2; exit 97 ;;\nesac\n'
    )
    uv.chmod(0o755)
    git = commands / "git"
    git.write_text("#!/bin/sh\necho fixture-revision\n")
    git.chmod(0o755)
    docker = commands / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        f"info) exit {0 if docker_ready else 1} ;;\n"
        '"compose up --pull never --no-build --wait --wait-timeout 120 -d postgres-test")\n'
        '  echo verification-service >> "$STARTUP_TRACE"; exit 0 ;;\n'
        '*) echo "undeclared docker startup command" >&2; exit 97 ;;\nesac\n'
    )
    docker.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "trace"
    token = home / "operator.token"
    completed = subprocess.run(
        ["bash", str(scripts / "start-agent-runtime.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(source / "src"),
            "STARTUP_PYTHON": sys.executable,
            "STARTUP_TRACE": str(trace),
            "LOCAL_AGENT_OPERATOR_TOKEN_FILE": str(token),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert token.stat().st_mode & 0o777 == 0o600
    assert token.read_text().strip()
    if docker_ready:
        assert completed.returncode == 91, completed.stderr
        assert trace.read_text().splitlines() == [
            "identity",
            "core-infrastructure",
            "verification-service",
        ]
    else:
        assert completed.returncode == 1
        assert "Docker is unavailable" in completed.stderr
        assert trace.read_text().splitlines() == ["identity", "core-infrastructure"]


def test_startup_dotenv_loader_preserves_json_array(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / ".env"
    expected = ["--headless=new", "--user-data-dir=/tmp/profile with spaces"]
    env_file.write_text(
        "LOCAL_AGENT_CHROME_DEVTOOLS_COMMAND_ARGS="
        + json.dumps(expected, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    # Pass the interpreter running the tests through the loader's documented
    # override so this does not depend on a .venv existing in the checkout.
    python_bin = shlex.quote(sys.executable)
    command = f"""
source scripts/start-agent-runtime.sh
load_dotenv_file "$DOTENV_TEST_FILE" {python_bin}
{python_bin} - <<'PY'
import json
import os

value = os.environ["LOCAL_AGENT_CHROME_DEVTOOLS_COMMAND_ARGS"]
assert json.loads(value) == [
    "--headless=new",
    "--user-data-dir=/tmp/profile with spaces",
]
PY
"""
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=repo_root,
        env={
            **os.environ,
            "DOTENV_TEST_FILE": str(env_file),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def _parse_runtime_flag(
    variable: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> str:
    """Run the script's argument parser and report one closed decision."""

    assert variable in {"START_ASR", "PROBE_FRONTIER"}
    repo_root = Path(__file__).resolve().parents[1]
    command = (
        f'source scripts/start-agent-runtime.sh\nparse_runtime_args "$@"\necho "${{{variable}}}"'
    )
    completed = subprocess.run(
        ["bash", "-c", command, "_", *args],
        cwd=repo_root,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _parse_runtime_args(*args: str, env: dict[str, str] | None = None) -> str:
    return _parse_runtime_flag("START_ASR", *args, env=env)


def test_asr_is_off_unless_a_human_asks_for_it() -> None:
    """whisper.cpp keeps a multi-gigabyte model resident for the whole session.

    Starting it by default charges every runtime for a capability most sessions
    never use, so the default is off and the request is explicit.
    """

    assert _parse_runtime_args() == "false"
    assert _parse_runtime_args("--with-asr") == "true"
    assert _parse_runtime_args("--no-asr") == "false"


def test_an_explicit_flag_beats_the_environment() -> None:
    """The env var is for launchd plists and scripted callers; argv is a person."""

    asked = {"LOCAL_AGENT_START_ASR": "true"}
    assert _parse_runtime_args(env=asked) == "true"
    assert _parse_runtime_args("--no-asr", env=asked) == "false"


def test_frontier_probe_is_off_unless_a_human_asks_for_it() -> None:
    """Restarting services is not authorization to spend a provider request."""

    assert _parse_runtime_flag("PROBE_FRONTIER") == "false"
    assert _parse_runtime_flag("PROBE_FRONTIER", "--frontier-probe") == "true"
    assert _parse_runtime_flag("PROBE_FRONTIER", "--no-frontier-probe") == "false"


def test_an_unknown_argument_is_refused_before_anything_starts() -> None:
    """A typo must not silently start the runtime in the wrong shape."""

    repo_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["bash", "scripts/start-agent-runtime.sh", "--with-asrr"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "Unknown argument: --with-asrr" in completed.stderr


def test_the_whisper_launchd_agent_is_not_bootstrapped_by_default() -> None:
    """Bootstrapping the agent is itself a launch, so it sits behind the gate.

    The bring-up further down was not the only thing that started whisper: the
    launchd bootstrap loop listed its label unconditionally, which brought the
    resident job back on every runtime start.
    """

    script = (Path(__file__).resolve().parents[1] / "scripts" / "start-agent-runtime.sh").read_text(
        encoding="utf-8"
    )
    unconditional, _, gated = script.partition('if [ "$START_ASR" = "true" ]; then')

    assert "com.rahul.local-first-agent.whisper" not in unconditional
    assert "com.rahul.local-first-agent.whisper" in gated


def test_work_moving_residents_start_only_after_junior_readiness() -> None:
    """Startup cannot let a queued dispatch race the model it depends on."""

    script = (Path(__file__).resolve().parents[1] / "scripts" / "start-agent-runtime.sh").read_text(
        encoding="utf-8"
    )
    service_labels = script.split("SERVICE_LAUNCH_LABELS=(", 1)[1].split(")", 1)[0]
    work_labels = script.split("WORK_LAUNCH_LABELS=(", 1)[1].split(")", 1)[0]
    moving = (
        "com.rahul.local-first-agent.enqueue-drainer",
        "com.rahul.local-first-agent.ledger-dispatcher",
        "com.rahul.local-first-agent.work-unit-crash-reconciler",
        "com.rahul.local-first-agent.refinery-fleet",
    )

    assert all(label not in service_labels for label in moving)
    assert all(label in work_labels for label in moving)
    assert script.index('bootstrap_launch_agents "${SERVICE_LAUNCH_LABELS[@]}"') < script.index(
        'active_general_selection="$(uv run local-agent active-general-selection)"'
    )
    assert script.index('uv run pi /start "/$junior_role"') < script.index(
        'bootstrap_launch_agents "${WORK_LAUNCH_LABELS[@]}"'
    )
    assert "uv run pi /start /gemma4" not in script


def test_the_legacy_force_direct_spelling_still_disables_handoff(
    monkeypatch,
) -> None:
    """Every handoff doc tells an operator to set LOCAL_AGENT_PI_FORCE_DIRECT=1.

    It works around the daemon's `init_sys_streams ... Bad file descriptor` bug,
    so renaming it outright would silently send someone following a runbook into
    the very failure they were avoiding. The new name is the honest one; the old
    one keeps working, inverted.
    """

    from local_first_agent_os.settings import Settings

    monkeypatch.delenv("LOCAL_AGENT_PI_HANDOFF_TO_DAEMON", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_PI_FORCE_DIRECT", raising=False)
    assert Settings().pi_handoff_to_daemon is True

    monkeypatch.setenv("LOCAL_AGENT_PI_FORCE_DIRECT", "1")
    assert Settings().pi_handoff_to_daemon is False

    monkeypatch.delenv("LOCAL_AGENT_PI_FORCE_DIRECT")
    monkeypatch.setenv("LOCAL_AGENT_PI_HANDOFF_TO_DAEMON", "false")
    assert Settings().pi_handoff_to_daemon is False


def test_an_explicit_choice_beats_the_legacy_spelling(monkeypatch) -> None:
    """Someone who sets the new name meant it, even with a stale export around."""

    from local_first_agent_os.settings import Settings

    monkeypatch.setenv("LOCAL_AGENT_PI_FORCE_DIRECT", "1")
    monkeypatch.setenv("LOCAL_AGENT_PI_HANDOFF_TO_DAEMON", "false")
    assert Settings().pi_handoff_to_daemon is False


def test_the_llama_port_follows_the_configured_base_url() -> None:
    """Moving the model's URL used to leave the scripts starting 8080.

    The application connects to llama_base_url; the scripts started and stopped
    LOCAL_AGENT_LLAMA_PORT. Both defaulted to 8080, so they agreed by luck rather
    than by construction.
    """

    repo_root = Path(__file__).resolve().parents[1]
    helper = subprocess.run(
        ["sed", "-n", "/_llama_port_from_base_url()/,/^}/p", "scripts/start-llama.sh"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert helper.strip(), "the helper must exist for the scripts to share a port"

    def port(base_url: str | None) -> str:
        env = dict(os.environ)
        env.pop("LOCAL_AGENT_LLAMA_BASE_URL", None)
        if base_url is not None:
            env["LOCAL_AGENT_LLAMA_BASE_URL"] = base_url
        return subprocess.run(
            ["bash", "-c", f"{helper}\n_llama_port_from_base_url || echo 8080"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert port("http://127.0.0.1:9099") == "9099"
    assert port(None) == "8080", "unset falls back to the shared default"
    assert port("http://host/no-port") == "8080", "a malformed URL must not yield a bad port"

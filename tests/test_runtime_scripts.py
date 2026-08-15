# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path


def test_stop_runtime_flushes_sessions_before_booting_out_daemons() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "stop-agent-runtime.sh").read_text(encoding="utf-8")

    assert "flush_session_memory\n\nbootout_launch_agents" in script


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_launchd_environment(
    tmp_path: Path,
    *,
    stuck: bool = False,
    bootout_timeout_seconds: int = 30,
) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "launchctl-state"
    state.mkdir()
    _write_executable(fake_bin / "uv", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "launchctl",
        """#!/usr/bin/env bash
set -euo pipefail
state="$FAKE_LAUNCHCTL_STATE"
command="$1"
target="${2:-}"
case "$command" in
  bootout)
    printf '%s' "$target" > "$state/current"
    printf '2' > "$state/remaining"
    printf 'bootout %s\n' "$target" >> "$state/events"
    ;;
  print)
    [ -f "$state/current" ] || exit 113
    [ "$(cat "$state/current")" = "$target" ] || exit 113
    if [ "${FAKE_LAUNCHCTL_STUCK:-0}" = "1" ]; then
      exit 0
    fi
    remaining="$(cat "$state/remaining")"
    if [ "$remaining" -gt 0 ]; then
      printf '%s' "$((remaining - 1))" > "$state/remaining"
      exit 0
    fi
    rm -f "$state/current" "$state/remaining"
    exit 113
    ;;
  bootstrap)
    if [ -f "$state/current" ]; then
      printf 'bootstrap-before-unload %s\n' "$target" >> "$state/events"
      exit 5
    fi
    printf 'bootstrap %s\n' "$target" >> "$state/events"
    ;;
  *)
    exit 64
    ;;
esac
""",
    )
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LAUNCHCTL_STATE": str(state),
        "FAKE_LAUNCHCTL_STUCK": "1" if stuck else "0",
        "LOCAL_AGENT_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS": str(bootout_timeout_seconds),
        "LOCAL_AGENT_LAUNCHD_BOOTOUT_POLL_SECONDS": "0.01",
    }


def _installed_labels() -> tuple[str, ...]:
    """The labels `install.sh` actually loads, read from the installer itself.

    Counted rather than hardcoded because the number is not the property under
    test: installing one more agent should not fail a test about ordering, and a
    test that has to be renumbered to add an agent teaches people to renumber it
    without reading what it checks.
    """

    script = (Path(__file__).resolve().parents[1] / "scripts" / "launchd" / "install.sh").read_text(
        encoding="utf-8"
    )
    body = script.split("PLISTS=(", 1)[1].split(")", 1)[0]
    return tuple(
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def test_launchd_installer_waits_for_bootout_before_bootstrap(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    # The happy path must never hit the bootout deadline: the fake launchctl needs
    # three subprocess spawns per label, and a one-second budget made this test
    # fail whenever the suite ran it under load.
    env = _fake_launchd_environment(tmp_path)

    result = subprocess.run(
        ["bash", "scripts/launchd/install.sh"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = (Path(env["FAKE_LAUNCHCTL_STATE"]) / "events").read_text(encoding="utf-8")
    labels = _installed_labels()
    assert "bootstrap-before-unload" not in events
    assert events.count("bootout ") == len(labels)
    assert events.count("bootstrap ") == len(labels)
    for label in labels:
        assert f"bootout gui/501/{label}" in events


def test_launchd_installer_fails_closed_when_bootout_never_finishes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    # This one asserts the deadline fires, so it keeps the short budget.
    env = _fake_launchd_environment(tmp_path, stuck=True, bootout_timeout_seconds=1)

    result = subprocess.run(
        ["bash", "scripts/launchd/install.sh"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "Timed out waiting for launchd to unload" in result.stderr
    events = (Path(env["FAKE_LAUNCHCTL_STATE"]) / "events").read_text(encoding="utf-8")
    assert "bootstrap " not in events


def test_pi_daemon_service_does_not_source_dotenv_as_shell(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_repo = tmp_path / "repo with spaces"
    fake_repo.mkdir()
    (fake_repo / ".env").write_text(
        'LOCAL_AGENT_CHROME_DEVTOOLS_COMMAND_ARGS=["-y","chrome-devtools-mcp@latest"]\n',
        encoding="utf-8",
    )
    fake_uv = tmp_path / "uv with spaces"
    fake_uv.touch()
    service = tmp_path / "pi-daemon.plist"

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "render-pi-daemon-service.py"),
            "launchd",
            str(fake_repo),
            str(fake_uv),
            str(service),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with service.open("rb") as handle:
        payload = plistlib.load(handle)
    shell = payload["ProgramArguments"][2]
    assert "source .env" not in shell
    assert "set -a" not in shell
    assert shell == (f"cd '{fake_repo.resolve()}' && exec '{fake_uv.resolve()}' run pi-daemon")
    assert payload["WorkingDirectory"] == str(fake_repo.resolve())
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is False


# --- The two resident loops, supervised ---------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_DIR = REPO_ROOT / "scripts" / "launchd"

# The loops that make queued work move, as (ResidentLoop value, launchd label).
# A machine that reboots must come back with these running, and until they had
# plists it came back with every service up and the queue frozen.
SUPERVISED_LOOPS: tuple[tuple[str, str], ...] = (
    ("work-unit-enqueue-drainer", "com.rahul.local-first-agent.enqueue-drainer"),
    ("ledger-dispatcher", "com.rahul.local-first-agent.ledger-dispatcher"),
)


def _rendered_plist(label: str, tmp_path: Path) -> dict:
    """The plist as launchd would see it, rendered through the real renderer.

    Reading the template directly would assert about `__REPO_ROOT__` rather than
    about what gets installed, and the placeholders are exactly where a path bug
    would live.
    """

    output = tmp_path / f"{label}.plist"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "render-launchd-template.py"),
            str(LAUNCHD_DIR / f"{label}.plist"),
            str(output),
            str(REPO_ROOT),
            sys.executable,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with output.open("rb") as handle:
        return plistlib.load(handle)


def test_every_loop_the_runtime_starts_is_also_supervised(tmp_path: Path) -> None:
    """A third resident loop added to the start script must decide about reboots.

    The start script is the operator's entry point and therefore decides which
    loops matter. Adding one there without a plist reintroduces exactly the gap
    this pair closed: work that moves while someone is logged in and stops
    moving when the machine restarts.
    """

    script = (REPO_ROOT / "scripts" / "start-agent-runtime.sh").read_text(encoding="utf-8")
    started = set(re.findall(r"agent_coordination_mcp\.py[^\n]*?\s(run_[a-z_]+)", script))

    assert started == {"run_enqueue_drainer", "run_ledger_dispatcher"}
    for _loop, label in SUPERVISED_LOOPS:
        assert (LAUNCHD_DIR / f"{label}.plist").exists()


def test_a_supervised_loop_restarts_itself(tmp_path: Path) -> None:
    """Unattended means the machine repairs the loop, not that a human does.

    KeepAlive is what makes a defect-death temporary, and it is also what makes
    an agent whose lock is held elsewhere a standby that takes over when the
    other copy stops. ThrottleInterval is what keeps that retry from being a
    busy loop.
    """

    for _loop, label in SUPERVISED_LOOPS:
        plist = _rendered_plist(label, tmp_path)
        assert plist["RunAtLoad"] is True
        assert plist["KeepAlive"] is True
        assert plist["ThrottleInterval"] >= 30


def test_a_supervised_loop_is_pointed_at_this_checkout(tmp_path: Path) -> None:
    """`--root` and the working directory both have to name the real repository.

    launchd starts a process with no inherited shell, so a placeholder that
    survived rendering would send the loop at a ledger under a path that does
    not exist, and its first act would be to create one.
    """

    for _loop, label in SUPERVISED_LOOPS:
        plist = _rendered_plist(label, tmp_path)
        argv = plist["ProgramArguments"]
        assert plist["WorkingDirectory"] == str(REPO_ROOT)
        assert argv[argv.index("--root") + 1] == str(REPO_ROOT)
        assert "__REPO_ROOT__" not in str(plist)
        assert "__HOME__" not in str(plist)
        assert "__UV_BIN__" not in str(plist)


def test_a_supervised_loop_polls_as_often_as_the_start_script_asks(tmp_path: Path) -> None:
    """One interval per loop, whoever starts it.

    The dispatcher polls faster than the drainer because a milestone is parked
    on the intent it settles. Two spellings of that decision would make which
    one an operator gets depend on how the loop happened to be started.
    """

    script = (REPO_ROOT / "scripts" / "start-agent-runtime.sh").read_text(encoding="utf-8")
    for command, label in (
        ("run_enqueue_drainer", "com.rahul.local-first-agent.enqueue-drainer"),
        ("run_ledger_dispatcher", "com.rahul.local-first-agent.ledger-dispatcher"),
    ):
        match = re.search(rf"{command}\s*\\?\s*\n?\s*--interval-seconds\s+(\d+)", script)
        assert match is not None, f"{command} has no interval in the start script"
        argv = _rendered_plist(label, tmp_path)["ProgramArguments"]
        assert argv[argv.index("--interval-seconds") + 1] == match.group(1)


def test_a_supervised_loop_reaches_the_same_ledger_as_the_start_script(tmp_path: Path) -> None:
    """The database a loop talks to must not depend on who started it.

    launchd inherits no shell, so these URLs are spelled again in the plists
    while the start script spells them as defaults. That duplication is real and
    this is the tripwire on it: a loop supervised against one Postgres while
    every other process uses another is a split-brain whose only symptom is work
    that never moves.
    """

    script = (REPO_ROOT / "scripts" / "start-agent-runtime.sh").read_text(encoding="utf-8")
    expected = {
        variable: re.search(
            rf'^export {variable}="\$\{{{variable}:-([^"}}]+)\}}"', script, re.MULTILINE
        )
        for variable in (
            "LOCAL_AGENT_DATABASE_URL",
            "LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL",
        )
    }
    defaults: dict[str, str] = {}
    for variable, match in expected.items():
        assert match is not None, f"{variable} is no longer a defaulted export"
        defaults[variable] = match.group(1)

    for _loop, label in SUPERVISED_LOOPS:
        environment = _rendered_plist(label, tmp_path)["EnvironmentVariables"]
        assert environment["LOCAL_AGENT_DATABASE_URL"] == defaults["LOCAL_AGENT_DATABASE_URL"]
        assert (
            environment["LOCAL_AGENT_COORDINATION_DATABASE_URL"]
            == defaults["LOCAL_AGENT_DATABASE_URL"]
        )
        assert (
            environment["DBOS_SYSTEM_DATABASE_URL"]
            == defaults["LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL"]
        )
        assert environment["LOCAL_AGENT_USE_DBOS"] == "true"


def test_stopping_the_runtime_boots_out_the_supervised_loops() -> None:
    """A stop that the machine undoes a minute later is not a stop.

    KeepAlive means killing the pid asks launchd to start it again, so the stop
    script has to eject the agent from the domain first. It already does that
    for the other daemons; the loops have to be on the same list.
    """

    script = (REPO_ROOT / "scripts" / "stop-agent-runtime.sh").read_text(encoding="utf-8")
    labels = script.split("LAUNCH_LABELS=(", 1)[1].split(")", 1)[0]
    for _loop, label in SUPERVISED_LOOPS:
        assert label in labels


def test_the_start_script_defers_to_launchd_for_a_supervised_loop() -> None:
    """Two starters for one singleton is a race the pid file cannot describe.

    Both copies would contend for the advisory lock and the loser would exit at
    once, leaving this script's pid file pointing at a dead process while the
    loop is in fact running fine under launchd.
    """

    script = (REPO_ROOT / "scripts" / "start-agent-runtime.sh").read_text(encoding="utf-8")
    assert "resident_loop_launch_label" in script
    for loop, label in SUPERVISED_LOOPS:
        assert loop in script
        assert label in script

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The verification gate runs in the target project's environment.

A verification command is a statement about the target project, so it runs in
the target project's environment and not the control plane's. The supervised
dispatcher exports `LOCAL_AGENT_USE_DBOS=true`; inherited by a gate's `pytest`,
that one variable failed this repository's own suite against a clean diff, and
the evidence blamed the diff. docs/verification_gate_environment_design.md is
the full account; these tests are its Tests section, one for one.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import AliasChoices
from staffing_support import repo_bench

import local_first_agent_os
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    FakeProcessPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
)
from local_first_agent_os.pow_wow.executor import (
    CONTROL_PLANE_ENV_NAMES,
    CONTROL_PLANE_ENV_PREFIXES,
    _headless_agent_environment,
    verification_gate_environment,
)
from local_first_agent_os.pow_wow.process import run_captured_shell_command
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.settings import Settings
from local_first_agent_os.staffing import Harness, JudgmentRole, Tier
from local_first_agent_os.toolchains import project_environment

REPO_ROOT = Path(__file__).resolve().parent.parent

# What a control-plane parent can hold, values included: the ledger-dispatcher
# plist's exports plus the per-child session handshake a spawning process sets.
# The assertions below run against the environment that caused the incident.
# The database URL doubles as the sentinel for "values never reach the record".
_DISPATCHER_ENVIRONMENT = {
    "LOCAL_AGENT_USE_DBOS": "true",
    "LOCAL_AGENT_COORDINATION_BACKEND": "postgres",
    "AGENT_COORDINATION_POOL_MAX_SIZE": "7",
    "DBOS_SYSTEM_DATABASE_URL": (
        "postgresql+psycopg://postgres:sekret@127.0.0.1:5432/local_agent_dbos"
    ),
    "VIRTUAL_ENV": "/control/plane/.venv",
    "AGENT_SESSION_ID": "claude-1",
}

_ENV_DUMP_COMMAND = (
    f'{shlex.quote(sys.executable)} -c "import json, os; print(json.dumps(dict(os.environ)))"'
)


def _export_dispatcher_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _DISPATCHER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    # The canary for "denylist, not allowlist": nothing this control plane owns,
    # so the gate must pass it through untouched.
    monkeypatch.setenv("UNRELATED_TOOLCHAIN_SETTING", "kept")


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("# target\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _target(path: Path) -> LinkedProject:
    return LinkedProject(
        id="ai_business_portfolio",
        kind="business_factory",
        path=path,
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="portfolio repo",
        verification_commands=[_ENV_DUMP_COMMAND],
    )


def _context(target: LinkedProject) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-1",
        goal="Implement the next gated portfolio task",
        directive="/saga Implement the next gated portfolio task",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        verification_commands=tuple(target.verification_commands),
        evidence_project_ids=("ai_business_portfolio_analysis",),
        memory_project_id="ai_stack_local",
    )


def _is_control_plane_name(name: str) -> bool:
    return name.startswith(CONTROL_PLANE_ENV_PREFIXES) or name in CONTROL_PLANE_ENV_NAMES


def test_a_verification_command_sees_no_control_plane_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate subprocess itself is clean, not just the dict we meant to give it."""

    _export_dispatcher_environment(monkeypatch)
    gate_environment, _stripped = verification_gate_environment(tmp_path)

    capture = run_captured_shell_command(
        _ENV_DUMP_COMMAND, tmp_path, timeout_seconds=60, environment=gate_environment
    )

    assert capture.exit_code == 0, capture.stderr
    observed = json.loads(capture.stdout)
    leaked = sorted(name for name in observed if _is_control_plane_name(name))
    assert leaked == [], f"the gate inherited control-plane variables: {leaked}"
    # The strip is not an allowlist by accident: a developer's shell survives.
    assert "HOME" in observed
    assert "PATH" in observed
    assert observed["UNRELATED_TOOLCHAIN_SETTING"] == "kept"


def test_the_agent_environment_is_unchanged_by_the_gate_strip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same dispatcher environment, both spawn paths, opposite answers.

    The agent spawn builds its environment through `project_environment`, and an
    agent *should* inherit the control plane's variables; the gate must not. The
    two assertions live in one test because the defect would be them agreeing.
    """

    _export_dispatcher_environment(monkeypatch)

    agent_environment = project_environment(tmp_path)
    gate_environment, stripped = verification_gate_environment(tmp_path)

    for name, value in _DISPATCHER_ENVIRONMENT.items():
        assert agent_environment[name] == value
        assert name not in gate_environment
        assert name in stripped
    assert "VIRTUAL_ENV" in agent_environment
    assert "VIRTUAL_ENV" not in gate_environment
    assert agent_environment["UNRELATED_TOOLCHAIN_SETTING"] == "kept"
    assert gate_environment["UNRELATED_TOOLCHAIN_SETTING"] == "kept"
    assert list(stripped) == sorted(stripped)


def test_the_gate_and_the_agent_run_in_different_environments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the executor: the agent subprocess keeps what the gate subprocess loses."""

    _export_dispatcher_environment(monkeypatch)
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    task = PowWowTaskSpec(
        task_name="implement_fixture",
        role="implementation_agent",
        description="write fake output and report the environment",
    )

    result = FakeProcessPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        agent_command=[
            sys.executable,
            "-c",
            "import json, os, pathlib; "
            "pathlib.Path('fake_agent_output.txt').write_text('made a change\\n'); "
            "print(json.dumps(dict(os.environ)))",
        ],
    ).dispatch_pow_wow("pow-env-split", target, (task,), _context(target))

    run = next(
        artifact.content
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "external_agent_run"
    )
    assert result.tasks[0].status == "completed"

    agent_env = json.loads(run["command"]["stdout"])
    assert agent_env["LOCAL_AGENT_USE_DBOS"] == "true"
    assert agent_env["VIRTUAL_ENV"] == "/control/plane/.venv"

    gate_env = json.loads(run["verification"][0]["stdout"])
    assert not [name for name in gate_env if _is_control_plane_name(name)]
    assert "HOME" in gate_env
    assert gate_env["UNRELATED_TOOLCHAIN_SETTING"] == "kept"

    stripped = run["verification_environment"]["stripped"]
    assert set(_DISPATCHER_ENVIRONMENT) <= set(stripped)
    assert all(_is_control_plane_name(name) for name in stripped)


def test_a_headless_agent_shell_cannot_own_the_operator_runtime() -> None:
    """Agent login shells must not register or release interactive Pi sessions."""

    assert _headless_agent_environment({})["LOCAL_AGENT_TERMINAL_SESSION_STARTED"] == "1"
    assert (
        _headless_agent_environment({"LOCAL_AGENT_TERMINAL_SESSION_STARTED": "0"})[
            "LOCAL_AGENT_TERMINAL_SESSION_STARTED"
        ]
        == "1"
    )


def test_the_run_record_names_every_stripped_variable_and_no_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`cli_agent_run.v1` says what was removed, by name, and never by value.

    An operator reading the record when a gate disagrees with a developer's
    shell needs the names; the values include database URLs.
    """

    _export_dispatcher_environment(monkeypatch)
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)

    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if 'login' in sys.argv:\n"
        "    print('logged in')\n"
        "    raise SystemExit(0)\n"
        "from pathlib import Path\n"
        "Path('fake_agent_output.txt').write_text('made a change\\n')\n"
        "print(json.dumps({'type': 'result', 'result': 'wrote fake_agent_output.txt'}))\n",
        encoding="utf-8",
    )
    os.chmod(agent, 0o755)
    # The bench is passed rather than defaulted: the executor's default is
    # `DEFAULT_BENCH`, which need not agree with the staffing the capability gate
    # reads, and the fake below is bound to a seat. Production wires the same
    # `load_bench` result in through `dispatcher_runner`.
    bench = repo_bench()
    senior_vendor = bench[Tier.SENIOR].harness

    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        bench=bench,
        claude_bin=str(agent) if senior_vendor is Harness.CLAUDE else Harness.CLAUDE.value,
        codex_bin=str(agent) if senior_vendor is Harness.CODEX else Harness.CODEX.value,
        verification_timeout_seconds=60,
        timeout_seconds=600,
    ).dispatch_pow_wow(
        "pow-record-names",
        target,
        (
            PowWowTaskSpec(
                task_name="implement_under_dispatcher_env",
                role="implementer",
                judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
                dispatch_kind="code",
                description="Create fake_agent_output.txt.",
            ),
        ),
        _context(target),
    )

    run = next(
        artifact.content
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "cli_agent_run"
    )
    assert run["schema_version"] == "cli_agent_run.v1"
    assert run["verification"], "the gate must have run for the record to mean anything"

    record = run["verification_environment"]
    stripped = record["stripped"]
    assert set(_DISPATCHER_ENVIRONMENT) <= set(stripped)
    assert all(_is_control_plane_name(name) for name in stripped)
    assert list(stripped) == sorted(stripped)

    # Names only, never values: nothing the dispatcher exported may appear, and
    # neither may any value the gate's own subprocess could have echoed back.
    for value in _DISPATCHER_ENVIRONMENT.values():
        assert value not in json.dumps(record)
    gate_env = json.loads(run["verification"][0]["stdout"])
    assert not [name for name in gate_env if _is_control_plane_name(name)]


# Variables this repository reads that are deliberately not control-plane
# settings: they belong to other systems, so the gate must pass them through
# and the prefix rule does not claim them. A new entry here is a conscious
# decision that the variable is somebody else's; a new *setting* belongs under
# one of the three prefixes instead.
_FOREIGN_ENVIRONMENT_READS = {
    "PATH": "the shell's, consulted for toolchain lookup",
    "NVM_DIR": "nvm's own variable, read to honor a target project's .nvmrc",
    "CLAUDE_BIN": "operator override for an external harness binary",
    "CODEX_BIN": "operator override for an external harness binary",
    "HERMES_BASE_URL": "external adapter endpoint",
    "HERMES_API_KEY": "external adapter credential",
    "OPENCODE_BASE_URL": "external adapter endpoint",
    "OPENCODE_API_KEY": "external adapter credential",
    "WF_API_KEY": "Workflowy's credential, named by Workflowy's convention",
}

_ENV_READ_PATTERNS = (
    re.compile(r'os\.environ\.get\(\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"'),
    re.compile(r'os\.environ\[\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"\s*\]'),
    re.compile(r'os\.getenv\(\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"'),
    re.compile(r'os\.environ\.setdefault\(\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"'),
)
# The repo's indirection convention: NAME_ENV / NAME_ENV_VAR constants holding
# the variable's name, read through the constant at the call site.
_ENV_NAME_CONSTANT = re.compile(
    r'^[A-Z][A-Z0-9_]*_ENV(?:_VAR)?(?::\s*str)?\s*=\s*"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"',
    re.MULTILINE,
)


def _environment_names_read_by_source() -> dict[str, set[str]]:
    src_root = Path(local_first_agent_os.__file__).resolve().parent
    names: dict[str, set[str]] = {}
    for source_file in sorted(src_root.rglob("*.py")):
        text = source_file.read_text(encoding="utf-8")
        for pattern in (*_ENV_READ_PATTERNS, _ENV_NAME_CONSTANT):
            for match in pattern.finditer(text):
                names.setdefault(match.group("name"), set()).add(source_file.name)
    return names


def test_every_environment_read_is_prefixed_or_consciously_foreign() -> None:
    """What keeps the prefix rule true as settings are added.

    The denylist strips three prefixes and one name. That is only sound while
    every setting this control plane reads lives under those prefixes, so this
    walks every read in the source tree - direct, through a `*_ENV` constant,
    or through a `Settings` alias - and requires each name to be a control-plane
    prefix, the by-name list, or a documented foreign variable.
    """

    names = _environment_names_read_by_source()

    assert Settings.model_config.get("env_prefix") == "LOCAL_AGENT_"
    for field_name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if alias is None:
            continue
        choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
        for choice in choices:
            assert isinstance(choice, str), f"{field_name} uses a non-string alias"
            names.setdefault(choice, set()).add(f"Settings.{field_name}")

    unclassified = {
        name: sorted(files)
        for name, files in names.items()
        if not _is_control_plane_name(name) and name not in _FOREIGN_ENVIRONMENT_READS
    }
    assert unclassified == {}, (
        "new environment reads must use a control-plane prefix or be classified "
        f"as foreign in this test: {unclassified}"
    )


def test_every_variable_the_launchd_plists_set_is_covered_by_the_strip() -> None:
    """The dispatcher's exports are the leak this design exists to stop.

    The read-side scan above keeps new settings under the prefixes; this is the
    set side: whatever a launchd service exports must be `PATH` or something
    `verification_gate_environment` removes, or a gate child inherits it.
    """

    plists = sorted((REPO_ROOT / "scripts" / "launchd").glob("*.plist"))
    assert plists, "the launchd services this test audits have moved"
    for plist_path in plists:
        exported = plistlib.loads(plist_path.read_bytes()).get("EnvironmentVariables") or {}
        uncovered = sorted(
            name for name in exported if name != "PATH" and not _is_control_plane_name(name)
        )
        assert uncovered == [], (
            f"{plist_path.name} exports {uncovered}, which the verification gate "
            "would pass through to a target project's suite"
        )


def test_the_suite_passes_as_a_verification_command_under_an_environment_that_fails_it_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one that would have caught the incident.

    Same command, same checkout, same inherited `LOCAL_AGENT_USE_DBOS=true`:
    run the way the gate used to run it, the suite fails on the runtime flag;
    run in the gate environment, it passes. The unit tests above prove the
    strip; this proves the strip is the one the incident needed.
    """

    monkeypatch.setenv("LOCAL_AGENT_USE_DBOS", "true")
    selection = "tests/test_work_unit_lifecycle.py::test_all_seven_phases_occur_in_the_fixed_order"
    command = f"uv run pytest {selection} -q"

    direct = run_captured_shell_command(command, REPO_ROOT, timeout_seconds=300)
    assert direct.exit_code != 0, (
        "the reproduction has drifted: the dispatcher environment no longer "
        f"fails the suite directly\n{direct.stdout}{direct.stderr}"
    )
    assert "DBOS" in direct.stdout, (
        f"the direct run failed for the wrong reason\n{direct.stdout}{direct.stderr}"
    )

    gate_environment, stripped = verification_gate_environment(REPO_ROOT)
    assert "LOCAL_AGENT_USE_DBOS" in stripped
    gated = run_captured_shell_command(
        command, REPO_ROOT, timeout_seconds=300, environment=gate_environment
    )
    assert gated.exit_code == 0, (
        f"the gate environment still fails the suite\n{gated.stdout}{gated.stderr}"
    )
    assert "1 passed" in gated.stdout, (
        f"exit 0 without the test running proves nothing\n{gated.stdout}{gated.stderr}"
    )

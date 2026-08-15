# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Approval-time target project scaffolding.

The scaffold is deterministic coordination work, not an implementation agent
task.  It creates a real git repository with a Python baseline, pins the Node
toolchain used by downstream executors, projects verification commands into a
GitHub Actions workflow, and registers the target with Project Center.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .settings import Settings

DEFAULT_PYTHON_VERSION = "3.13"
DEFAULT_NODE_VERSION = "22.19.0"
DEFAULT_VERIFICATION_COMMANDS = (
    "uv run pytest",
    "uv run ruff check",
    "uv run pyright",
)
_PROJECT_ID = re.compile(r"[a-z][a-z0-9_]*")
_NODE_VERSION = re.compile(r"(?:v)?(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class TargetProjectScaffold:
    project_id: str
    path: str
    kind: str = "python_application"
    python_version: str = DEFAULT_PYTHON_VERSION
    node_version: str = DEFAULT_NODE_VERSION
    verification_commands: tuple[str, ...] = DEFAULT_VERIFICATION_COMMANDS
    schema_version: str = "target_project_scaffold.v1"

    def __post_init__(self) -> None:
        if _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ValueError(
                "Scaffold project id must start with a lowercase letter and contain "
                "only lowercase letters, digits, and underscores."
            )
        if _NODE_VERSION.fullmatch(self.node_version) is None:
            raise ValueError("Scaffold node_version must be an exact version such as 22.19.0.")

    @property
    def expanded_path(self) -> Path:
        return Path(self.path).expanduser().resolve()

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verification_commands"] = list(self.verification_commands)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> TargetProjectScaffold:
        if not isinstance(payload, dict):
            raise ValueError("target_project_scaffold must be an object.")
        if payload.get("schema_version") != "target_project_scaffold.v1":
            raise ValueError("Unsupported target project scaffold schema version.")
        commands = payload.get("verification_commands")
        if not isinstance(commands, list) or not all(
            isinstance(command, str) and command.strip() for command in commands
        ):
            raise ValueError("Scaffold verification_commands must be non-empty strings.")
        return cls(
            project_id=str(payload.get("project_id") or ""),
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "python_application"),
            python_version=str(payload.get("python_version") or DEFAULT_PYTHON_VERSION),
            node_version=str(payload.get("node_version") or DEFAULT_NODE_VERSION),
            verification_commands=tuple(commands),
        )


def scaffold_spec(settings: Settings, project_id: str) -> TargetProjectScaffold:
    return TargetProjectScaffold(
        project_id=project_id,
        path=str((settings.projects_root / project_id).expanduser().resolve()),
    )


def scaffold_target_project(
    spec: TargetProjectScaffold,
    *,
    settings: Settings,
    finalized_gawd_path: Path,
) -> dict[str, Any]:
    """Create and register a target repo, safely replaying a completed scaffold."""

    target = spec.expanded_path
    manifest_path = target / ".local-agent-scaffold.json"
    if target.exists():
        if not manifest_path.exists():
            raise FileExistsError(
                f"Scaffold target already exists without a scaffold manifest: {target}"
            )
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        if persisted != spec.to_payload():
            raise ValueError(f"Existing scaffold contract does not match: {target}")
        _register_linked_project(settings.linked_projects_path, spec)
        return _scaffold_result(spec, replayed=True)

    if not finalized_gawd_path.is_file():
        raise FileNotFoundError(f"Finalized GAWD doc not found: {finalized_gawd_path}")
    _require_node_version(spec.node_version)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{spec.project_id}-scaffold-", dir=target.parent))
    try:
        _run_checked_scaffold_command(["git", "init", "-b", "main"], cwd=staging)
        _run_checked_scaffold_command(
            [
                "uv",
                "init",
                "--app",
                "--name",
                spec.project_id.replace("_", "-"),
                "--python",
                spec.python_version,
                "--no-workspace",
            ],
            cwd=staging,
        )
        _run_checked_scaffold_command(
            ["uv", "add", "--dev", "pytest", "ruff", "pyright"], cwd=staging
        )
        _write_scaffold_files(staging, spec, finalized_gawd_path)
        for command in spec.verification_commands:
            _run_checked_scaffold_command(["/bin/zsh", "-lc", command], cwd=staging)
        _run_checked_scaffold_command(["git", "add", "-A"], cwd=staging)
        _run_checked_scaffold_command(
            ["git", "commit", "-m", f"Initialize {spec.project_id} project"], cwd=staging
        )
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _register_linked_project(settings.linked_projects_path, spec)
    return _scaffold_result(spec, replayed=False)


def _write_scaffold_files(
    root: Path,
    spec: TargetProjectScaffold,
    finalized_gawd_path: Path,
) -> None:
    (root / ".python-version").write_text(f"{spec.python_version}\n", encoding="utf-8")
    (root / ".nvmrc").write_text(f"{spec.node_version}\n", encoding="utf-8")
    (root / ".node-version").write_text(f"{spec.node_version}\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": spec.project_id.replace("_", "-"),
                "private": True,
                "engines": {"node": f">={spec.node_version} <23"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_scaffold.py").write_text(
        "from pathlib import Path\n\n\ndef test_project_scaffold() -> None:\n"
        "    assert Path('pyproject.toml').is_file()\n",
        encoding="utf-8",
    )
    design_dir = root / "docs" / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(finalized_gawd_path, design_dir / "gawd_doc.md")
    workflow = root / ".github" / "workflows"
    workflow.mkdir(parents=True, exist_ok=True)
    (workflow / "ci.yml").write_text(_ci_workflow(spec), encoding="utf-8")
    (root / ".local-agent-scaffold.json").write_text(
        json.dumps(spec.to_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _ci_workflow(spec: TargetProjectScaffold) -> str:
    verification = "\n".join(
        f"      - run: {json.dumps(command)}" for command in spec.verification_commands
    )
    return (
        "name: CI\n\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n\n"
        "jobs:\n"
        "  verify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version-file: .python-version\n"
        "      - uses: actions/setup-node@v4\n"
        "        with:\n"
        "          node-version-file: .nvmrc\n"
        "      - uses: astral-sh/setup-uv@v6\n"
        "      - run: uv sync --locked\n"
        f"{verification}\n"
    )


def _register_linked_project(registry_path: Path, spec: TargetProjectScaffold) -> None:
    registry_path = registry_path.expanduser().resolve()
    payload = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise ValueError("linked_projects.toml has no projects list.")
    existing = next(
        (project for project in projects if project.get("id") == spec.project_id),
        None,
    )
    if existing is not None:
        if Path(str(existing.get("path") or "")).expanduser().resolve() != spec.expanded_path:
            raise ValueError(f"Linked project id already points elsewhere: {spec.project_id}")
        return
    commands = ", ".join(json.dumps(command) for command in spec.verification_commands)
    block = (
        "\n[[projects]]\n"
        f"id = {json.dumps(spec.project_id)}\n"
        f"kind = {json.dumps(spec.kind)}\n"
        f"path = {json.dumps(str(spec.expanded_path))}\n"
        'status = "active_product_repo"\n'
        "read_only = false\n"
        f"description = {json.dumps('Scaffolded target project ' + spec.project_id + '.')}\n"
        'primary_interfaces = ["uv run pytest"]\n'
        'owns = ["project implementation"]\n'
        'avoid = ["control-plane coordination internals"]\n'
        f"verification_commands = [{commands}]\n"
    )
    with registry_path.open("a", encoding="utf-8") as handle:
        handle.write(block)


def _require_node_version(version: str) -> None:
    if _node_version_available(version):
        return
    nvm_script = Path.home() / ".nvm" / "nvm.sh"
    if nvm_script.is_file():
        completed = subprocess.run(
            [
                "/bin/zsh",
                "-lc",
                f"source {nvm_script} && nvm install {version}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if completed.returncode == 0 and _node_version_available(version):
            return
        output = (completed.stdout + completed.stderr).strip()
        raise RuntimeError(f"Unable to install Node {version} with nvm: {output}")
    raise RuntimeError(
        f"Node {version} is required for scaffolding. Install nvm and Node {version}, "
        "then retry approval."
    )


def _node_version_available(version: str) -> bool:
    candidates = [
        shutil.which("node"),
        str(Path.home() / ".nvm" / "versions" / "node" / f"v{version}" / "bin" / "node"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        completed = subprocess.run(
            [candidate, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip().removeprefix("v") == version:
            return True
    return False


def _run_checked_scaffold_command(command: list[str], *, cwd: Path) -> None:
    timeout_seconds = 30 if command[0] == "git" else 900
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{command[0]} exceeded its {timeout_seconds}s scaffold timeout"
        ) from exc
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).strip()
        raise RuntimeError(f"{command[0]} exited {completed.returncode}: {output}")


def _scaffold_result(spec: TargetProjectScaffold, *, replayed: bool) -> dict[str, Any]:
    return {
        "schema_version": "target_project_scaffold_result.v1",
        "project_id": spec.project_id,
        "path": str(spec.expanded_path),
        "python_version": spec.python_version,
        "node_version": spec.node_version,
        "verification_commands": list(spec.verification_commands),
        "replayed": replayed,
    }

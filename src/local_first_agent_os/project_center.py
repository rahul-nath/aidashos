# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from .browser_acceptance import DEFAULT_RESPONSIVE_VIEWPORTS, BrowserViewport
from .project_access import ProjectAccessPolicy, access_policy_from_record
from .settings import Settings, get_settings

if TYPE_CHECKING:
    from .pow_wow.protocol import ReferencePack


@dataclass(frozen=True)
class BrowserAcceptanceProfile:
    preview_command: str
    target_url_template: str = "http://127.0.0.1:{port}"
    readiness_path: str = "/"
    required_paths: tuple[str, ...] = ("/",)
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    viewports: tuple[BrowserViewport, ...] = DEFAULT_RESPONSIVE_VIEWPORTS
    required_selectors: tuple[str, ...] = ()
    bounded_selectors: tuple[str, ...] = ()
    startup_timeout_seconds: float = 60.0
    capture_timeout_seconds: int = 60


@dataclass(frozen=True)
class LinkedProject:
    id: str
    kind: str
    path: Path
    status: str
    access: ProjectAccessPolicy
    description: str
    primary_interfaces: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    integrated_branch: str = "main"
    """The branch the refinery fast-forwards, and the branch its stacks start from.

    Optional with a default rather than required, because every linked project's
    trunk is already `main`, so requiring it would mean editing every entry to
    state what is already true. A project whose trunk is not `main` is exactly
    the project whose owner will notice a field named this.

    Distinct from the `branch` in `project_status_row`, which is whatever the
    checkout happens to have out right now. This one is a declaration and that
    one is an observation, and the refinery refuses rather than reconciling them:
    a checkout on some other branch is a fact about the operator's machine, not a
    statement about anyone's diff.
    """
    reference_packs: list[ReferencePack] = field(default_factory=list)
    browser_acceptance: BrowserAcceptanceProfile | None = None

    @property
    def expanded_path(self) -> Path:
        return self.path.expanduser()

    @property
    def read_only(self) -> bool:
        """Derived from the access mode, so the two cannot disagree.

        Kept as a property because `dispatcher_runner` and `decomposition` ask
        this exact question; they now read a mode through a familiar name rather
        than a field that could drift from the policy beside it.
        """

        return self.access.read_only

    @property
    def owns(self) -> list[str]:
        return list(self.access.owns)

    @property
    def avoid(self) -> list[str]:
        return list(self.access.avoid)


@dataclass(frozen=True)
class ProjectCenter:
    id: str
    description: str
    control_plane_project: str
    default_saga_project: str
    default_memory_project: str
    projects: tuple[LinkedProject, ...]

    def project_by_id(self, project_id: str) -> LinkedProject:
        for project in self.projects:
            if project.id == project_id:
                return project
        raise KeyError(f"Unknown linked project: {project_id}")

    def default_saga_target(self) -> LinkedProject:
        return self.project_by_id(self.default_saga_project)

    def status_rows(self, *, include_git: bool = True) -> list[dict[str, Any]]:
        return [project_status_row(project, include_git=include_git) for project in self.projects]

    def as_dict(self, *, include_git: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "control_plane_project": self.control_plane_project,
            "default_saga_project": self.default_saga_project,
            "default_memory_project": self.default_memory_project,
            "projects": self.status_rows(include_git=include_git),
        }


class BrowserAcceptanceView(BaseModel):
    """The browser-acceptance profile as the project row publishes it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    preview_command: str
    required_paths: list[str]
    viewports: list[BrowserViewport]


class ProjectStatusRow(BaseModel):
    """One registered project, with its git state when the path is a repository.

    The git fields are absent from the underlying row when git was not consulted,
    the path does not exist, or reading it failed. They are modelled as nullable
    rather than optional so the published contract is "always present, sometimes
    null", which is what a client actually receives once the model serializes.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    id: str
    kind: str
    path: str
    exists: bool
    status: str
    read_only: bool
    description: str
    primary_interfaces: list[str]
    owns: list[str]
    avoid: list[str]
    verification_commands: list[str]
    integrated_branch: str
    reference_packs: list[str]
    browser_acceptance: BrowserAcceptanceView | None = None
    git_repo: bool
    git_error: str | None = None
    git_dirty: bool | None = None
    git_dirty_entries: int | None = None
    branch: str | None = None
    head_sha: str | None = None


class ProjectCenterView(BaseModel):
    """The project registry, as the cockpit's project picker reads it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    id: str
    description: str
    control_plane_project: str
    default_saga_project: str
    default_memory_project: str
    projects: list[ProjectStatusRow]


def _string_list(raw: object, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field_name} must be a list of strings.")
    return list(raw)


def _string(raw: object, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return raw.strip()


def _browser_acceptance_profile(raw: object) -> BrowserAcceptanceProfile | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("browser_acceptance must be a table")
    allowed_keys = {
        "preview_command",
        "target_url_template",
        "readiness_path",
        "required_paths",
        "allowed_hosts",
        "viewports",
        "required_selectors",
        "bounded_selectors",
        "startup_timeout_seconds",
        "capture_timeout_seconds",
    }
    unknown = sorted(set(raw) - allowed_keys)
    if unknown:
        raise ValueError(f"browser_acceptance has unknown keys: {', '.join(unknown)}")
    command = _string(raw.get("preview_command"), "browser_acceptance.preview_command")
    target_url_template = str(raw.get("target_url_template") or "http://127.0.0.1:{port}")
    if "{port}" not in target_url_template or not target_url_template.startswith(
        ("http://127.0.0.1:", "http://localhost:")
    ):
        raise ValueError(
            "browser_acceptance.target_url_template must be a local HTTP URL with {port}"
        )
    raw_viewports = raw.get("viewports")
    if raw_viewports is None:
        viewports = DEFAULT_RESPONSIVE_VIEWPORTS
    elif not isinstance(raw_viewports, list):
        raise ValueError("browser_acceptance.viewports must be an array of tables")
    else:
        viewports = tuple(BrowserViewport.model_validate(value) for value in raw_viewports)
    profile = BrowserAcceptanceProfile(
        preview_command=command,
        target_url_template=target_url_template,
        readiness_path=str(raw.get("readiness_path") or "/"),
        required_paths=tuple(_string_list(raw.get("required_paths") or ["/"], "required_paths")),
        allowed_hosts=tuple(
            _string_list(
                raw.get("allowed_hosts") or ["127.0.0.1", "localhost"],
                "allowed_hosts",
            )
        ),
        viewports=viewports,
        required_selectors=tuple(_string_list(raw.get("required_selectors"), "required_selectors")),
        bounded_selectors=tuple(_string_list(raw.get("bounded_selectors"), "bounded_selectors")),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds") or 60),
        capture_timeout_seconds=int(raw.get("capture_timeout_seconds") or 60),
    )
    if not profile.readiness_path.startswith("/"):
        raise ValueError("browser_acceptance.readiness_path must start with /")
    return profile


def _parse_linked_project_record(raw: dict[str, Any]) -> LinkedProject:
    """One registry entry, refusing the combination that cannot be verified.

    A project that may take code work must declare how that work is checked. The
    executor runs exactly what this list names, so an empty list on a writable
    project means every code run is certified by zero commands - which is the
    shape ``pow_wow.verification`` exists to stop at runtime, caught here at the
    cheaper moment instead.

    Read-only projects are exempt because they are refused for code intents
    before an executor is ever reached, so there is nothing for them to verify.
    """

    from .pow_wow.protocol import ReferencePack

    project_id = _string(raw.get("id"), "project.id")
    read_only = bool(raw.get("read_only", False))
    verification_commands = _string_list(
        raw.get("verification_commands"),
        "verification_commands",
    )
    if not read_only and not verification_commands:
        raise ValueError(
            f"Linked project {project_id!r} is writable and declares no "
            "verification_commands, so any code run against it would be certified by "
            "nothing. Declare at least one verification command, or set "
            "read_only = true if it must not take code work."
        )

    return LinkedProject(
        id=project_id,
        kind=_string(raw.get("kind"), "project.kind"),
        path=Path(_string(raw.get("path"), "project.path")),
        status=_string(raw.get("status"), "project.status"),
        access=access_policy_from_record(
            read_only=read_only,
            owns=_string_list(raw.get("owns"), "owns"),
            avoid=_string_list(raw.get("avoid"), "avoid"),
        ),
        description=_string(raw.get("description"), "project.description"),
        primary_interfaces=_string_list(raw.get("primary_interfaces"), "primary_interfaces"),
        verification_commands=verification_commands,
        integrated_branch=_string(raw.get("integrated_branch"), "integrated_branch")
        if raw.get("integrated_branch") is not None
        else "main",
        reference_packs=[
            ReferencePack(value)
            for value in _string_list(raw.get("reference_packs"), "reference_packs")
        ],
        browser_acceptance=_browser_acceptance_profile(raw.get("browser_acceptance")),
    )


def load_project_center(settings: Settings | None = None) -> ProjectCenter:
    settings = settings or get_settings()
    payload = settings.load_toml(settings.linked_projects_path)
    if not payload:
        raise FileNotFoundError(
            f"Linked project registry not found: {settings.linked_projects_path}"
        )

    center = payload.get("center") or {}
    raw_projects = payload.get("projects") or []
    if not isinstance(raw_projects, list) or not raw_projects:
        raise ValueError("linked_projects.toml must define at least one [[projects]] entry.")

    projects = tuple(
        _parse_linked_project_record(item) for item in raw_projects if isinstance(item, dict)
    )
    project_ids = {project.id for project in projects}
    for required in (
        center.get("control_plane_project"),
        center.get("default_saga_project"),
        center.get("default_memory_project"),
    ):
        if required not in project_ids:
            raise ValueError(f"Center references unknown project id: {required!r}")

    return ProjectCenter(
        id=_string(center.get("id"), "center.id"),
        description=_string(center.get("description"), "center.description"),
        control_plane_project=_string(
            center.get("control_plane_project"),
            "center.control_plane_project",
        ),
        default_saga_project=_string(
            center.get("default_saga_project"),
            "center.default_saga_project",
        ),
        default_memory_project=_string(
            center.get("default_memory_project"),
            "center.default_memory_project",
        ),
        projects=projects,
    )


def project_status_row(project: LinkedProject, *, include_git: bool = True) -> dict[str, Any]:
    path = project.expanded_path
    exists = path.exists()
    git_root = path / ".git"
    row: dict[str, Any] = {
        "id": project.id,
        "kind": project.kind,
        "path": str(path),
        "exists": exists,
        "status": project.status,
        "read_only": project.read_only,
        "description": project.description,
        "primary_interfaces": project.primary_interfaces,
        "owns": project.owns,
        "avoid": project.avoid,
        "verification_commands": project.verification_commands,
        "integrated_branch": project.integrated_branch,
        "reference_packs": [pack.value for pack in project.reference_packs],
        "browser_acceptance": (
            {
                "preview_command": project.browser_acceptance.preview_command,
                "required_paths": list(project.browser_acceptance.required_paths),
                "viewports": [
                    viewport.model_dump(mode="json")
                    for viewport in project.browser_acceptance.viewports
                ],
            }
            if project.browser_acceptance is not None
            else None
        ),
        "git_repo": git_root.exists(),
    }
    if not include_git or not exists or not git_root.exists():
        return row

    try:
        status = _git_lines(path, "status", "--short")
        branch = _git_text(path, "branch", "--show-current")
        head_sha = _git_text(path, "rev-parse", "HEAD")
    except RuntimeError as exc:
        row["git_error"] = str(exc)
        return row
    row["git_dirty"] = bool(status)
    row["git_dirty_entries"] = len(status)
    row["branch"] = branch or None
    row["head_sha"] = head_sha or None
    return row


def _git_run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _git_lines(path: Path, *args: str) -> list[str]:
    return [line for line in _git_run(path, *args).stdout.splitlines() if line.strip()]


def _git_text(path: Path, *args: str) -> str:
    return _git_run(path, *args).stdout.strip()

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
import uvicorn
from rich.console import Console

from .contracts import IngressEvent, SourceType, WorkflowType, WorkspaceId
from .ingress import normalize_file_event, normalize_prompt_event, normalize_scheduled_event
from .local_timer import is_timer_directive, run_timer_directive
from .model_registry import ModelRegistry
from .project_center import load_project_center
from .settings import get_settings

app = typer.Typer(help="Local-first durable agent orchestration CLI")
console = Console()


def _route_logs_to_stderr() -> None:
    from .runtime import get_runtime

    # Build the runtime first: it configures observability, and this function
    # exists to override that configuration's stdout handler afterwards.
    get_runtime()
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = sys.stderr


def _run_workflow_managed(workflow_type: WorkflowType, event: IngressEvent) -> dict[str, Any]:
    if get_settings().use_dbos:
        logging.getLogger("dbos").setLevel(logging.WARNING)
        from .dbos_app import run_workflow_durably

        return run_workflow_durably(workflow_type, event)
    from .workflow import run_workflow

    return run_workflow(workflow_type, event).model_dump(mode="json")


@app.command()
def init_db() -> None:
    from .runtime import get_runtime

    get_runtime()
    console.print("[green]Initialized DB schema and workspace policies.[/green]")


@app.command("models-help")
def models_help() -> None:
    """Print available models, their aliases, and how to load them.

    start-agent-runtime.sh pre-loads the junior tier (general, the
    first dependency of every finalization pow-wow). Every other role loads
    explicitly via `pi /start /<role-or-alias>` to control memory. The
    compactor is the one exception: it auto-loads when context exceeds the
    compaction threshold during a query.
    """
    from .directives import DEFAULT_ALIASES

    registry = ModelRegistry(get_settings())
    aliases_by_role: dict[str, list[str]] = {}
    for alias, role_value in DEFAULT_ALIASES.items():
        if alias != f"/{role_value}":
            aliases_by_role.setdefault(role_value, []).append(alias)
    rows: list[tuple[str, str, str, str]] = []
    for role, spec in sorted(registry.models.items(), key=lambda kv: kv[1].priority):
        load_cmd = f"pi /start /{role.value}"
        notes = []
        alias_list = aliases_by_role.get(role.value)
        if alias_list:
            notes.append("aliases: " + ", ".join(sorted(alias_list)))
        if role.value == "general":
            notes.append("pre-loaded by start-agent-runtime.sh (junior tier)")
        if role.value == "compactor":
            notes.append("auto-loads at context threshold")
        rows.append((role.value, spec.server_model_name, load_cmd, "; ".join(notes)))

    console.print()
    console.print(
        "[bold]Available models[/bold] "
        "(junior tier pre-loaded at runtime start; the rest load explicitly):"
    )
    console.print()
    role_w = max(len(r[0]) for r in rows)
    name_w = max(len(r[1]) for r in rows)
    cmd_w = max(len(r[2]) for r in rows)
    for role, name, cmd, note in rows:
        suffix = f"  [dim]# {note}[/dim]" if note else ""
        console.print(
            f"  [cyan]{role:<{role_w}}[/cyan]  "
            f"{name:<{name_w}}  "
            f"[green]{cmd:<{cmd_w}}[/green]{suffix}"
        )
    console.print()
    console.print(
        "Stop a model: [green]pi /stop /<role>[/green]   Stop all: [green]pi /stop[/green]"
    )


@app.command("timeout-policy")
def timeout_policy(
    operation: str | None = typer.Argument(
        None,
        help="Operation kind to infer; omit to list the complete policy.",
    ),
    expected_seconds: float | None = typer.Option(
        None,
        "--expected-seconds",
        help="Expected happy-path duration for application/model operations.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Inspect the timeout/retry budget before adding a blocking operation."""

    from .timeout_policy import infer_timeout_budget, timeout_policy_payload

    payload = (
        infer_timeout_budget(operation, expected_seconds=expected_seconds).to_payload()
        if operation
        else timeout_policy_payload()
    )
    if json_output:
        console.print_json(data=payload)
        return
    console.print_json(data=payload)


@app.command("projects")
def projects(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    no_git: bool = typer.Option(False, "--no-git", help="Skip git dirty-state checks."),
) -> None:
    """Show the linked local AI project map.

    This is the explicit center-of-gravity map for the sibling repos: what each
    repo owns, which one is safe to edit, and which interfaces are authoritative.
    """
    center = load_project_center(get_settings())
    payload = center.as_dict(include_git=not no_git)
    if json_output:
        console.print_json(data=payload)
        return

    console.print(f"[bold]{center.id}[/bold]")
    console.print(center.description)
    console.print()
    console.print(
        f"control-plane=[cyan]{center.control_plane_project}[/cyan]  "
        f"default-saga=[cyan]{center.default_saga_project}[/cyan]  "
        f"default-memory=[cyan]{center.default_memory_project}[/cyan]"
    )
    console.print()

    for project in payload["projects"]:
        exists = "exists" if project["exists"] else "missing"
        dirty = ""
        if project.get("git_repo") and "git_dirty" in project:
            dirty = (
                f", dirty={project.get('git_dirty_entries', 0)}"
                if project.get("git_dirty")
                else ", clean"
            )
        access = "read-only" if project["read_only"] else "editable"
        console.print(
            f"[bold]{project['id']}[/bold] "
            f"[dim]({project['kind']}, {project['status']}, {access}, {exists}{dirty})[/dim]"
        )
        console.print(f"  path: {project['path']}")
        console.print(f"  role: {project['description']}")
        if project["primary_interfaces"]:
            console.print(f"  interfaces: {', '.join(project['primary_interfaces'])}")
        if project["owns"]:
            console.print(f"  owns: {', '.join(project['owns'])}")
        if project["avoid"]:
            console.print(f"  avoid: {', '.join(project['avoid'])}")
        console.print()


@app.command("project-status")
def project_status(
    project_id: str = typer.Argument(help="Linked project id, such as pest_site_factory."),
    json_output: bool = typer.Option(False, "--json", help="Print the complete snapshot."),
) -> None:
    """Show the one authoritative next action for a linked project."""

    from .project_action import build_project_action_snapshot

    try:
        snapshot = build_project_action_snapshot(project_id, settings=get_settings())
    except Exception as exc:
        console.print(f"[red]Project action unavailable:[/red] {exc}")
        raise typer.Exit(3) from exc
    payload = snapshot.model_dump(mode="json")
    if json_output:
        console.print_json(data=payload)
        return
    console.print(f"[bold]{project_id}[/bold]  [cyan]{snapshot.action.value}[/cyan]")
    console.print(snapshot.summary)
    if snapshot.milestone:
        console.print(f"Milestone: {snapshot.milestone.name or snapshot.milestone.milestone_id}")
    if snapshot.next_command:
        console.print(f"Next: [green]{snapshot.next_command}[/green]")
    for warning in snapshot.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command("session-daemon")
def session_daemon() -> None:
    """Run the local shell-session context daemon."""
    from .session_memory import run_session_daemon

    run_session_daemon()


@app.command("lifecycle-maintenance")
def lifecycle_maintenance(
    quiet: bool = typer.Option(False, "--quiet", help="Only update the latest status file."),
) -> None:
    """Bound local logs, terminalize expired leases, and apply retention.

    Retention deletes audit evidence older than
    ``LOCAL_AGENT_LIFECYCLE_RETENTION_SECONDS`` that nothing still references.
    It never resumes work, and never touches durable project evidence.
    """

    from .lifecycle_maintenance import run_lifecycle_maintenance

    report = run_lifecycle_maintenance(get_settings())
    if not quiet:
        console.print_json(data=report)


@app.command("monitor-feedback")
def monitor_feedback(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Decide and report without submitting intents or recording rows.",
    ),
) -> None:
    """Run one feedback reactor cycle: ledger facts to proposed diagnosis work.

    Proposes only. Every proposal is a PENDING advisory intent that the
    existing dispatcher and approval gates handle unchanged. An invalid rule
    catalog exits non-zero rather than running against a catalog it misread.
    """

    from .coordination.monitor_feedback import CoordinationReactorLedger
    from .coordination.store import now as ledger_now
    from .monitor_feedback import load_feedback_rules, run_feedback_cycle
    from .monitor_feedback.reactor import DryRunReactorLedger
    from .monitor_feedback.rules import FeedbackRuleError

    settings = get_settings()
    try:
        catalog = load_feedback_rules(settings.config_dir / "feedback_rules.toml")
    except FeedbackRuleError as exc:
        console.print(f"[red]Invalid feedback rule catalog:[/red] {exc}")
        raise typer.Exit(2) from exc

    ledger = CoordinationReactorLedger()
    report = run_feedback_cycle(
        DryRunReactorLedger(ledger) if dry_run else ledger,
        catalog,
        now=ledger_now(),
    )
    console.print_json(data=report)


@app.command("pi-daemon")
def pi_daemon() -> None:
    """Run the resident local Pi orchestrator daemon."""
    from .pi_daemon import run_pi_daemon

    run_pi_daemon()


@app.command("session-flush")
def session_flush(session_id: str | None = None) -> None:
    """Flush hot shell-session contexts from the daemon to artifacts/DB."""
    from .session_memory import SessionDaemonClient

    rows = SessionDaemonClient(get_settings()).flush(session_id=session_id)
    console.print_json(data={"flushed": rows})


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Serve the HTTP API."""

    # Built here rather than imported as a module-level object. `api.py` used to
    # end in `app = create_app()`, which ran `get_runtime()` as a side effect of
    # importing the module, so any test that imported the API connected to
    # whatever LOCAL_AGENT_DATABASE_URL names - the durable ledger on 5432, not
    # the disposable test database on 5433 that `tests/conftest.py` deliberately
    # arranges.
    from .api import create_app

    uvicorn.run(create_app(), host=host, port=port, reload=reload)
    # uvicorn has finished its orderly shutdown and printed its banners; the
    # only question left is whether the interpreter can exit. The lifespan
    # already destroyed the runtime it launched, but destroy does not stop a
    # thread hosting a recovered, parked workflow, and joining one would hang
    # a Ctrl-C'd server forever. Same boundary as the ledger CLI's `main`.
    from .dbos_app import exit_code_after_runtime_shutdown

    raise SystemExit(exit_code_after_runtime_shutdown(0))


@app.command()
def ask(prompt: str, workspace_id: str = WorkspaceId.GENERAL.value) -> None:
    event = normalize_prompt_event(prompt, workspace_id=workspace_id)
    result = _run_workflow_managed(WorkflowType.GENERAL_QUESTIONS, event)
    console.print_json(data=result)


@app.command("ingest-file")
def run_file_workflow(
    path: Path,
    workflow_type: WorkflowType,
    workspace_id: str,
    stable: bool = False,
) -> None:
    event = normalize_file_event(
        path=path,
        workspace_id=workspace_id,
        workflow_type=workflow_type,
        stable=stable,
    )
    result = _run_workflow_managed(workflow_type, event)
    console.print_json(data=result)


@app.command()
def watch(path: Path, workflow_type: WorkflowType, workspace_id: str) -> None:
    from .watcher import watch_directory

    console.print(f"Watching {path} for {workflow_type.value} events in workspace {workspace_id}")
    watch_directory(path, workspace_id, workflow_type)


@app.command("apple-notes-sync")
def apple_notes_sync(export_path: Path | None = None) -> None:
    payload = {"export_path": str(export_path)} if export_path else {}
    event = normalize_scheduled_event(
        source_type=SourceType.APPLE_NOTES,
        workspace_id=WorkspaceId.APPLE_NOTES.value,
        event_type="notes.poll",
        payload=payload,
    )
    console.print_json(data=_run_workflow_managed(WorkflowType.APPLE_NOTES_SYNC, event))


@app.command("create-tomorrow")
def create_tomorrow(instruction: str, target_top_level: str | None = None) -> None:
    """Turn one specific instruction into a proposed dated daily view.

    The patch ends in MANUAL_REVIEW; nothing is written to Workflowy.
    """
    payload: dict[str, str] = {"instruction": instruction}
    if target_top_level:
        payload["target_top_level"] = target_top_level
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="create.tomorrow",
        payload=payload,
    )
    console.print_json(data=_run_workflow_managed(WorkflowType.CREATE_TOMORROW, event))


@app.command("workflowy-import-chunks")
def workflowy_import_chunks(
    path: Path,
    limit: int | None = None,
    top_level: str | None = None,
    batch_size: int = 1,
) -> None:
    from .runtime import get_runtime

    runtime = get_runtime()
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.import_chunks",
        payload={
            "path": str(path),
            "limit": limit,
            "top_level": top_level,
            "batch_size": batch_size,
        },
    )
    runtime.repository.register_ingress_event(event)
    workflow_id = f"workflowy_import:{event.content_sha256}"
    runtime.repository.start_workflow_run(
        workflow_id=workflow_id,
        workflow_type="workflowy_import_chunks",
        workspace_id=WorkspaceId.WORKFLOWY.value,
        input_event_id=event.event_id,
    )
    added = runtime.retrieval.import_workflowy_chunks_jsonl(
        path,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        workflow_id=workflow_id,
        limit=limit,
        top_level=top_level,
        batch_size=batch_size,
    )
    console.print({"workflow_id": workflow_id, "embedding_chunks_added": added})


@app.command("workflowy-refresh")
def workflowy_refresh_cmd(
    input: Path | None = None,
    chunks: Path = Path("data/seed/workflowy_chunks_with_meta.jsonl"),
    max_chars: int = 1200,
    generation_id: Annotated[
        UUID | None,
        typer.Option(
            help=("Existing generation UUID to retry. Omit it for a fresh corpus generation.")
        ),
    ] = None,
) -> None:
    """Durably sync the Workflowy account and import the chunks into the
    vector store. Sync and import are checkpointed DBOS steps."""
    from .workflowy_refresh import run_workflowy_refresh

    result = run_workflowy_refresh(
        input_path=str(input) if input else None,
        chunks_path=str(chunks),
        max_chars=max_chars,
        generation_id=generation_id,
    )
    console.print(result)


@app.command()
def dashboard() -> None:
    from .runtime import get_runtime

    console.print_json(json.dumps(get_runtime().repository.dashboard_summary()))


@app.command("models")
def models() -> None:
    registry = ModelRegistry(get_settings())
    rows = {}
    for role, spec in registry.models.items():
        rows[role.value] = {
            **spec.model_dump(),
            "gguf_exists": Path(spec.gguf_path).exists() if spec.gguf_path else None,
            "mmproj_exists": Path(spec.mmproj_path).exists() if spec.mmproj_path else None,
        }
    console.print_json(json.dumps(rows))


@app.command("pi")
def pi_directive(
    directive: Annotated[
        list[str],
        typer.Argument(help='Directive text, for example: "/start /chandra"'),
    ],
    workspace_id: str = WorkspaceId.GENERAL.value,
    context_file: Path | None = None,
    max_window_tokens: int | None = None,
    shell_session_id: str | None = None,
    json_output: bool = False,
) -> None:
    from .pi_channel import render_terminal_result, run_terminal_query
    from .session_memory import SessionDaemonUnavailable

    text = " ".join(directive).strip()
    if is_timer_directive(text):
        result = run_timer_directive(text)
        if json_output:
            console.print_json(data=result)
        else:
            console.print(render_terminal_result(result), markup=False)
        if result.get("status") == "failed":
            raise typer.Exit(1)
        return
    _route_logs_to_stderr()
    streamed = False
    try:
        for item in run_terminal_query(
            text,
            workspace_id=workspace_id,
            context_file=context_file,
            max_window_tokens=max_window_tokens,
            shell_session_id=shell_session_id,
            streaming=not json_output,
        ):
            if isinstance(item, str):
                streamed = True
                sys.stdout.write(item)
                sys.stdout.flush()
            else:
                if streamed:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    streamed = False
                if json_output:
                    console.print_json(data=item)
                else:
                    console.print(render_terminal_result(item), markup=False)
    except SessionDaemonUnavailable as exc:
        if streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
        console.print(str(exc), style="red", highlight=False)
        raise typer.Exit(3) from exc


@app.command("pi-repl")
def pi_repl(workspace_id: str = WorkspaceId.GENERAL.value) -> None:
    from .pi_channel import repl

    repl(workspace_id=workspace_id)


@app.command("resume-workflows")
def resume_workflows() -> None:
    logging.getLogger("dbos").setLevel(logging.WARNING)
    from .dbos_app import (
        launch_dbos,
        resume_pending_workflows,
        should_resume_pending_workflows_locally,
    )
    from .runtime import get_runtime

    get_runtime()
    launch_dbos()
    if not should_resume_pending_workflows_locally():
        console.print(
            {
                "resumed_workflow_ids": [],
                "count": 0,
                "skipped": "dbos_conductor_manages_recovery",
            }
        )
        return
    resumed = resume_pending_workflows()
    console.print({"resumed_workflow_ids": resumed, "count": len(resumed)})


@app.command("vector-store-dump")
def vector_store_dump(output: Path) -> None:
    from .runtime import get_runtime
    from .vector_store_io import dump_vector_store

    runtime = get_runtime()
    summary = dump_vector_store(runtime, output)
    console.print_json(json.dumps(summary.as_dict()))


@app.command("vector-store-restore")
def vector_store_restore(source: Path) -> None:
    from .runtime import get_runtime
    from .vector_store_io import restore_vector_store

    runtime = get_runtime()
    summary = restore_vector_store(runtime, source)
    console.print_json(json.dumps(summary.as_dict()))


@app.command()
def harness() -> None:
    from .runtime import get_runtime
    from .workflow import run_workflow

    runtime = get_runtime()
    q = normalize_prompt_event(
        "What durable boundary owns Workflowy writes?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    question_result = run_workflow(WorkflowType.GENERAL_QUESTIONS, q)
    audio_event = normalize_scheduled_event(
        source_type=SourceType.FILE,
        workspace_id=WorkspaceId.AUDIO.value,
        event_type="file.created",
        payload={"source_uri": "file:///tmp/audio.m4a"},
    )
    audio_result = run_workflow(WorkflowType.AUDIO_TRANSCRIPTION, audio_event)
    training_event = normalize_scheduled_event(
        source_type=SourceType.SCHEDULED,
        workspace_id=WorkspaceId.TRAINING.value,
        event_type="training.export.requested",
        payload={},
    )
    training_result = run_workflow(WorkflowType.TRAINING_EXPORT_STUB, training_event)
    console.print_json(
        json.dumps(
            {
                "question_workflow": question_result.model_dump(mode="json"),
                "audio_stub": audio_result.model_dump(mode="json"),
                "training_stub": training_result.model_dump(mode="json"),
                "dashboard": runtime.repository.dashboard_summary(),
            }
        )
    )


if __name__ == "__main__":
    app()

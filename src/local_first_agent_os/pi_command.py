# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console

from .contracts import ArtifactRole, WorkspaceId
from .directives import DISPATCH_ALIAS, DirectiveParser
from .local_timer import is_timer_directive, run_timer_directive
from .pi_daemon import PiDaemonClient, PiDaemonUnavailable, ensure_pi_daemon
from .settings import get_settings


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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _requires_foreground_terminal(text: str) -> bool:
    """Keep terminal-bound input and long result-bearing OCR in the caller."""
    tokens = text.lower().split()
    microphone_capture = any(
        token == "/start" and next_token in {"/asr", "/audio"}
        for token, next_token in zip(tokens, tokens[1:], strict=False)
    )
    ocr_capture = any(
        token in {"/ocr", "/hard-ocr"}
        and (index == 0 or tokens[index - 1] not in {"/start", "/stop"})
        for index, token in enumerate(tokens)
    )
    return microphone_capture or ocr_capture


def _is_ocr_capture_command(text: str) -> bool:
    tokens = text.lower().split()
    return any(
        token in {"/ocr", "/hard-ocr"}
        and (index == 0 or tokens[index - 1] not in {"/start", "/stop"})
        for index, token in enumerate(tokens)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pi",
        description="Terminal command channel for the local-first Pi runtime.",
    )
    parser.add_argument(
        "--workspace-id",
        default=WorkspaceId.GENERAL.value,
        help="Workspace ID used for durable workflow records.",
    )
    parser.add_argument(
        "--context-file",
        type=Path,
        default=None,
        help="Optional context file; compaction runs first when it is above threshold.",
    )
    parser.add_argument(
        "--max-window-tokens",
        type=int,
        default=None,
        help="Override context window size for compaction threshold checks.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Shell session id for per-model conversational context.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw workflow result JSON instead of the user-facing Pi response.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Wait for the full answer before printing it.",
    )
    parser.add_argument(
        "text",
        nargs="*",
        help="Directive or query. Examples: /start /ocr, /stop, what do I know about DBOS?",
    )
    args, directive_args = parser.parse_known_args()
    if directive_args:
        args.text = [*args.text, *directive_args]
    if not args.text:
        code = _run_repl(workspace_id=args.workspace_id)
        if code:
            sys.exit(code)
        return
    text = shlex.join(args.text)
    if _is_walkthru_command(text) and not args.json and sys.stdin.isatty():
        code = _run_walkthru_interview(
            text,
            workspace_id=args.workspace_id,
            context_file=args.context_file,
            max_window_tokens=args.max_window_tokens,
            session_id=args.session_id,
        )
    else:
        code = _run_daemon_query(
            text,
            workspace_id=args.workspace_id,
            context_file=args.context_file,
            max_window_tokens=args.max_window_tokens,
            session_id=args.session_id,
            json_output=args.json,
            streaming=not (args.json or args.no_stream),
        )
    if code:
        sys.exit(code)


def _run_daemon_query(
    text: str,
    *,
    workspace_id: str,
    context_file: Path | None,
    max_window_tokens: int | None,
    session_id: str | None,
    json_output: bool,
    streaming: bool,
) -> int:
    settings = get_settings()
    if text.strip() == DISPATCH_ALIAS and not json_output:
        print(
            "dispatch: claiming at most one PENDING intent; if claimed, "
            "executing its tiered tasks, verification, checkpoint, and terminal "
            "ledger transition...",
            flush=True,
        )
    if not settings.pi_handoff_to_daemon or _requires_foreground_terminal(text):
        if _is_ocr_capture_command(text) and not json_output:
            print(
                "[pi] OCR is running in the foreground; large images can take several minutes.",
                flush=True,
            )
        return _run_direct_query(
            text,
            workspace_id=workspace_id,
            context_file=context_file,
            max_window_tokens=max_window_tokens,
            session_id=session_id,
            json_output=json_output,
            streaming=streaming,
        )
    context = (
        context_file.read_text(encoding="utf-8", errors="replace")
        if context_file is not None
        else None
    )
    shell_session_id = (
        session_id or os.getenv("LOCAL_AGENT_SHELL_SESSION_ID") or f"shell-{os.getppid()}"
    )
    try:
        ensure_pi_daemon(settings)
        events = PiDaemonClient(settings).stream_query(
            text=text,
            workspace_id=workspace_id,
            session_id=shell_session_id,
            context=context,
            max_window_tokens=max_window_tokens,
            streaming=streaming,
        )
        return _render_daemon_events(events, json_output=json_output)
    except PiDaemonUnavailable as exc:
        if settings.pi_direct_fallback:
            print(f"[pi] {exc} Falling back to direct local execution.", file=sys.stderr)
            return _run_direct_query(
                text,
                workspace_id=workspace_id,
                context_file=context_file,
                max_window_tokens=max_window_tokens,
                session_id=session_id,
                json_output=json_output,
                streaming=streaming,
            )
        print(str(exc), file=sys.stderr)
        return 3


WalkthruTurn = tuple[int, dict[str, Any] | None]


def _is_walkthru_command(text: str) -> bool:
    """Ask the parser instead of re-deriving the rule from the flags.

    Whether `/start /new-project` means a walkthru now turns on the absence of a
    draft path and of --no-walkthru, not merely the presence of --walkthru. That
    rule belongs to DirectiveParser; a second copy here is a second thing to keep
    in sync. The cheap prefix check stays in front so no other directive pays for
    building a parser, and a parse failure routes to the daemon, which is what
    renders the help block.
    """

    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    if tokens[:2] != ["/start", "/new-project"]:
        return False
    try:
        spec = DirectiveParser(get_settings()).parse(text)
    except Exception:
        return False
    return spec.walkthru_action is not None


def _run_walkthru_interview(
    initial_command: str,
    *,
    workspace_id: str,
    context_file: Path | None,
    max_window_tokens: int | None,
    session_id: str | None,
) -> int:
    settings = get_settings()
    shell_session_id = (
        session_id or os.getenv("LOCAL_AGENT_SHELL_SESSION_ID") or f"shell-{os.getppid()}"
    )
    context = (
        context_file.read_text(encoding="utf-8", errors="replace")
        if context_file is not None
        else None
    )
    try:
        ensure_pi_daemon(settings)
    except PiDaemonUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    command = _resume_command_if_requested(initial_command)

    def send(next_command: str) -> WalkthruTurn:
        return _run_walkthru_daemon_turn(
            next_command,
            workspace_id=workspace_id,
            session_id=shell_session_id,
            context=context,
            max_window_tokens=max_window_tokens,
        )

    code, payload = send(command)
    if code or payload is None:
        return code or 1
    return _run_walkthru_until_terminal_state(payload, send=send)


def _resume_command_if_requested(initial_command: str) -> str:
    from .directives import DirectiveParser
    from .gawd_walkthru import GawdWalkthruStore

    try:
        spec = DirectiveParser(get_settings()).parse(initial_command)
    except ValueError:
        return initial_command
    if (
        spec.walkthru_action != "start"
        or spec.walkthru_id is not None
        or spec.target_project_id is not None
        or spec.create_target_id is not None
    ):
        return initial_command
    repo_root = Path(__file__).resolve().parents[2]
    session = GawdWalkthruStore(repo_root).find_latest_incomplete()
    if session is None:
        return initial_command
    walkthru_id = str(session["walkthru_id"])
    state = str(session["state"])
    response = input(f"Resume saved GAWD Walkthru {walkthru_id} ({state})? [Y/n] ").strip()
    if response.lower() in {"n", "no"}:
        return initial_command
    return shlex.join(
        [
            "/start",
            "/new-project",
            "--walkthru",
            walkthru_id,
            "--status",
        ]
    )


def _run_walkthru_daemon_turn(
    command: str,
    *,
    workspace_id: str,
    session_id: str,
    context: str | None,
    max_window_tokens: int | None,
) -> WalkthruTurn:
    console = Console()
    failure_code = 0
    walkthru_payload: dict[str, Any] | None = None
    events = PiDaemonClient(get_settings()).stream_query(
        text=command,
        workspace_id=workspace_id,
        session_id=session_id,
        context=context,
        max_window_tokens=max_window_tokens,
        streaming=False,
    )
    try:
        for event in events:
            event_type = event.get("type")
            if event_type == "error":
                print(str(event.get("error") or "Pi daemon error."), file=sys.stderr)
                code = event.get("exit_code")
                failure_code = code if isinstance(code, int) and code > 0 else 1
                continue
            if event_type != "result":
                continue
            result = event.get("result")
            if not isinstance(result, dict):
                continue
            payload = event.get("directive_payload")
            if not isinstance(payload, dict) or payload.get("action") != "new_project_walkthru":
                rendered = event.get("rendered")
                console.print(
                    rendered if isinstance(rendered, str) else _render_local_result(result),
                    markup=False,
                )
                continue
            walkthru_payload = payload
            if payload.get("error"):
                print(f"walkthru failed: {payload['error']}", file=sys.stderr)
                failure_code = 1
                continue
            rendered = str(event.get("rendered") or "")
            if payload.get("state") != "finished":
                rendered = rendered.split("\nNext commands:", maxsplit=1)[0]
            console.print(rendered, markup=False)
    except PiDaemonUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3, None
    return failure_code, walkthru_payload


def _run_walkthru_until_terminal_state(
    payload: dict[str, Any],
    *,
    send: Callable[[str], WalkthruTurn],
    read_operator_input: Callable[[str], str] = input,
) -> int:
    current = payload
    try:
        while True:
            state = str(current.get("state") or "")
            walkthru_id = str(current.get("walkthru_id") or "")
            if state == "finished":
                return 0
            if not walkthru_id:
                print("GAWD Walkthru result is missing its id.", file=sys.stderr)
                return 1
            if state == "awaiting_answer":
                answer = read_operator_input("\nYou (/skip or /pause): ")
                normalized = answer.strip().lower()
                if normalized in {"/pause", "/quit", "/exit"}:
                    return _print_walkthru_resume_instructions(walkthru_id)
                if normalized == "/skip":
                    command = _build_resume_command(walkthru_id, "--skip")
                elif not answer.strip():
                    continue
                else:
                    print("\nSaving answer and preparing the summary…")
                    command = _build_resume_command(walkthru_id, "--answer", answer)
            elif state == "awaiting_summary":
                pending = current.get("pending_answer")
                if not isinstance(pending, dict) or not pending.get("verbatim"):
                    print("Saved GAWD Walkthru answer is missing.", file=sys.stderr)
                    return 1
                print("\nRetrying the saved answer summary…")
                command = _build_resume_command(
                    walkthru_id,
                    "--answer",
                    str(pending["verbatim"]),
                )
            elif state == "awaiting_review":
                choice = (
                    read_operator_input("\nAccept this summary? [Enter=yes, r=revise, p=pause] ")
                    .strip()
                    .lower()
                )
                if choice in {"p", "pause", "q", "quit", "exit"}:
                    return _print_walkthru_resume_instructions(walkthru_id)
                if choice in {"", "y", "yes", "a", "accept"}:
                    command = _build_resume_command(walkthru_id, "--accept")
                elif choice in {"r", "revise", "edit", "n", "no"}:
                    corrected = read_operator_input("Corrected summary: ")
                    if not corrected.strip():
                        continue
                    command = _build_resume_command(walkthru_id, "--revise", corrected)
                else:
                    print("Enter accepts; r revises; p pauses.")
                    continue
            elif state == "ready_to_finish":
                choice = (
                    read_operator_input(
                        "\nRender the sparse GAWD draft? [Enter=yes, e=edit, p=pause] "
                    )
                    .strip()
                    .lower()
                )
                if choice in {"p", "pause", "q", "quit", "exit"}:
                    return _print_walkthru_resume_instructions(walkthru_id)
                if choice in {"", "y", "yes", "f", "finish"}:
                    command = _build_resume_command(walkthru_id, "--finish")
                elif choice in {"e", "edit", "n", "no"}:
                    section_id = read_operator_input("Section id: ").strip()
                    corrected = read_operator_input("Corrected summary: ")
                    if not section_id or not corrected.strip():
                        continue
                    command = _build_resume_command(
                        walkthru_id,
                        "--edit",
                        section_id,
                        corrected,
                    )
                else:
                    print("Enter renders; e edits a section; p pauses.")
                    continue
            else:
                print(f"Unsupported GAWD Walkthru state: {state}", file=sys.stderr)
                return 1
            code, updated = send(command)
            if code or updated is None:
                return code or 1
            current = updated
    except (EOFError, KeyboardInterrupt):
        print()
        return _print_walkthru_resume_instructions(str(current.get("walkthru_id") or ""))


def _build_resume_command(walkthru_id: str, action: str, *values: str) -> str:
    return shlex.join(
        [
            "/start",
            "/new-project",
            "--walkthru",
            walkthru_id,
            action,
            *values,
        ]
    )


def _print_walkthru_resume_instructions(walkthru_id: str) -> int:
    print(
        f"\nGAWD Walkthru {walkthru_id} is saved. Run the same bare --walkthru command to resume."
    )
    return 0


def _render_daemon_events(events: Any, *, json_output: bool) -> int:
    console = Console()
    streamed = False
    failure_code = 0
    for event in events:
        event_type = event.get("type")
        if event_type == "status":
            message = event.get("message")
            if isinstance(message, str) and not json_output:
                print(f"[pi] {message}", flush=True)
            continue
        if event_type == "delta":
            text = event.get("text")
            if isinstance(text, str):
                streamed = True
                sys.stdout.write(text)
                sys.stdout.flush()
            continue
        if event_type == "result":
            result = event.get("result")
            if not isinstance(result, dict):
                continue
            if streamed:
                sys.stdout.write("\n")
                sys.stdout.flush()
            if json_output:
                console.print_json(data=result)
            elif not (streamed and _has_artifact_role(result, ArtifactRole.ANSWER)):
                rendered = event.get("rendered")
                console.print(
                    rendered if isinstance(rendered, str) else _render_local_result(result),
                    markup=False,
                )
            streamed = False
            continue
        if event_type == "error":
            if streamed:
                sys.stdout.write("\n")
                sys.stdout.flush()
                streamed = False
            error = event.get("error")
            print(str(error or "Pi daemon error."), file=sys.stderr)
            # Preserve the daemon's typed exit code (2 = model not loaded,
            # 3 = session daemon down) so callers branch the same way they
            # would on the direct path; default to 1 for generic failures.
            code = event.get("exit_code")
            failure_code = code if isinstance(code, int) and code > 0 else 1
    return failure_code


def _run_repl(*, workspace_id: str) -> int:
    console = Console()
    console.print(
        "Pi channel. Use /start, /start /ocr, /get query, /fetch /workflowy query, "
        "/timer 50, /chrome list, "
        "/try-milestone, /approve-most-recent, /dispatch, /compact, /stop, "
        "/review-merge, /approve-merge <approval_id>, "
        "or plain text."
    )
    while True:
        try:
            line = input("pi> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if line in {"", "/quit", "/exit"}:
            if line:
                return 0
            continue
        # A failed query (model not loaded, daemon error, …) should not tear
        # down the interactive session — report it and keep the prompt open.
        _run_daemon_query(
            line,
            workspace_id=workspace_id,
            context_file=None,
            max_window_tokens=None,
            session_id=None,
            json_output=False,
            streaming=True,
        )


def _run_direct_query(
    text: str,
    *,
    workspace_id: str,
    context_file: Path | None,
    max_window_tokens: int | None,
    session_id: str | None,
    json_output: bool,
    streaming: bool,
) -> int:
    console = Console()
    if is_timer_directive(text):
        result = run_timer_directive(text)
        if json_output:
            console.print_json(data=result)
        else:
            console.print(_render_local_result(result), markup=False)
        return 1 if result.get("status") == "failed" else 0
    from .model_manager import ModelNotLoadedError
    from .pi_channel import render_terminal_result, run_terminal_query
    from .session_memory import SessionDaemonUnavailable

    _route_logs_to_stderr()
    shell_session_id = (
        session_id or os.getenv("LOCAL_AGENT_SHELL_SESSION_ID") or f"shell-{os.getppid()}"
    )
    streamed = False

    try:
        for item in run_terminal_query(
            text,
            workspace_id=workspace_id,
            context_file=context_file,
            max_window_tokens=max_window_tokens,
            shell_session_id=shell_session_id,
            streaming=streaming,
        ):
            if isinstance(item, str):
                streamed = True
                sys.stdout.write(item)
                sys.stdout.flush()
            else:
                if streamed:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                if json_output:
                    console.print_json(data=item)
                elif not (streamed and _has_artifact_role(item, ArtifactRole.ANSWER)):
                    console.print(render_terminal_result(item), markup=False)
                streamed = False
    except ModelNotLoadedError as exc:
        if streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
        print(str(exc), file=sys.stderr)
        return 2
    except SessionDaemonUnavailable as exc:
        if streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
        print(str(exc), file=sys.stderr)
        return 3
    return 0


def _has_artifact_role(result: dict[str, object], role: ArtifactRole) -> bool:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("role") == role.value:
            return True
    return False


def _render_local_result(result: dict[str, object]) -> str:
    terminal_message = result.get("terminal_message")
    if isinstance(terminal_message, str):
        return terminal_message
    return str(result.get("status", "completed"))

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, overload

from rich.console import Console

from .contracts import (
    BARE_TERMINAL_DIRECTIVES,
    ArtifactRole,
    CompactionPayload,
    ModelRole,
    PiRequestContext,
    SourceType,
    TerminalAction,
    TerminalActionKind,
    WorkflowType,
    WorkspaceId,
)
from .directives import (
    APPROVED_GAWD_ALIAS,
    DISPATCHER_ALIAS,
    NEW_PROJECT_ALIAS,
    TOP_LEVEL_DIRECTIVES,
    DirectiveParser,
)
from .ingress import normalize_prompt_event, normalize_scheduled_event
from .local_timer import is_timer_directive, run_timer_directive
from .model_registry import ModelRegistry
from .runtime import get_runtime
from .session_memory import SessionDaemonClient, ensure_session_daemon
from .settings import get_settings
from .utils import TURN_ASSISTANT, TURN_USER, estimate_tokens
from .workflow import WorkflowEngine, run_workflow

GENERAL_WORKSPACE_ID = WorkspaceId.GENERAL.value

_request_ctx: ContextVar[PiRequestContext | None] = ContextVar(
    "pi_request_ctx",
    default=None,
)


def _current_request_ctx() -> PiRequestContext:
    return _request_ctx.get() or PiRequestContext()


def _run_workflow_durably_or_direct(workflow_type: WorkflowType, event):
    if get_settings().use_dbos:
        from .dbos_app import run_workflow_durably

        return run_workflow_durably(workflow_type, event)
    return run_workflow(workflow_type, event).model_dump(mode="json")


console = Console()


def normalize_terminal_text(text: str) -> str:
    stripped = text.strip()
    first, _, tail = stripped.partition(" ")
    if first in BARE_TERMINAL_DIRECTIVES:
        return f"/{first}" + (f" {tail}" if tail else "")
    return stripped


def _safe_shlex_split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def split_chain(text: str) -> list[str]:
    normalized = normalize_terminal_text(text)
    if not normalized:
        return []
    if not normalized.startswith("/"):
        return [normalized]
    tokens = _safe_shlex_split(normalized)
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "&&":
            if current:
                segments.append(current)
                current = []
            continue
        if (
            token in TOP_LEVEL_DIRECTIVES
            and current
            and not _is_nested_directive_argument(current, token)
        ):
            segments.append(current)
            current = [token]
            continue
        current.append(token)
    if current:
        segments.append(current)
    return [
        shlex.join(segment) if segment and segment[0].startswith("/") else " ".join(segment)
        for segment in segments
    ]


def _is_nested_directive_argument(current: list[str], token: str) -> bool:
    return (
        (tuple(current) in {("/start",), ("/stop",)} and token in {"/ocr", "/hard-ocr"})
        or (current == ["/start"] and token == "/store")
        or (current == ["/read"] and token == "/ledger")
    )


def plan_terminal_actions(text: str) -> list[TerminalAction]:
    actions: list[TerminalAction] = []
    for segment in split_chain(text):
        actions.extend(_plan_segment(segment))
    return actions


def _plan_segment(segment: str) -> list[TerminalAction]:
    if not segment.startswith("/"):
        return [TerminalAction(TerminalActionKind.QUERY, segment)]
    tokens = _safe_shlex_split(segment)
    if not tokens:
        return []
    command = tokens[0]
    if not command.startswith("/"):
        return [TerminalAction(TerminalActionKind.QUERY, " ".join(tokens))]
    if command in {
        "/status",
        "/project-status",
        "/get",
        "/fetch",
        "/compact",
        "/timer",
        "/store",
        "/embed",
        "/ocr",
        "/hard-ocr",
        "/send-to-wf",
        "/done",
        "/chrome",
        "/ledger",
        "/read",
    }:
        return [TerminalAction(TerminalActionKind.DIRECTIVE, shlex.join(tokens))]
    if command == "/screenshot":
        if len(tokens) <= 2:
            return [TerminalAction(TerminalActionKind.DIRECTIVE, shlex.join(tokens))]
        return [
            TerminalAction(TerminalActionKind.DIRECTIVE, shlex.join(tokens[:2])),
            TerminalAction(TerminalActionKind.QUERY, " ".join(tokens[2:])),
        ]
    if command in {"/start", "/stop"}:
        if len(tokens) == 1:
            return [TerminalAction(TerminalActionKind.DIRECTIVE, command)]
        if tokens[1] in {"/store", NEW_PROJECT_ALIAS, APPROVED_GAWD_ALIAS, DISPATCHER_ALIAS}:
            return [TerminalAction(TerminalActionKind.DIRECTIVE, shlex.join(tokens))]
        directive_tokens = tokens[:1]
        query_model_role: ModelRole | None = None
        tail_start = 1
        if _looks_like_model_selector(tokens[1]):
            directive_tokens.append(tokens[1])
            query_model_role = _model_role_for_start_selector(tokens[1])
            tail_start = 2
        actions = [TerminalAction(TerminalActionKind.DIRECTIVE, shlex.join(directive_tokens))]
        if tail_start < len(tokens) and Path(tokens[tail_start]).expanduser().exists():
            actions.append(
                TerminalAction(
                    TerminalActionKind.DIRECTIVE,
                    shlex.join(["/store", tokens[tail_start]]),
                )
            )
            tail_start += 1
            query_model_role = None
        if tail_start < len(tokens):
            actions.append(
                TerminalAction(
                    TerminalActionKind.QUERY,
                    " ".join(tokens[tail_start:]),
                    model_role=query_model_role,
                    model_selector=tokens[1] if query_model_role is not None else None,
                )
            )
        return actions
    query_model_role = _model_role_for_start_selector(command)
    if query_model_role is not None and len(tokens) > 1:
        return [
            TerminalAction(
                TerminalActionKind.QUERY,
                " ".join(tokens[1:]),
                model_role=query_model_role,
                model_selector=command,
            )
        ]
    return [TerminalAction(TerminalActionKind.DIRECTIVE, shlex.join(tokens))]


def _artifact_payload(result: dict[str, Any], role: ArtifactRole | str) -> dict[str, Any] | None:
    role_value = role.value if isinstance(role, ArtifactRole) else role
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, dict) or str(artifact.get("role")) != role_value:
            continue
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        payload = get_runtime().artifact_store.read_json(artifact_id)
        return payload if isinstance(payload, dict) else None
    return None


def directive_result_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    return _artifact_payload(result, ArtifactRole.DIRECTIVE_RESULT)


def render_terminal_result(result: dict[str, Any]) -> str:
    terminal_message = result.get("terminal_message")
    if isinstance(terminal_message, str):
        return terminal_message

    answer = _artifact_payload(result, ArtifactRole.ANSWER)
    if answer is not None and "answer" in answer:
        value = answer["answer"]
        if isinstance(value, str):
            return value
        return str(value)

    directive = _artifact_payload(result, ArtifactRole.DIRECTIVE_RESULT)
    if directive is not None:
        status = directive.get("status") or directive.get("action") or "completed"
        if directive.get("error"):
            return f"{status}: {directive['error']}"
        if directive.get("action") == "new_project_walkthru":
            return render_gawd_walkthru(directive)
        if directive.get("warning"):
            return str(directive["warning"])
        if directive.get("report"):
            return str(directive["report"])
        return str(status)

    compaction = _artifact_payload(result, ArtifactRole.CONTEXT_COMPACTION)
    if compaction is not None:
        status = compaction.get("status", "completed")
        original = compaction.get("original_token_count")
        compacted = compaction.get("compacted_token_count")
        if status == "compacted":
            return f"compacted context: {original} -> {compacted} estimated tokens"
        return f"compaction {status}"

    ocr_manifest = _artifact_payload(result, ArtifactRole.OCR_BATCH_MANIFEST)
    if ocr_manifest is not None:
        if ocr_manifest.get("error"):
            return f"OCR failed: {ocr_manifest['error']}"
        transcribed = ocr_manifest.get("images_transcribed")
        failed = ocr_manifest.get("images_failed")
        skipped = ocr_manifest.get("images_skipped")
        if isinstance(transcribed, list) and len(transcribed) == 1:
            ocr_text = _artifact_payload(result, ArtifactRole.OCR_TEXT)
            if ocr_text is not None and isinstance(ocr_text.get("transcription"), str):
                return str(ocr_text["transcription"])
        return (
            f"OCR persisted {len(transcribed) if isinstance(transcribed, list) else 0} image(s) "
            f"from {ocr_manifest.get('root', 'the requested path')}; "
            f"{len(failed) if isinstance(failed, list) else 0} failed, "
            f"{len(skipped) if isinstance(skipped, list) else 0} skipped, not indexed."
        )

    status = result.get("status", "completed")
    workflow_type = result.get("workflow_type", "workflow")
    return f"{workflow_type}: {status}"


def render_gawd_walkthru(
    payload: dict[str, Any],
    *,
    include_commands: bool = True,
) -> str:
    state = str(payload.get("state") or "")
    walkthru_id = str(payload.get("walkthru_id") or "")
    progress = f"{payload.get('completed_sections', 0)}/{payload.get('total_sections', '?')}"
    lines = [f"GAWD Walkthru {walkthru_id} ({progress})"]
    if state == "awaiting_answer":
        section = payload.get("section")
        if isinstance(section, dict):
            lines.extend(
                (
                    f"\n{section.get('label', section.get('section_id', 'Next section'))}",
                    str(section.get("question") or ""),
                    f"Guidance: {section.get('guidance')}",
                )
            )
    elif state == "awaiting_summary":
        pending = payload.get("pending_answer")
        if isinstance(pending, dict):
            lines.extend(
                (
                    f"\nSection: {pending.get('section_id', '')}",
                    "\nSaved answer awaiting summary:",
                    str(pending.get("verbatim") or ""),
                )
            )
    elif state == "awaiting_review":
        proposal = payload.get("proposal")
        if isinstance(proposal, dict):
            suggestions = proposal.get("suggestions")
            lines.extend(
                (
                    f"\nSection: {proposal.get('section_id', '')}",
                    "\nVerbatim answer:",
                    str(proposal.get("verbatim") or ""),
                    "\nProposed summary:",
                    str(proposal.get("summary") or ""),
                    "\nModel suggestions (not part of the contract):",
                )
            )
            suggestion_lines = (
                [f"- {item}" for item in suggestions if isinstance(item, str)]
                if isinstance(suggestions, list)
                else []
            )
            lines.extend(suggestion_lines or ["- none"])
    elif state == "ready_to_finish":
        review = payload.get("review")
        lines.append("\nFinal review:")
        if isinstance(review, list):
            for response in review:
                if not isinstance(response, dict):
                    continue
                summary = response.get("accepted_summary")
                lines.append(
                    f"[{response.get('section_id')}] " + (str(summary) if summary else "(skipped)")
                )
    elif state == "finished":
        lines.extend(("\nSparse GAWD draft rendered:", str(payload.get("draft_path") or "")))
    if include_commands:
        next_commands = payload.get("next_commands")
        if isinstance(next_commands, dict):
            lines.append("\nNext commands:")
            lines.extend(str(command) for command in next_commands.values())
        elif payload.get("next_command"):
            lines.extend(("\nNext command:", str(payload["next_command"])))
    return "\n".join(lines)


def _looks_like_model_selector(token: str) -> bool:
    if token in {"/ocr", "/hard-ocr"}:
        return True
    if token in TOP_LEVEL_DIRECTIVES:
        return False
    if token.startswith("/"):
        return True
    models_dir = get_settings().llama_models_dir.expanduser()
    return (models_dir / token).exists()


def _model_role_for_start_selector(token: str) -> ModelRole | None:
    try:
        return DirectiveParser(get_settings()).parse(shlex.join(["/start", token])).model_role
    except Exception:
        return None


def _model_id_for_action(action: TerminalAction) -> str:
    if action.model_role is not None:
        role = action.model_role
        return ModelRegistry(get_settings()).resolve_model(role).model_id
    try:
        runtime = get_runtime()
        role = runtime.model_manager.effective_general_role()
        return runtime.model_registry.resolve_model(role).model_id
    except Exception:
        return ModelRegistry(get_settings()).resolve_model(ModelRole.GENERAL).model_id


def _default_shell_session_id() -> str:
    return os.getenv("LOCAL_AGENT_SHELL_SESSION_ID") or f"shell-{os.getppid()}"


def _merge_contexts(*contexts: str | None) -> str | None:
    parts = [context for context in contexts if context is not None and context.strip()]
    return "\n\n".join(parts) if parts else None


def _extract_answer_text_from_result(result: dict[str, Any]) -> str | None:
    direct = result.get("answer")
    if isinstance(direct, str):
        return direct
    answer = _artifact_payload(result, ArtifactRole.ANSWER)
    if answer is None:
        return None
    value = answer.get("answer")
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _compaction_artifact_id(result: dict[str, Any]) -> str | None:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("role")) != ArtifactRole.CONTEXT_COMPACTION.value:
            continue
        artifact_id = artifact.get("artifact_id")
        return artifact_id if isinstance(artifact_id, str) else None
    return None


def build_general_prompt(text: str, context: str | None) -> str:
    return text if context is None else f"{text}\n\nContext:\n{context}"


def should_compact_context(context: str) -> bool:
    parser = DirectiveParser(get_settings())
    token_count = estimate_tokens(context)
    return token_count >= int(parser.default_max_window_tokens * parser.compaction_threshold_ratio)


def run_pi_directive(
    text: str,
    *,
    workspace_id: str | None = None,
    context: str | None = None,
    max_window_tokens: int | None = None,
) -> dict[str, Any]:
    ctx = _current_request_ctx()
    effective_workspace_id = workspace_id or ctx.workspace_id
    effective_context = ctx.context if context is None else context
    effective_max_window_tokens = max_window_tokens or ctx.max_window_tokens
    directive = normalize_terminal_text(text)
    if not directive.startswith("/"):
        return run_general_query(
            directive,
            workspace_id=effective_workspace_id,
            context=effective_context,
        )
    if is_timer_directive(directive):
        return run_timer_directive(directive)
    parser = DirectiveParser(get_settings())
    payload: dict[str, object] = {"directive": directive}
    if effective_context is not None:
        payload["context"] = effective_context
    if effective_max_window_tokens is not None:
        payload["max_window_tokens"] = effective_max_window_tokens
    command = directive.split(maxsplit=1)[0]
    if command == "/compact":
        payload.setdefault("max_window_tokens", parser.default_max_window_tokens)
        payload["threshold_ratio"] = parser.compaction_threshold_ratio
        payload["target_ratio"] = parser.compaction_target_ratio
        event_type = "pi.context_compaction"
        workflow_type = WorkflowType.CONTEXT_COMPACTION
    else:
        event_type = "pi.directive"
        workflow_type = WorkflowType.MODEL_DIRECTIVE
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=effective_workspace_id,
        event_type=event_type,
        payload=payload,
    )
    return _run_workflow_durably_or_direct(workflow_type, event)


def run_context_compaction(
    *,
    workspace_id: str,
    context: str,
    max_window_tokens: int,
    threshold_ratio: float,
    target_ratio: float,
) -> dict[str, Any]:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=workspace_id,
        event_type="pi.context_compaction",
        payload={
            "directive": "/compact",
            "context": context,
            "max_window_tokens": max_window_tokens,
            "threshold_ratio": threshold_ratio,
            "target_ratio": target_ratio,
        },
    )
    return _run_workflow_durably_or_direct(WorkflowType.CONTEXT_COMPACTION, event)


def extract_compacted_context_or_raise(result: dict[str, Any], original_context: str) -> str:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("Compaction result is missing artifacts.")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("role")) != ArtifactRole.CONTEXT_COMPACTION.value:
            continue
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise RuntimeError("Compaction artifact is missing artifact_id.")
        try:
            raw = get_runtime().artifact_store.read_json(artifact_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to read compaction artifact: {artifact_id}") from exc
        payload = CompactionPayload.model_validate(raw)
        if payload.status == "not_needed":
            return original_context
        return payload.compacted_context
    raise RuntimeError("Compaction result did not include a context_compaction artifact.")


def run_general_query(
    text: str,
    *,
    workspace_id: str = GENERAL_WORKSPACE_ID,
    context: str | None = None,
    model_role: ModelRole | str | None = None,
    model_selector: str | None = None,
) -> dict[str, Any]:
    prompt = build_general_prompt(text, context)
    event = normalize_prompt_event(prompt, workspace_id=workspace_id)
    payload: dict[str, Any] = {**event.payload, "use_retrieval": False}
    if model_role is not None:
        payload["model_role"] = ModelRole(model_role).value
    if model_selector is not None:
        payload["model_selector"] = model_selector
    event = event.model_copy(update={"payload": payload})
    return _run_workflow_durably_or_direct(WorkflowType.GENERAL_QUESTIONS, event)


def _stream_general_query(
    text: str,
    *,
    context: str | None = None,
    model_role: ModelRole | str | None = None,
    model_selector: str | None = None,
) -> Iterator[str | dict[str, Any]]:
    ctx = _current_request_ctx()
    prompt = build_general_prompt(text, context)
    event = normalize_prompt_event(prompt, workspace_id=ctx.workspace_id)
    payload: dict[str, Any] = {**event.payload, "use_retrieval": False}
    if model_role is not None:
        payload["model_role"] = ModelRole(model_role).value
    if model_selector is not None:
        payload["model_selector"] = model_selector
    event = event.model_copy(update={"payload": payload})
    for item in WorkflowEngine(get_runtime()).stream_general_questions(event):
        if isinstance(item, str):
            yield item
        else:
            yield item.model_dump(mode="json")


@overload
def run_terminal_query(
    text: str,
    *,
    workspace_id: str = GENERAL_WORKSPACE_ID,
    context: str | None = None,
    context_file: Path | None = None,
    max_window_tokens: int | None = None,
    shell_session_id: str | None = None,
    streaming: Literal[True],
) -> Iterator[str | dict[str, Any]]: ...


@overload
def run_terminal_query(
    text: str,
    *,
    workspace_id: str = GENERAL_WORKSPACE_ID,
    context: str | None = None,
    context_file: Path | None = None,
    max_window_tokens: int | None = None,
    shell_session_id: str | None = None,
    streaming: Literal[False] = False,
) -> list[dict[str, Any]]: ...


def run_terminal_query(
    text: str,
    *,
    workspace_id: str = GENERAL_WORKSPACE_ID,
    context: str | None = None,
    context_file: Path | None = None,
    max_window_tokens: int | None = None,
    shell_session_id: str | None = None,
    streaming: bool = False,
) -> Iterator[str | dict[str, Any]] | list[dict[str, Any]]:
    shell_session_id = shell_session_id or _default_shell_session_id()
    if streaming:
        return _iter_terminal_query(
            text,
            workspace_id=workspace_id,
            context=context,
            context_file=context_file,
            max_window_tokens=max_window_tokens,
            shell_session_id=shell_session_id,
            streaming=True,
        )
    return [
        item
        for item in _iter_terminal_query(
            text,
            workspace_id=workspace_id,
            context=context,
            context_file=context_file,
            max_window_tokens=max_window_tokens,
            shell_session_id=shell_session_id,
            streaming=False,
        )
        if isinstance(item, dict)
    ]


def _iter_terminal_query(
    text: str,
    *,
    workspace_id: str,
    context: str | None,
    context_file: Path | None,
    max_window_tokens: int | None,
    shell_session_id: str | None,
    streaming: bool,
) -> Iterator[str | dict[str, Any]]:
    if context_file is not None:
        context = context_file.read_text(encoding="utf-8", errors="replace")
    token = _request_ctx.set(
        PiRequestContext(
            workspace_id=workspace_id,
            shell_session_id=shell_session_id,
            source_workspace_id=workspace_id,
            context=context,
            max_window_tokens=max_window_tokens,
            streaming=streaming,
        )
    )
    try:
        yield from _execute_query(text)
    finally:
        _request_ctx.reset(token)


def _execute_query(text: str) -> Iterator[str | dict[str, Any]]:
    ctx = _current_request_ctx()
    parser = DirectiveParser(get_settings())
    effective_max_window_tokens = ctx.max_window_tokens or parser.default_max_window_tokens
    actions = plan_terminal_actions(text)
    local_only = all(
        action.kind == TerminalActionKind.DIRECTIVE and is_timer_directive(action.text)
        for action in actions
    )
    if not ctx.shell_session_id and not local_only:
        raise RuntimeError("Pi requires a shell_session_id; stateless terminal mode is disabled.")
    session_client: SessionDaemonClient | None = None

    def get_shell_session_id() -> str:
        if ctx.shell_session_id is None:
            raise RuntimeError("This action requires a shell session.")
        return ctx.shell_session_id

    def get_session_client() -> SessionDaemonClient:
        nonlocal session_client
        if session_client is None:
            ensure_session_daemon(get_settings())
            session_client = SessionDaemonClient(get_settings())
        return session_client

    def compact_context_if_needed(
        action_text: str,
        active_context: str | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if active_context is None or _is_compact_directive(action_text):
            return active_context, None
        pending_prompt = build_general_prompt(action_text, active_context)
        token_count = estimate_tokens(pending_prompt)
        threshold_tokens = int(effective_max_window_tokens * parser.compaction_threshold_ratio)
        if token_count < threshold_tokens:
            return active_context, None
        compact_result = run_context_compaction(
            workspace_id=ctx.workspace_id,
            context=active_context,
            max_window_tokens=effective_max_window_tokens,
            threshold_ratio=parser.compaction_threshold_ratio,
            target_ratio=parser.compaction_target_ratio,
        )
        compacted_context = extract_compacted_context_or_raise(compact_result, active_context)
        return compacted_context, compact_result

    def session_context_for_action(action: TerminalAction) -> tuple[str | None, str | None]:
        model_id = _model_id_for_action(action)
        ctx.model_selector = action.model_selector
        ctx.model_id = model_id
        return model_id, get_session_client().get_context(
            session_id=get_shell_session_id(),
            model_id=model_id,
        )

    def begin_session_turn(action: TerminalAction) -> dict[str, str]:
        model_id = ctx.model_id or _model_id_for_action(action)
        handle = get_session_client().begin_turn(
            session_id=get_shell_session_id(),
            model_id=model_id,
            user_text=action.text,
            model_selector=action.model_selector,
            max_window_tokens=effective_max_window_tokens,
            source_workspace_id=ctx.source_workspace_id or ctx.workspace_id,
            retrieved_artifact_ids=ctx.retrieval_sources,
        )
        handle["model_id"] = model_id
        return handle

    def complete_session_turn(
        result: dict[str, Any],
        handle: dict[str, str],
    ) -> None:
        answer = _extract_answer_text_from_result(result)
        if answer is None:
            return
        get_session_client().complete_turn(
            session_id=get_shell_session_id(),
            model_id=handle["model_id"],
            turn_id=handle["turn_id"],
            created_at=handle["created_at"],
            answer=answer,
        )

    index = 0
    while index < len(actions):
        action = actions[index]
        if action.kind == TerminalActionKind.DIRECTIVE and is_timer_directive(action.text):
            yield run_timer_directive(action.text)
            index += 1
            continue
        if action.kind == TerminalActionKind.DIRECTIVE and _is_start_directive(action.text):
            group = [action]
            cursor = index + 1
            while cursor < len(actions) and _is_start_directive(actions[cursor].text):
                group.append(actions[cursor])
                cursor += 1
            if len(group) > 1:
                ctx.context, compact_result = compact_context_if_needed(
                    group[0].text,
                    ctx.context,
                )
                if compact_result is not None:
                    yield compact_result
                with ThreadPoolExecutor(max_workers=len(group)) as executor:
                    futures = [
                        executor.submit(
                            run_pi_directive,
                            item.text,
                            workspace_id=ctx.workspace_id,
                            context=ctx.context,
                            max_window_tokens=effective_max_window_tokens,
                        )
                        for item in group
                    ]
                    for future in futures:
                        yield future.result()
                index = cursor
                continue
            action = group[0]
        if action.kind == TerminalActionKind.DIRECTIVE:
            ctx.context, compact_result = compact_context_if_needed(action.text, ctx.context)
            if compact_result is not None:
                yield compact_result
            directive_result = run_pi_directive(
                action.text,
                workspace_id=ctx.workspace_id,
                context=ctx.context,
                max_window_tokens=effective_max_window_tokens,
            )
            yield directive_result
            if ctx.context is not None and _is_compact_directive(action.text):
                ctx.context = extract_compacted_context_or_raise(directive_result, ctx.context)
        else:
            model_id, session_context = session_context_for_action(action)
            query_context = _merge_contexts(session_context, ctx.context)
            query_context, compact_result = compact_context_if_needed(
                action.text,
                query_context,
            )
            if compact_result is not None:
                yield compact_result
                if model_id is not None and ctx.context is None:
                    get_session_client().set_context(
                        session_id=get_shell_session_id(),
                        model_id=model_id,
                        context=query_context or "",
                        compacted_summary_artifact_id=_compaction_artifact_id(compact_result),
                        max_window_tokens=effective_max_window_tokens,
                    )
            turn_handle = begin_session_turn(action)
            if ctx.streaming:
                for item in _stream_general_query(
                    action.text,
                    context=query_context,
                    model_role=action.model_role,
                    model_selector=action.model_selector,
                ):
                    if isinstance(item, dict):
                        complete_session_turn(item, turn_handle)
                    yield item
            else:
                kwargs: dict[str, Any] = {}
                if action.model_role is not None:
                    kwargs["model_role"] = action.model_role
                if action.model_selector is not None:
                    kwargs["model_selector"] = action.model_selector
                query_result = run_general_query(
                    action.text,
                    workspace_id=ctx.workspace_id,
                    context=query_context,
                    **kwargs,
                )
                complete_session_turn(query_result, turn_handle)
                yield query_result
        index += 1


def _is_compact_directive(text: str) -> bool:
    return normalize_terminal_text(text).split(maxsplit=1)[0] == "/compact"


def _is_start_directive(text: str) -> bool:
    return normalize_terminal_text(text).split(maxsplit=1)[0] == "/start"


def repl(workspace_id: str = GENERAL_WORKSPACE_ID) -> None:
    console.print(
        "Pi channel. Use /start, /start /ocr, /get query, /fetch /workflowy query, "
        "/timer 50, /chrome list, /compact, "
        "/stop, or plain text."
    )
    context = ""
    while True:
        try:
            line = input("pi> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if line in {"", "/quit", "/exit"}:
            if line:
                return
            continue
        streamed_chars: list[str] = []
        streamed = False
        for item in run_terminal_query(
            line,
            workspace_id=workspace_id,
            context=context or None,
            streaming=True,
        ):
            if isinstance(item, str):
                streamed = True
                streamed_chars.append(item)
                sys.stdout.write(item)
                sys.stdout.flush()
            else:
                if streamed:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    streamed = False
                console.print(render_terminal_result(item), markup=False)
        if streamed_chars:
            answer = "".join(streamed_chars)
            context += f"{TURN_USER}\n{line}\n{TURN_ASSISTANT}\n{answer}\n"

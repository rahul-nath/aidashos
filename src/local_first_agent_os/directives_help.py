# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import shlex
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from .contracts import DirectiveHelp
from .directives import AGENT_QUERY_ALIASES, TOP_LEVEL_DIRECTIVES, DirectiveParser

CANONICAL_EXAMPLES: tuple[str, ...] = (
    "/start /qwen",
    "/start /chandra",
    "/start /ocr",
    "/start /asr",
    "/start /med",
    "/start /logging",
    "/stop /logging",
    "/status",
    "/start /store ~/ai_projects/ai_stack_local/output",
    '/get "workflowy durable boundary"',
    '/fetch /workflowy "give me the first idea bullet under /ideas"',
    "/timer 50",
    "/ocr /absolute/path/to/images",
    "/store ~/Documents/notes",
    '/screenshot ~/Pictures/screen.png "summarize this"',
    "/send-to-wf ~/Documents/note.txt 04/28",
    "/done embeddings durability",
    "/chrome list",
    "/chrome open https://example.com",
    "/chrome read docs",
    '/chrome decide docs --prompt "which can I close?"',
    "/chrome summarize docs",
    "/chrome close-category docs --yes",
    "/chrome close 1",
    "/compact",
    "/claude explain how the ledger records a dispatch",
    "/codex what does resolve_transcript_pointer guarantee?",
    "/stop",
    "/try-milestone",
    "/approve-most-recent",
    "/dispatch",
    "/review-merge",
    "/approve-merge <approval_id>",
)


def explain_failure(parser: DirectiveParser, raw: str, error: str) -> DirectiveHelp:
    tokens = shlex.split(raw) if raw.strip() else []
    if not tokens:
        return DirectiveHelp(
            summary="Pi received an empty directive.",
            suggestions=[
                "Type /start with a model alias (e.g. /start /qwen) to load a model, "
                "or send plain text to query the base.",
            ],
            canonical_examples=list(CANONICAL_EXAMPLES),
        )
    head = tokens[0]
    if not head.startswith("/"):
        return DirectiveHelp(
            summary=(
                "Plain text was routed through the directive workflow instead of the general "
                "query path."
            ),
            suggestions=[
                "Run `pi <text>` to send a base-model query, or prefix with /get for retrieval.",
            ],
            canonical_examples=[
                f"pi {raw}",
                f"/get {raw}",
            ],
        )
    if head in TOP_LEVEL_DIRECTIVES:
        return _explain_known_directive(parser, head, tokens, error)
    return _explain_unknown_directive(parser, head, error)


def _explain_known_directive(
    parser: DirectiveParser,
    head: str,
    tokens: list[str],
    error: str,
) -> DirectiveHelp:
    if head in AGENT_QUERY_ALIASES:
        return DirectiveHelp(
            summary=f"{head} requires a question to ask the harness.",
            suggestions=[
                f"{head} <your question>, in plain prose; the whole tail is the question.",
                "The answer comes back to the terminal and stays in the harness's own "
                "transcript, so no worktree or merge gate is involved.",
            ],
            canonical_examples=[
                "/claude explain how the ledger records a dispatch",
                "/codex what does resolve_transcript_pointer guarantee?",
            ],
        )
    if head in {"/store", "/embed"} and len(tokens) <= 1:
        return DirectiveHelp(
            summary=f"{head} requires a local file or directory path.",
            suggestions=[
                f"{head} /absolute/path/to/dir",
                f"/start {head} /absolute/path/to/dir",
                f"{head} /remote /absolute/path/to/dir",
            ],
            canonical_examples=[
                "/store ~/Documents/notes",
                "/start /store ~/ai_projects/ai_stack_local/output",
                "/embed ~/Pictures/screenshots",
            ],
        )
    if head in {"/ocr", "/hard-ocr"}:
        return DirectiveHelp(
            summary=f"{head} requires exactly one absolute image or directory path.",
            suggestions=[
                f"{head} /absolute/path/to/image.png",
                f"{head} /absolute/path/to/image-repository",
            ],
            canonical_examples=[
                f"/start {head}",
                f"{head} /absolute/path/to/image-repository",
            ],
        )
    if head == "/start" and len(tokens) >= 3 and tokens[1] == "/store":
        return DirectiveHelp(
            summary="/start /store still needs a local path after the /store flag.",
            suggestions=[
                "/start /store /absolute/path/to/dir",
                "/start /store /remote /absolute/path/to/dir",
            ],
            canonical_examples=[
                "/start /store ~/Documents/notes",
            ],
        )
    if head == "/screenshot" and len(tokens) <= 1:
        return DirectiveHelp(
            summary="/screenshot requires the path to an image file.",
            suggestions=[
                "/screenshot /absolute/path/to/image.png",
                '/screenshot /absolute/path/to/image.png "what is in this image?"',
            ],
            canonical_examples=[
                '/screenshot ~/Pictures/screen.png "summarize this"',
            ],
        )
    if head in {"/start", "/stop"} and len(tokens) >= 2:
        unknown = tokens[1]
        return _suggest_alias(parser, head, unknown, error)
    if head == "/get" and len(tokens) <= 1:
        return DirectiveHelp(
            summary="/get expects a query string after the directive.",
            suggestions=[
                "/get workflowy durable boundary",
                '/get "what owns workflow truth?"',
            ],
            canonical_examples=[
                "/get workflowy durable boundary",
            ],
        )
    if head == "/fetch":
        return DirectiveHelp(
            summary="/fetch requires a supported indexed source and a query.",
            suggestions=[
                "/fetch /workflowy give me the first idea bullet under /ideas",
                "/fetch /workflowy what did I write about agent orchestration?",
            ],
            canonical_examples=[
                "/fetch /workflowy give me the first idea bullet under /ideas",
            ],
        )
    if head == "/timer" and len(tokens) <= 1:
        return DirectiveHelp(
            summary="/timer expects a duration after the directive.",
            suggestions=[
                "/timer 50",
                "/timer 25m",
                "/timer 90s",
            ],
            canonical_examples=[
                "/timer 50",
            ],
        )
    if head == "/done" and len(tokens) <= 1:
        return DirectiveHelp(
            summary="/done expects a search query after the directive.",
            suggestions=[
                "/done embeddings durability",
                '/done "what owns workflow truth?"',
            ],
            canonical_examples=[
                "/done embeddings durability",
            ],
        )
    if head == "/send-to-wf":
        return DirectiveHelp(
            summary=("/send-to-wf needs a file path and a final MM/DD argument."),
            suggestions=[
                "/send-to-wf ~/Documents/note.txt 04/28",
                "/send-to-wf ~/Pictures/screen.png 12/03",
                "/send-to-wf ~/Recordings/voice.mp3 11/15",
            ],
            canonical_examples=[
                "/send-to-wf ~/Documents/note.txt 04/28",
            ],
        )
    if head == "/chrome":
        return DirectiveHelp(
            summary="/chrome controls Chrome tabs and pages through Chrome DevTools MCP.",
            suggestions=[
                "/chrome list",
                "/chrome start tabs",
                "/chrome start isolated",
                "/chrome status",
                "/chrome open https://example.com",
                "/chrome gather docs",
                "/chrome read docs",
                '/chrome decide docs --prompt "which can I close?"',
                "/chrome summarize docs",
                "/chrome close-category docs --yes",
                "/chrome navigate 1 https://example.com",
                "/chrome close 1",
            ],
            canonical_examples=[
                "/chrome list",
                "/chrome open https://example.com",
                "/chrome read docs",
                '/chrome decide docs --prompt "which can I close?"',
                "/chrome summarize docs",
                '/chrome eval 1 "() => document.title"',
            ],
        )
    if (
        "does not exist" in error.lower()
        or "no supported" in error.lower()
        or "existing file or directory" in error.lower()
    ):
        path = " ".join(tokens[1:]) or "(no path)"
        return DirectiveHelp(
            summary=f"{head} could not find usable content at: {path}",
            suggestions=[
                "Confirm the path exists and is readable.",
                "For directories, check that text or image files match the configured extensions.",
                "Use /start /store with the same path to embed and inspect what was discovered.",
            ],
            canonical_examples=[
                "/store ~/Documents/notes",
                "/start /store ~/Pictures/screenshots",
            ],
        )
    return DirectiveHelp(
        summary=f"Pi could not interpret {head} with the remaining arguments.",
        suggestions=[
            f"Run `{head}` with no arguments to see the default behavior.",
            "Top-level: /start, /stop, /get, /fetch, /compact, /timer, /store, /embed, "
            "/screenshot, /chrome.",
        ],
        canonical_examples=list(CANONICAL_EXAMPLES),
    )


def _explain_unknown_directive(
    parser: DirectiveParser,
    head: str,
    error: str,
) -> DirectiveHelp:
    candidates = sorted(TOP_LEVEL_DIRECTIVES | set(parser.aliases.keys()))
    matches = get_close_matches(head, candidates, n=3, cutoff=0.45)
    suggestions: list[str] = []
    if matches:
        suggestions.append("Did you mean one of: " + ", ".join(matches) + "?")
    suggestions.append("Top-level directives: " + ", ".join(sorted(TOP_LEVEL_DIRECTIVES)))
    suggestions.append("Plain text without a leading slash is treated as a base-model query.")
    return DirectiveHelp(
        summary=f"{head} is not a recognized directive. {error}".strip(),
        suggestions=suggestions,
        canonical_examples=list(CANONICAL_EXAMPLES),
    )


def _suggest_alias(
    parser: DirectiveParser,
    head: str,
    unknown: str,
    error: str,
) -> DirectiveHelp:
    aliases = sorted(parser.aliases.keys())
    matches = get_close_matches(unknown, aliases, n=3, cutoff=0.4)
    models_dir = parser.settings.llama_models_dir.expanduser()
    local_models: list[str] = []
    if models_dir.exists():
        local_models = sorted(p.name for p in models_dir.iterdir() if p.is_dir())
    suggestions: list[str] = []
    if matches:
        suggestions.append(
            f"{head} understood, but {unknown} is unknown. Try: {', '.join(matches)}"
        )
    if local_models:
        suggestions.append(
            "Local model directories under ~/models that can be used after a slash: "
            + ", ".join(f"/{name}" for name in local_models[:8])
        )
    if not suggestions:
        suggestions.append(
            f"{head} accepts an alias from configs/directives.toml or a directory under "
            f"{models_dir}."
        )
    return DirectiveHelp(
        summary=(f"{head} {unknown} did not resolve to a known model role. {error}".strip()),
        suggestions=suggestions,
        canonical_examples=[
            "/start /qwen",
            "/start /gemma4",
            "/start /fallback",
            "/start /chandra",
            "/start /ocr",
            "/stop /gemma4",
        ],
    )


def help_payload(parser: DirectiveParser, raw: str, error: str) -> dict[str, Any]:
    """Compose a JSON-serializable help block for a failed directive."""

    return explain_failure(parser, raw, error).as_dict()


def looks_like_path(token: str) -> bool:
    return bool(token) and Path(token).expanduser().exists()

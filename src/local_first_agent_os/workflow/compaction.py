# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Context compaction data and pure transformation operations."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..contracts import (
    ModelRole,
)
from ..utils import estimate_tokens

logger = logging.getLogger(__name__)

QWEN_CONTEXT_WINDOW = 32768
HIGH_WATER_RATIO = 0.90
TARGET_RATIO = 0.50
HIGH_WATER_TOKENS = 29491
TARGET_TOKENS = 16384
KEEP_LAST_N_EXCHANGES = 10

COMPACTION_MEMORY_KEYS = [
    "durable_facts",
    "user_preferences",
    "current_decisions",
    "unresolved_questions",
    "commands_configs_paths",
    "constraints",
    "recent_conversation_state",
    "exact_snippets_to_preserve",
    "failed_attempts_still_relevant",
    "image_state",
    "tool_state",
    "discarded_as_redundant",
]


def _empty_compaction_memory() -> dict[str, Any]:
    return {key: [] for key in COMPACTION_MEMORY_KEYS}


def _normalize_compaction_memory(memory: dict[str, Any]) -> dict[str, Any]:
    normalized = _empty_compaction_memory()
    for key in COMPACTION_MEMORY_KEYS:
        value = memory.get(key, [])
        if value is None:
            items: list[Any] = []
        elif isinstance(value, list):
            items = value
        else:
            items = [value]
        deduped = []
        seen = set()
        for item in items:
            marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(item)
        normalized[key] = deduped
    return normalized


def _format_memory_items(items: list[Any]) -> str:
    if not items:
        return "(none)"
    lines = []
    for item in items:
        if isinstance(item, (dict, list)):
            body = json.dumps(item, sort_keys=True, ensure_ascii=False)
        else:
            body = str(item)
        lines.append(f"- {body}")
    return "\n".join(lines)


def build_compacted_context(
    memory: dict[str, Any], raw_tail: str, old_user_prompts: str = ""
) -> str:
    normalized = _normalize_compaction_memory(memory)
    sections = [
        ("Durable facts", "durable_facts"),
        ("User preferences", "user_preferences"),
        ("Current decisions", "current_decisions"),
        ("Unresolved questions", "unresolved_questions"),
        ("Commands, configs, paths, filenames", "commands_configs_paths"),
        ("Constraints", "constraints"),
        ("Recent conversation state", "recent_conversation_state"),
        ("Exact snippets to preserve", "exact_snippets_to_preserve"),
        ("Failed attempts still relevant", "failed_attempts_still_relevant"),
        ("Image state", "image_state"),
        ("Tool state", "tool_state"),
    ]
    parts = ["# Compact Memory"]
    for title, key in sections:
        parts.append(f"## {title}")
        parts.append(_format_memory_items(normalized[key]))
    if old_user_prompts.strip():
        parts.append("# User Prompts (verbatim)")
        parts.append(old_user_prompts)
    parts.append("# Recent Raw Tail")
    parts.append(raw_tail)
    return "\n\n".join(parts)


def parse_model_json_object(output: dict[str, Any]) -> dict[str, Any]:
    if all(key in output for key in COMPACTION_MEMORY_KEYS[:3]):
        return _normalize_compaction_memory(output)
    content = output.get("text")
    if not isinstance(content, str):
        raise ValueError("Compactor output did not contain a JSON object or text JSON.")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Compactor text output did not contain JSON.")
        stripped = stripped[start : end + 1]
    loaded = json.loads(stripped)
    if not isinstance(loaded, dict):
        raise ValueError("Compactor JSON output is not an object.")
    return _normalize_compaction_memory(loaded)


def build_context_compaction_payload(
    *,
    directive: str,
    status: str,
    compactor_model_id: str,
    original_token_count: int,
    compacted_token_count: int,
    max_window_tokens: int,
    threshold_ratio: float,
    target_ratio: float,
    raw_tail_token_count: int,
    total_exchanges: int,
    compacted_exchanges: int,
    structured_memory: dict[str, Any] | None = None,
    compacted_context: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": "context_compaction.v2",
        "directive": directive,
        "status": status,
        "model_role": ModelRole.COMPACTOR.value,
        "model_id": compactor_model_id,
        "original_token_count": original_token_count,
        "compacted_token_count": compacted_token_count,
        "max_window_tokens": max_window_tokens,
        "threshold_ratio": threshold_ratio,
        "target_ratio": target_ratio,
        "raw_tail_token_count": raw_tail_token_count,
        "total_exchanges": total_exchanges,
        "compacted_exchanges": compacted_exchanges,
        "structured_memory": structured_memory or {},
        "compacted_context": compacted_context,
    }


def prune_memory_to_token_target(
    memory: dict[str, Any],
    raw_tail: str,
    target_tokens: int,
    old_user_prompts: str = "",
) -> dict[str, Any]:
    pruned = _normalize_compaction_memory(memory)
    prune_order = [
        "recent_conversation_state",
        "failed_attempts_still_relevant",
        "tool_state",
        "image_state",
        "durable_facts",
        "user_preferences",
        "current_decisions",
        "unresolved_questions",
        "commands_configs_paths",
        "constraints",
        "exact_snippets_to_preserve",
    ]
    while (
        estimate_tokens(build_compacted_context(pruned, raw_tail, old_user_prompts)) > target_tokens
    ):
        changed = False
        for key in prune_order:
            items = pruned.get(key, [])
            if items:
                pruned[key] = items[:-1]
                changed = True
                break
        if not changed:
            break
    return pruned

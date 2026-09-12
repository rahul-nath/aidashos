# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded, redacted events emitted by an asynchronously supervised process."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .constants import (
    AGENT_EVENT_MALFORMED_TEXT_LIMIT,
    AGENT_EVENT_MAX_LINE_BYTES,
    AGENT_EVENT_MAX_PAYLOAD_BYTES,
    AGENT_EVENT_TRANSCRIPT_TEXT_LIMIT,
    AGENT_EVENT_VALUE_MAX_DEPTH,
    AGENT_EVENT_VALUE_MAX_ITEMS,
)
from .contracts import ArtifactRef
from .staffing import FrontierHarness

_SENSITIVE_KEY = re.compile(
    r"(^|_)(thinking|reasoning|chain_of_thought|secret|password|token|credential|api_key)($|_)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(r"(?i)(api[_-]?key|password|secret|bearer)\s*[:=]\s*[^\s,;]+")


class ProcessEventSource(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    LIFECYCLE = "lifecycle"


class ExecutionArtifactStore(Protocol):
    """The artifact operations required by supervised execution."""

    def write_text(
        self,
        *,
        role: str,
        text: str,
        workflow_id: str | None,
        schema_version: str,
        mime_type: str = "text/plain",
    ) -> ArtifactRef: ...

    def read_text(self, artifact_id: str) -> str: ...


@dataclass(frozen=True)
class ExecutionStreamEvent:
    lease_id: str
    sequence: int
    occurred_at: float
    source: ProcessEventSource
    kind: str
    payload: dict[str, object]
    payload_sha256: str


def redact_execution_text(
    value: str,
    *,
    limit: int = AGENT_EVENT_TRANSCRIPT_TEXT_LIMIT,
) -> str:
    clean = _SENSITIVE_TEXT.sub(r"\1=[REDACTED]", value)
    return clean if len(clean) <= limit else f"{clean[:limit]}…[truncated]"


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > AGENT_EVENT_VALUE_MAX_DEPTH:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _SENSITIVE_KEY.search(key):
                safe[key] = "[REDACTED]"
            elif key == "content" and isinstance(item, list):
                safe[key] = [
                    _safe_value(block, depth=depth + 1)
                    for block in item
                    if not (
                        isinstance(block, Mapping)
                        and str(block.get("type") or "").lower()
                        in {"thinking", "reasoning", "analysis"}
                    )
                ]
            else:
                safe[key] = _safe_value(item, depth=depth + 1)
        return safe
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:AGENT_EVENT_VALUE_MAX_ITEMS]]
    if isinstance(value, str):
        return redact_execution_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_execution_text(str(value))


def _event_kind(harness: str, payload: Mapping[str, Any]) -> str:
    raw = payload.get("type") or payload.get("event") or payload.get("kind")
    kind = str(raw or "unknown")
    if harness == FrontierHarness.CODEX.value and kind.startswith("item."):
        item = payload.get("item")
        if isinstance(item, Mapping) and item.get("type"):
            return f"{kind}:{item['type']}"
    if harness == FrontierHarness.CLAUDE.value and kind == "assistant":
        return "assistant.message"
    return kind


def normalize_jsonl_line(
    *,
    harness: str,
    source: ProcessEventSource | str,
    line: bytes,
) -> tuple[str, dict[str, object]]:
    """Normalize one process line without retaining private reasoning."""

    source = ProcessEventSource(source)
    raw_hash = hashlib.sha256(line).hexdigest()
    if len(line) > AGENT_EVENT_MAX_LINE_BYTES:
        return "oversized", {
            "raw_sha256": raw_hash,
            "size_bytes": len(line),
            "omitted": True,
        }
    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
    if source is ProcessEventSource.STDERR:
        return "stderr", {"text": redact_execution_text(text), "raw_sha256": raw_hash}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", {
            "raw_sha256": raw_hash,
            "text": redact_execution_text(text, limit=AGENT_EVENT_MALFORMED_TEXT_LIMIT),
            "malformed_json": True,
        }
    if not isinstance(decoded, Mapping):
        return "unknown", {"raw_sha256": raw_hash, "value": _safe_value(decoded)}
    payload = _safe_value(decoded)
    if not isinstance(payload, dict):
        raise RuntimeError("mapping event normalization did not produce a dictionary")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > AGENT_EVENT_MAX_PAYLOAD_BYTES:
        return "oversized", {
            "raw_sha256": raw_hash,
            "normalized_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "size_bytes": len(canonical.encode("utf-8")),
            "omitted": True,
        }
    return _event_kind(harness, decoded), payload


def execution_payload_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def has_meaningful_agent_progress(source: ProcessEventSource | str, kind: str) -> bool:
    """Classify visible process output; liveness noise is deliberately excluded."""

    if ProcessEventSource(source) is not ProcessEventSource.STDOUT:
        return False
    normalized = kind.casefold()
    return not any(
        marker in normalized
        for marker in (
            "heartbeat",
            "warning",
            "keepalive",
            "rate_limit",
            "oversized",
            "unknown",
        )
    )


__all__ = [
    "ExecutionArtifactStore",
    "ExecutionStreamEvent",
    "ProcessEventSource",
    "execution_payload_hash",
    "has_meaningful_agent_progress",
    "normalize_jsonl_line",
    "redact_execution_text",
]

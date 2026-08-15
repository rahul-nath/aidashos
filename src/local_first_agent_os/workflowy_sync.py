#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
workflowy_sync.py — fetch a full Workflowy API snapshot and emit metadata-aware chunks.

Reads the Workflowy API JSON export (the `/api/v1/nodes-export` endpoint),
builds the outline tree, applies the semantic chunker, and writes a chunk JSONL
ready for `local-agent workflowy-import-chunks`.

Usage:
  WF_API_KEY=... uv run python -m local_first_agent_os.workflowy_sync
  uv run python -m local_first_agent_os.workflowy_sync --input data/workflowy/raw/latest.json
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from . import workflowy_chunking as rules

WORKFLOWY_EXPORT_URL = "https://workflowy.com/api/v1/nodes-export"
DEFAULT_API_KEY_ENV = "WF_API_KEY"
DEFAULT_RAW_OUTPUT = Path("data/workflowy/raw/latest.json")
DEFAULT_NORMALIZED_OUTPUT = Path("data/workflowy/normalized/latest.json")
DEFAULT_CHUNKS_JSONL_OUTPUT = Path("data/seed/workflowy_chunks_with_meta.jsonl")
DEFAULT_CHUNKS_JSON_OUTPUT = Path("data/seed/workflowy_chunks.json")

ANCHOR_RE = re.compile(
    r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
BLOCK_TAG_RE = re.compile(r"</?(?:p|div|h[1-6]|ul|ol|li)\b[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_parent_dir(path)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_snapshot(api_key: str, timeout_seconds: int = 300) -> dict[str, Any]:
    response = httpx.get(
        WORKFLOWY_EXPORT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def strip_html_tags(text: str) -> str:
    text = BR_RE.sub("\n", text)
    text = LI_OPEN_RE.sub("- ", text)
    text = BLOCK_TAG_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    return text


def clean_rich_text(value: Any) -> str:
    if value is None:
        return ""

    text = html.unescape(str(value))

    def replace_anchor(match: re.Match[str]) -> str:
        href = html.unescape(match.group(1)).strip()
        label = strip_html_tags(html.unescape(match.group(2))).strip()
        if label:
            return f"[{label}]({href})"
        return href

    text = ANCHOR_RE.sub(replace_anchor, text)
    text = strip_html_tags(text)
    text = text.replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_parent_id(value: Any) -> str | None:
    if value in (None, "", "None", "null"):
        return None
    return str(value)


def coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def node_sort_key(node: rules.WorkflowyNode) -> tuple[int, int, int, str]:
    return (
        node.priority if node.priority is not None else 10**9,
        node.created_at if node.created_at is not None else 10**9,
        node.line_no,
        rules.normalize_text(node.text).lower(),
    )


def normalize_api_node(raw_node: dict[str, Any], line_no: int) -> rules.WorkflowyNode:
    raw_data = raw_node.get("data")
    node_data = raw_data if isinstance(raw_data, dict) else {}
    layout_mode = node_data.get("layoutMode")
    return rules.WorkflowyNode(
        text=clean_rich_text(raw_node.get("name")),
        line_no=line_no,
        note=clean_rich_text(raw_node.get("note")),
        node_id=str(raw_node.get("id")) if raw_node.get("id") else None,
        parent_id=clean_parent_id(raw_node.get("parent_id")),
        priority=coerce_int(raw_node.get("priority")),
        created_at=coerce_int(raw_node.get("createdAt")),
        modified_at=coerce_int(raw_node.get("modifiedAt")),
        completed_at=coerce_int(raw_node.get("completedAt")),
        layout_mode=str(layout_mode) if layout_mode else None,
    )


def build_workflowy_tree(raw_nodes: list[dict[str, Any]]) -> rules.WorkflowyNode:
    normalized_nodes = [
        normalize_api_node(raw_node, line_no=index) for index, raw_node in enumerate(raw_nodes, 1)
    ]
    node_by_id = {node.node_id: node for node in normalized_nodes if node.node_id is not None}

    roots: list[rules.WorkflowyNode] = []
    for node in normalized_nodes:
        if node.parent_id and node.parent_id in node_by_id:
            node_by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)

    for node in normalized_nodes:
        node.children.sort(key=node_sort_key)
    roots.sort(key=node_sort_key)

    return rules.WorkflowyNode(text="__root__", children=roots)


def serialize_node(node: rules.WorkflowyNode) -> dict[str, Any]:
    return {
        "id": node.node_id,
        "parent_id": node.parent_id,
        "text": node.text,
        "note": node.note,
        "priority": node.priority,
        "created_at": node.created_at,
        "created_at_iso": rules.timestamp_to_iso(node.created_at),
        "modified_at": node.modified_at,
        "modified_at_iso": rules.timestamp_to_iso(node.modified_at),
        "completed_at": node.completed_at,
        "completed_at_iso": rules.timestamp_to_iso(node.completed_at),
        "layout_mode": node.layout_mode,
        "child_count": len(node.children),
        "children": [serialize_node(child) for child in node.children],
    }


def workflowy_tree_to_payload(root: rules.WorkflowyNode) -> dict[str, Any]:
    return {
        "children": [serialize_node(child) for child in rules.nonempty_children(root)],
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_parent_dir(path)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def count_nodes(root: rules.WorkflowyNode) -> int:
    total = 0
    for node in rules.nonempty_children(root):
        total += 1
        total += count_nodes(node)
    return total


def summarize_snapshot(
    raw_nodes: list[dict[str, Any]], root: rules.WorkflowyNode
) -> dict[str, int]:
    completed_nodes = sum(
        1 for raw_node in raw_nodes if raw_node.get("completedAt") not in (None, "")
    )
    note_nodes = sum(1 for raw_node in raw_nodes if clean_rich_text(raw_node.get("note")))
    return {
        "raw_nodes": len(raw_nodes),
        "tree_nodes": count_nodes(root),
        "top_level_nodes": len(rules.nonempty_children(root)),
        "completed_nodes": completed_nodes,
        "nodes_with_notes": note_nodes,
    }


def sync_workflowy(
    *,
    input_path: Path | None = None,
    raw_output: Path = DEFAULT_RAW_OUTPUT,
    normalized_output: Path = DEFAULT_NORMALIZED_OUTPUT,
    chunks_output: Path = DEFAULT_CHUNKS_JSONL_OUTPUT,
    chunks_json_output: Path = DEFAULT_CHUNKS_JSON_OUTPUT,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    max_chars: int = 1200,
) -> int:
    """Fetch (or load) a Workflowy snapshot, apply the semantic chunker, and
    write the chunk files. Returns the number of chunks produced."""
    if input_path is not None:
        snapshot = load_snapshot(input_path)
        input_label = str(input_path)
    else:
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise SystemExit(f"Missing Workflowy API key in environment variable {api_key_env}.")
        snapshot = fetch_snapshot(api_key)
        input_label = WORKFLOWY_EXPORT_URL

    raw_nodes = snapshot.get("nodes")
    if not isinstance(raw_nodes, list):
        raise SystemExit("Workflowy export did not contain a top-level 'nodes' array.")

    write_json(raw_output, snapshot)

    root = build_workflowy_tree(raw_nodes)
    write_json(normalized_output, workflowy_tree_to_payload(root))

    chunks = rules.chunk_workflowy_tree(root, max_chars=max_chars)
    chunk_payloads = rules.chunks_to_payloads(chunks)
    write_jsonl(chunks_output, chunk_payloads)
    write_json(chunks_json_output, chunk_payloads)

    summary = summarize_snapshot(raw_nodes, root)
    print(f"Workflowy source: {input_label}")
    print(
        "Nodes: "
        f"{summary['raw_nodes']} raw, "
        f"{summary['tree_nodes']} normalized, "
        f"{summary['top_level_nodes']} top-level, "
        f"{summary['completed_nodes']} completed, "
        f"{summary['nodes_with_notes']} with notes"
    )
    print(f"Chunks: {len(chunks)} -> {chunks_output}")
    return len(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Workflowy's full API export and emit metadata-aware chunk files.",
    )
    parser.add_argument(
        "--input",
        help="Existing Workflowy export JSON to read instead of fetching from the API.",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"Env var holding the Workflowy API key (default: {DEFAULT_API_KEY_ENV})",
    )
    parser.add_argument(
        "--raw-output",
        default=str(DEFAULT_RAW_OUTPUT),
        help=f"Raw snapshot output path (default: {DEFAULT_RAW_OUTPUT})",
    )
    parser.add_argument(
        "--normalized-output",
        default=str(DEFAULT_NORMALIZED_OUTPUT),
        help=f"Normalized tree output path (default: {DEFAULT_NORMALIZED_OUTPUT})",
    )
    parser.add_argument(
        "--chunks-output",
        default=str(DEFAULT_CHUNKS_JSONL_OUTPUT),
        help=f"Chunk JSONL output path (default: {DEFAULT_CHUNKS_JSONL_OUTPUT})",
    )
    parser.add_argument(
        "--chunks-json-output",
        default=str(DEFAULT_CHUNKS_JSON_OUTPUT),
        help=f"Chunk JSON array output path (default: {DEFAULT_CHUNKS_JSON_OUTPUT})",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1200,
        help="Base chunk size target before recursive splitting (default: 1200)",
    )
    args = parser.parse_args()
    sync_workflowy(
        input_path=Path(args.input) if args.input else None,
        raw_output=Path(args.raw_output),
        normalized_output=Path(args.normalized_output),
        chunks_output=Path(args.chunks_output),
        chunks_json_output=Path(args.chunks_json_output),
        api_key_env=args.api_key_env,
        max_chars=args.max_chars,
    )


if __name__ == "__main__":
    main()

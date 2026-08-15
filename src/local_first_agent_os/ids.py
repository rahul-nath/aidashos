# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import CHUNKER_VERSION, WORKFLOW_VERSION, SourceType, WorkflowType


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_text(canonical)


def build_event_id(
    source_type: SourceType | str,
    workspace_id: str,
    source_uri: str,
    event_type: str,
    content_sha256: str | None,
) -> str:
    hash_part = content_sha256 or sha256_text(source_uri)
    return f"{source_type}:{workspace_id}:{source_uri}:{event_type}:sha256:{hash_part}"


def build_workflow_id(
    workflow_type: WorkflowType | str,
    workspace_id: str,
    source_type: SourceType | str,
    content_sha256: str,
    workflow_version: str = WORKFLOW_VERSION,
) -> str:
    return f"{workflow_type}:{workspace_id}:{source_type}:{content_sha256}:{workflow_version}"


def build_artifact_id(role: str, sha256: str) -> str:
    return f"artifact:{role}:{sha256[:32]}"


def build_invocation_id(workflow_id: str, model_role: str, input_sha256: str, params: Any) -> str:
    params_hash = canonical_json_hash(params)
    return f"model:{sha256_text(f'{workflow_id}:{model_role}:{input_sha256}:{params_hash}')[:32]}"


def build_pi_turn_id(workflow_id: str, task_type: str, prompt_sha256: str) -> str:
    return f"pi:{sha256_text(f'{workflow_id}:{task_type}:{prompt_sha256}')[:32]}"


def build_session_item_id(turn_id: str, ordinal: int) -> str:
    return f"session-item:{sha256_text(f'{turn_id}:{ordinal}')[:32]}"


def build_tool_call_id(workflow_id: str, tool_name: str, input_payload: Any) -> str:
    seed = f"{workflow_id}:{tool_name}:{canonical_json_hash(input_payload)}"
    return f"tool:{sha256_text(seed)[:32]}"


def build_chunk_id(
    artifact_sha256: str,
    chunk_index: int,
    embedding_model_id: str,
    chunker_version: str = CHUNKER_VERSION,
) -> str:
    return sha256_text(f"{artifact_sha256}:{chunk_index}:{chunker_version}:{embedding_model_id}")


def build_egress_id(egress_type: str, destination_uri: str, content_sha256: str) -> str:
    return f"egress:{sha256_text(f'{egress_type}:{destination_uri}:{content_sha256}')[:32]}"


def build_graph_node_id(node_type: str, normalized_name: str) -> str:
    """Stable identity for a graph node.

    Keying on `(node_type, normalized_name)` is what makes the merge idempotent
    and what scopes resolution to a single type: two entities of different types
    can never collide however similar their names.
    """
    return f"node:{sha256_text(f'{node_type}:{normalized_name}')[:32]}"


def build_graph_edge_id(src_node_id: str, edge_type: str, dst_node_id: str) -> str:
    """Stable identity for a directed, typed edge.

    Direction is part of the key, so asserting the reverse relation creates a
    second edge rather than thickening the first.
    """
    return f"edge:{sha256_text(f'{src_node_id}:{edge_type}:{dst_node_id}')[:32]}"


def build_graph_mention_id(node_id: str, artifact_id: str, chunk_id: str | None) -> str:
    return f"mention:{sha256_text(f'{node_id}:{artifact_id}:{chunk_id or ""}')[:32]}"


def build_graph_extraction_workflow_id(
    artifact_id: str,
    ontology_version: str,
    extractor_model_id: str,
) -> str:
    """Idempotency key for one artifact's extraction.

    The ontology version is in the hash on purpose: bumping it is how the
    operator asks for a clean re-derivation rather than a deduplicated no-op.
    """
    seed = f"{artifact_id}:{ontology_version}:{extractor_model_id}"
    return f"{WorkflowType.GRAPH_EXTRACTION.value}:{sha256_text(seed)[:32]}:{WORKFLOW_VERSION}"

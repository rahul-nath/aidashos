# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BULLET_RE = re.compile(r"^( *)([-*+]) (.*)$")
NOTE_RE = re.compile(r"^( *)(>) ?(.*)$")
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}$")
RESULTS_SEPARATOR_RE = re.compile(r"^\={80}\s*$")
THERAPY_SESSION_RE = re.compile(
    r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?(?:\s+\d{1,2}(?::\d{2})?(?:am|pm))?(?:\s+.*)?$",
    re.IGNORECASE,
)


@dataclass
class WorkflowyNode:
    text: str
    line_no: int = 0
    note: str | None = None
    node_id: str | None = None
    parent_id: str | None = None
    priority: int | None = None
    created_at: int | None = None
    modified_at: int | None = None
    completed_at: int | None = None
    layout_mode: str | None = None
    children: list[WorkflowyNode] = field(default_factory=list)


@dataclass
class WorkflowyRenderedChunk:
    headings: list[str]
    text: str
    context_text: str
    node_ids: list[str] = field(default_factory=list)
    parent_ids: list[str] = field(default_factory=list)
    path_titles: list[str] = field(default_factory=list)
    root_node_id: str | None = None
    created_at_min: int | None = None
    created_at_max: int | None = None
    modified_at_max: int | None = None
    priority_min: int | None = None
    priority_max: int | None = None
    layout_modes: list[str] = field(default_factory=list)
    has_notes: bool = False
    node_count: int = 0


@dataclass
class ChunkingState:
    base_max_chars: int
    longest_single_bullet_chars: int = 0

    @property
    def current_max_chars(self) -> int:
        return max(self.base_max_chars, self.longest_single_bullet_chars)

    def observe_single_bullet_chunk(self, chunk: WorkflowyRenderedChunk) -> None:
        self.longest_single_bullet_chars = max(
            self.longest_single_bullet_chars,
            len(chunk.text),
        )


@dataclass(frozen=True)
class WorkflowyRule:
    name: str
    matches: Callable[[WorkflowyNode], bool]
    apply: Callable[[WorkflowyNode, ChunkingState], list[WorkflowyRenderedChunk]]


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip().replace("\u00a0", " ")
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_note(note: str | None) -> str:
    if not note:
        return ""
    note = note.replace("\u00a0", " ")
    note = note.replace("\r\n", "\n").replace("\r", "\n")
    note = "\n".join(line.rstrip() for line in note.splitlines())
    note = re.sub(r"\n{3,}", "\n\n", note)
    return note.strip()


def canonical_text(text: str | None) -> str:
    return normalize_text(text).rstrip(":").lower()


def is_blank_text(text: str | None) -> bool:
    return normalize_text(text) == ""


def has_note(node: WorkflowyNode) -> bool:
    return normalize_note(node.note) != ""


def nonempty_children(node: WorkflowyNode) -> list[WorkflowyNode]:
    return [
        child
        for child in node.children
        if not (is_blank_text(child.text) and not has_note(child) and not nonempty_children(child))
    ]


def append_note_text(existing: str | None, line: str) -> str:
    if existing:
        return f"{existing}\n{line}"
    return line


def parse_workflowy_markdown(text: str) -> WorkflowyNode:
    root = WorkflowyNode(text="__root__", line_no=0)
    stack: list[tuple[int, WorkflowyNode]] = [(-1, root)]

    for line_no, line in enumerate(text.splitlines(), 1):
        match = BULLET_RE.match(line)
        if match:
            depth = len(match.group(1)) // 2
            node = WorkflowyNode(text=match.group(3), line_no=line_no)

            while stack and stack[-1][0] >= depth:
                stack.pop()

            stack[-1][1].children.append(node)
            stack.append((depth, node))
            continue

        note_match = NOTE_RE.match(line)
        if note_match:
            note_depth = max(0, len(note_match.group(1)) // 2 - 1)
            for stack_depth, stack_node in reversed(stack):
                if stack_depth == note_depth:
                    stack_node.note = append_note_text(stack_node.note, note_match.group(3))
                    break

    return root


def looks_like_workflowy_markdown(text: str) -> bool:
    for line in text.splitlines():
        if not line.strip():
            continue
        return bool(BULLET_RE.match(line))
    return False


def split_results_sections(text: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        if RESULTS_SEPARATOR_RE.fullmatch(line.strip()):
            section = "\n".join(current).strip()
            if section:
                sections.append(section)
            current = []
            continue
        current.append(line)

    trailing = "\n".join(current).strip()
    if trailing:
        sections.append(trailing)

    return sections


def extract_workflowy_section(text: str) -> str:
    sections = split_results_sections(text)
    if sections and looks_like_workflowy_markdown(sections[0]):
        return sections[0]
    if looks_like_workflowy_markdown(text):
        return text.strip()
    raise ValueError("Could not find a Workflowy markdown section in the input.")


def render_note_lines(note: str | None, depth: int) -> list[str]:
    normalized = normalize_note(note)
    if not normalized:
        return []

    lines: list[str] = []
    indent = "  " * depth
    for line in normalized.splitlines():
        if line:
            lines.append(f"{indent}> {line}")
        else:
            lines.append(f"{indent}>")
    return lines


def render_subtree(node: WorkflowyNode, depth: int) -> list[str]:
    children = nonempty_children(node)
    lines: list[str] = []

    if not is_blank_text(node.text):
        lines.append(f"{'  ' * depth}- {node.text}")
        lines.extend(render_note_lines(node.note, depth + 1))
        child_depth = depth + 1
    else:
        lines.extend(render_note_lines(node.note, depth))
        child_depth = depth

    for child in children:
        lines.extend(render_subtree(child, child_depth))

    return lines


def render_chunk(ancestor_chain: list[WorkflowyNode], body_nodes: list[WorkflowyNode]) -> str:
    lines: list[str] = []
    depth = 0

    for ancestor in ancestor_chain:
        if is_blank_text(ancestor.text):
            lines.extend(render_note_lines(ancestor.note, depth))
            continue

        lines.append(f"{'  ' * depth}- {ancestor.text}")
        lines.extend(render_note_lines(ancestor.note, depth + 1))
        depth += 1

    for node in body_nodes:
        lines.extend(render_subtree(node, depth))

    return "\n".join(lines).strip()


def build_headings(
    ancestor_chain: list[WorkflowyNode],
    body_nodes: list[WorkflowyNode],
) -> list[str]:
    headings = [normalize_text(node.text) for node in ancestor_chain if normalize_text(node.text)]
    body_labels = [normalize_text(node.text) for node in body_nodes if normalize_text(node.text)]

    if not body_labels:
        return headings
    if len(body_labels) == 1:
        return headings + body_labels
    return headings + [f"{body_labels[0]} .. {body_labels[-1]}"]


def iter_subtree_nodes(node: WorkflowyNode) -> list[WorkflowyNode]:
    nodes = [node]
    for child in nonempty_children(node):
        nodes.extend(iter_subtree_nodes(child))
    return nodes


def collect_chunk_nodes(
    ancestor_chain: list[WorkflowyNode],
    body_nodes: list[WorkflowyNode],
) -> list[WorkflowyNode]:
    collected: list[WorkflowyNode] = []
    seen: set[int] = set()

    for node in ancestor_chain:
        node_key = id(node)
        if node_key in seen:
            continue
        seen.add(node_key)
        collected.append(node)

    for node in body_nodes:
        for descendant in iter_subtree_nodes(node):
            node_key = id(descendant)
            if node_key in seen:
                continue
            seen.add(node_key)
            collected.append(descendant)

    return collected


def unique_nonempty(values: list[str | int | None]) -> list[str | int]:
    seen: set[str | int] = set()
    unique: list[str | int] = []

    for value in values:
        if value in (None, ""):
            continue
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)

    return unique


def timestamp_to_iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def build_context_text(
    headings: list[str],
    text: str,
    created_at_min: int | None,
    created_at_max: int | None,
    modified_at_max: int | None,
    priority_min: int | None,
    priority_max: int | None,
) -> str:
    lines: list[str] = []

    if headings:
        lines.append(f"[Path] {' > '.join(headings)}")

    if created_at_min is not None:
        created_min_iso = timestamp_to_iso(created_at_min)
        created_max_iso = timestamp_to_iso(created_at_max)
        if created_at_max is not None and created_at_max != created_at_min and created_max_iso:
            lines.append(f"[Created range] {created_min_iso} .. {created_max_iso}")
        elif created_min_iso:
            lines.append(f"[Created] {created_min_iso}")

    modified_iso = timestamp_to_iso(modified_at_max)
    if modified_iso:
        lines.append(f"[Modified] {modified_iso}")

    if priority_min is not None:
        if priority_max is not None and priority_max != priority_min:
            lines.append(f"[Priority range] {priority_min}..{priority_max}")
        else:
            lines.append(f"[Priority] {priority_min}")

    if not lines:
        return text

    return "\n".join(lines) + "\n\n" + text


def make_chunk(
    ancestor_chain: list[WorkflowyNode],
    body_nodes: list[WorkflowyNode],
) -> WorkflowyRenderedChunk:
    headings = build_headings(ancestor_chain, body_nodes)
    text = render_chunk(ancestor_chain, body_nodes)
    chunk_nodes = collect_chunk_nodes(ancestor_chain, body_nodes)

    created_values = [node.created_at for node in chunk_nodes if node.created_at is not None]
    modified_values = [node.modified_at for node in chunk_nodes if node.modified_at is not None]
    priority_values = [node.priority for node in chunk_nodes if node.priority is not None]

    root_node: WorkflowyNode | None = None
    for node in ancestor_chain + body_nodes:
        if node.node_id:
            root_node = node
            break

    created_at_min = min(created_values) if created_values else None
    created_at_max = max(created_values) if created_values else None
    modified_at_max = max(modified_values) if modified_values else None
    priority_min = min(priority_values) if priority_values else None
    priority_max = max(priority_values) if priority_values else None

    context_text = build_context_text(
        headings=headings,
        text=text,
        created_at_min=created_at_min,
        created_at_max=created_at_max,
        modified_at_max=modified_at_max,
        priority_min=priority_min,
        priority_max=priority_max,
    )

    return WorkflowyRenderedChunk(
        headings=headings,
        text=text,
        context_text=context_text,
        node_ids=[str(value) for value in unique_nonempty([node.node_id for node in chunk_nodes])],
        parent_ids=[
            str(value) for value in unique_nonempty([node.parent_id for node in chunk_nodes])
        ],
        path_titles=headings,
        root_node_id=root_node.node_id if root_node else None,
        created_at_min=created_at_min,
        created_at_max=created_at_max,
        modified_at_max=modified_at_max,
        priority_min=priority_min,
        priority_max=priority_max,
        layout_modes=[
            str(value) for value in unique_nonempty([node.layout_mode for node in chunk_nodes])
        ],
        has_notes=any(has_note(node) for node in chunk_nodes),
        node_count=len(chunk_nodes),
    )


def chunk_size(ancestor_chain: list[WorkflowyNode], body_nodes: list[WorkflowyNode]) -> int:
    return len(render_chunk(ancestor_chain, body_nodes))


def emit_chunk(
    ancestor_chain: list[WorkflowyNode],
    body_nodes: list[WorkflowyNode],
    state: ChunkingState,
) -> WorkflowyRenderedChunk:
    chunk = make_chunk(ancestor_chain, body_nodes)
    if len(body_nodes) == 1:
        state.observe_single_bullet_chunk(chunk)
    return chunk


def split_by_children(
    ancestor_chain: list[WorkflowyNode],
    children: list[WorkflowyNode],
    state: ChunkingState,
) -> list[WorkflowyRenderedChunk]:
    chunks: list[WorkflowyRenderedChunk] = []
    current: list[WorkflowyNode] = []

    for child in children:
        child_size = chunk_size(ancestor_chain, [child])

        if child_size > state.current_max_chars and nonempty_children(child):
            if current:
                chunks.append(emit_chunk(ancestor_chain, current, state))
                current = []
            chunks.extend(split_node(child, ancestor_chain, state))
            continue

        if not current:
            current = [child]
            continue

        if chunk_size(ancestor_chain, current + [child]) <= state.current_max_chars:
            current.append(child)
        else:
            chunks.append(emit_chunk(ancestor_chain, current, state))
            current = [child]

    if current:
        chunks.append(emit_chunk(ancestor_chain, current, state))

    return chunks


def split_node(
    node: WorkflowyNode,
    ancestor_chain: list[WorkflowyNode],
    state: ChunkingState,
) -> list[WorkflowyRenderedChunk]:
    subtree_chunk = make_chunk(ancestor_chain, [node])
    children = nonempty_children(node)

    if len(subtree_chunk.text) <= state.current_max_chars or not children:
        state.observe_single_bullet_chunk(subtree_chunk)
        return [subtree_chunk]

    return split_by_children(ancestor_chain + [node], children, state)


def group_remaining_children(
    parent: WorkflowyNode,
    children: list[WorkflowyNode],
    state: ChunkingState,
) -> list[WorkflowyRenderedChunk]:
    if not children:
        return []
    return split_by_children([parent], children, state)


def is_top_level_date(node: WorkflowyNode) -> bool:
    return bool(DATE_RE.fullmatch(normalize_text(node.text)))


def is_top_level_label(node: WorkflowyNode, *labels: str) -> bool:
    return canonical_text(node.text) in {canonical_text(label) for label in labels}


def is_idea_bullet(node: WorkflowyNode) -> bool:
    return canonical_text(node.text).startswith("idea:")


def is_therapy_session(node: WorkflowyNode) -> bool:
    return bool(THERAPY_SESSION_RE.fullmatch(normalize_text(node.text)))


def chunk_date_rule(node: WorkflowyNode, state: ChunkingState) -> list[WorkflowyRenderedChunk]:
    return split_node(node, [], state)


def chunk_ideas_rule(node: WorkflowyNode, state: ChunkingState) -> list[WorkflowyRenderedChunk]:
    chunks: list[WorkflowyRenderedChunk] = []
    generic_children: list[WorkflowyNode] = []

    for child in nonempty_children(node):
        if is_idea_bullet(child):
            chunks.append(emit_chunk([node], [child], state))
        else:
            generic_children.append(child)

    chunks.extend(group_remaining_children(node, generic_children, state))
    return chunks


def chunk_journal_rule(node: WorkflowyNode, state: ChunkingState) -> list[WorkflowyRenderedChunk]:
    return [emit_chunk([node], [child], state) for child in nonempty_children(node)]


def chunk_keep_job_rule(
    jobs_node: WorkflowyNode,
    keep_job_node: WorkflowyNode,
    state: ChunkingState,
) -> list[WorkflowyRenderedChunk]:
    chunks: list[WorkflowyRenderedChunk] = []
    generic_children: list[WorkflowyNode] = []

    for child in nonempty_children(keep_job_node):
        if canonical_text(child.text) == canonical_text("Past company reflections"):
            for company in nonempty_children(child):
                chunks.extend(split_node(company, [jobs_node, keep_job_node, child], state))
        else:
            generic_children.append(child)

    if generic_children:
        chunks.extend(split_by_children([jobs_node, keep_job_node], generic_children, state))

    return chunks


def chunk_jobs_rule(node: WorkflowyNode, state: ChunkingState) -> list[WorkflowyRenderedChunk]:
    chunks: list[WorkflowyRenderedChunk] = []
    generic_children: list[WorkflowyNode] = []
    lifecycle_labels = {
        canonical_text("1. prepare"),
        canonical_text("2. connect"),
        canonical_text("3. apply + interview"),
        canonical_text("4. negotiate offer"),
    }

    for child in nonempty_children(node):
        child_label = canonical_text(child.text)
        if child_label in lifecycle_labels:
            chunks.extend(split_node(child, [node], state))
        elif child_label == canonical_text("5. keep the job"):
            chunks.extend(chunk_keep_job_rule(node, child, state))
        else:
            generic_children.append(child)

    chunks.extend(group_remaining_children(node, generic_children, state))
    return chunks


def chunk_health_rule(node: WorkflowyNode, state: ChunkingState) -> list[WorkflowyRenderedChunk]:
    chunks: list[WorkflowyRenderedChunk] = []
    generic_children: list[WorkflowyNode] = []

    for child in nonempty_children(node):
        child_label = canonical_text(child.text)

        if child_label == canonical_text("mental health daily"):
            chunks.extend(
                emit_chunk([node, child], [grandchild], state)
                for grandchild in nonempty_children(child)
            )
        elif child_label == canonical_text("therapy + coaching"):
            therapy_generic_children: list[WorkflowyNode] = []
            for session in nonempty_children(child):
                if is_therapy_session(session):
                    chunks.append(emit_chunk([node, child], [session], state))
                else:
                    therapy_generic_children.append(session)

            if therapy_generic_children:
                chunks.extend(split_by_children([node, child], therapy_generic_children, state))
        else:
            generic_children.append(child)

    chunks.extend(group_remaining_children(node, generic_children, state))
    return chunks


def chunk_generic_top_level_rule(
    node: WorkflowyNode,
    state: ChunkingState,
) -> list[WorkflowyRenderedChunk]:
    return split_node(node, [], state)


TOP_LEVEL_RULES: list[WorkflowyRule] = [
    WorkflowyRule(
        name="top-level-date",
        matches=is_top_level_date,
        apply=chunk_date_rule,
    ),
    WorkflowyRule(
        name="ideas",
        matches=lambda node: is_top_level_label(node, "/ideas"),
        apply=chunk_ideas_rule,
    ),
    WorkflowyRule(
        name="jobs",
        matches=lambda node: is_top_level_label(node, "/jobs"),
        apply=chunk_jobs_rule,
    ),
    WorkflowyRule(
        name="health",
        matches=lambda node: is_top_level_label(node, "/health"),
        apply=chunk_health_rule,
    ),
    WorkflowyRule(
        name="journal",
        matches=lambda node: is_top_level_label(node, "/journal"),
        apply=chunk_journal_rule,
    ),
]


def chunk_workflowy_tree(
    root: WorkflowyNode,
    max_chars: int,
) -> list[WorkflowyRenderedChunk]:
    chunks: list[WorkflowyRenderedChunk] = []
    shared_longest_single_bullet_chars = 0

    for top_level_node in nonempty_children(root):
        state = ChunkingState(
            base_max_chars=max_chars,
            longest_single_bullet_chars=shared_longest_single_bullet_chars,
        )

        for rule in TOP_LEVEL_RULES:
            if rule.matches(top_level_node):
                chunks.extend(rule.apply(top_level_node, state))
                break
        else:
            chunks.extend(chunk_generic_top_level_rule(top_level_node, state))

        shared_longest_single_bullet_chars = max(
            shared_longest_single_bullet_chars,
            state.longest_single_bullet_chars,
        )

    return chunks


def chunk_workflowy_section(
    text: str,
    max_chars: int,
) -> list[WorkflowyRenderedChunk]:
    return chunk_workflowy_tree(parse_workflowy_markdown(text), max_chars=max_chars)


def chunk_to_payload(chunk: WorkflowyRenderedChunk, idx: int) -> dict[str, object]:
    return {
        "chunk_idx": idx,
        "headings": chunk.headings,
        "path_titles": chunk.path_titles,
        "top_level": chunk.headings[0] if chunk.headings else "(root)",
        "char_count": len(chunk.text),
        "context_char_count": len(chunk.context_text),
        "text": chunk.text,
        "context_text": chunk.context_text,
        "node_ids": chunk.node_ids,
        "parent_ids": chunk.parent_ids,
        "root_node_id": chunk.root_node_id,
        "created_at_min": chunk.created_at_min,
        "created_at_min_iso": timestamp_to_iso(chunk.created_at_min),
        "created_at_max": chunk.created_at_max,
        "created_at_max_iso": timestamp_to_iso(chunk.created_at_max),
        "modified_at_max": chunk.modified_at_max,
        "modified_at_max_iso": timestamp_to_iso(chunk.modified_at_max),
        "priority_min": chunk.priority_min,
        "priority_max": chunk.priority_max,
        "layout_modes": chunk.layout_modes,
        "has_notes": chunk.has_notes,
        "node_count": chunk.node_count,
    }


def chunks_to_payloads(chunks: list[WorkflowyRenderedChunk]) -> list[dict[str, object]]:
    return [chunk_to_payload(chunk, idx) for idx, chunk in enumerate(chunks)]


def format_chunks_as_markdown(chunks: list[WorkflowyRenderedChunk]) -> str:
    rendered_blocks: list[str] = []

    for idx, chunk in enumerate(chunks):
        section = " > ".join(chunk.headings) if chunk.headings else "(root)"
        rendered_blocks.append(
            f"<!-- chunk {idx} | {len(chunk.text)} chars | {section} -->\n{chunk.text}"
        )

    return f"\n\n{('=' * 80)}\n\n".join(rendered_blocks)


def format_chunks_as_json(chunks: list[WorkflowyRenderedChunk]) -> str:
    return json.dumps(chunks_to_payloads(chunks), indent=2, ensure_ascii=False)


def format_chunks_as_jsonl(chunks: list[WorkflowyRenderedChunk]) -> str:
    return "\n".join(
        json.dumps(payload, ensure_ascii=False) for payload in chunks_to_payloads(chunks)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply Workflowy chunking rules to Workflowy markdown or RESULTS.md",
    )
    parser.add_argument(
        "input",
        help="Path to a Workflowy markdown export or output/RESULTS.md",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1200,
        help="Base chunk size target before recursive splitting (default: 1200)",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json", "jsonl"),
        default="json",
        help="Output format for rendered chunks (default: json)",
    )
    parser.add_argument(
        "--output",
        help="Optional output path. Defaults to stdout when omitted.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    text = input_path.read_text(encoding="utf-8", errors="replace")
    workflowy_text = extract_workflowy_section(text)
    chunks = chunk_workflowy_section(workflowy_text, max_chars=args.max_chars)

    if args.format == "markdown":
        rendered = format_chunks_as_markdown(chunks)
    elif args.format == "jsonl":
        rendered = format_chunks_as_jsonl(chunks)
    else:
        rendered = format_chunks_as_json(chunks)

    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote {len(chunks)} chunk(s) to {args.output}")
        return

    print(rendered)


if __name__ == "__main__":
    main()

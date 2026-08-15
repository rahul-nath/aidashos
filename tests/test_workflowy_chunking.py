# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Chunking is pure logic over an outline, so it is tested without any I/O.

These cover the parse-to-chunk pipeline that `workflowy_sync` and the
`workflowy-import-chunks` CLI both depend on. The invariant that matters most
is that chunking never loses a bullet: a chunk boundary may move, but text
that entered must come out.
"""

from __future__ import annotations

from local_first_agent_os import workflowy_chunking as chunking

NESTED_OUTLINE = """- Jobs
  - Apply to Acme
    > note line one
    > note line two
  - Apply to Beta
- Health
  - Sleep
"""


def _all_texts(node: chunking.WorkflowyNode) -> list[str]:
    texts = [] if node.text == "__root__" else [node.text]
    for child in node.children:
        texts.extend(_all_texts(child))
    return texts


def test_parse_nests_by_two_space_indent() -> None:
    root = chunking.parse_workflowy_markdown(NESTED_OUTLINE)

    assert [child.text for child in root.children] == ["Jobs", "Health"]
    assert [child.text for child in root.children[0].children] == [
        "Apply to Acme",
        "Apply to Beta",
    ]
    assert [child.text for child in root.children[1].children] == ["Sleep"]


def test_parse_attaches_multiline_notes_to_the_bullet_they_follow() -> None:
    root = chunking.parse_workflowy_markdown(NESTED_OUTLINE)
    acme = root.children[0].children[0]

    assert acme.note == "note line one\nnote line two"
    assert root.children[0].children[1].note is None
    assert root.children[0].note is None


def test_looks_like_workflowy_markdown_reads_the_first_nonblank_line() -> None:
    assert chunking.looks_like_workflowy_markdown(NESTED_OUTLINE) is True
    assert chunking.looks_like_workflowy_markdown("\n\n- a bullet") is True
    assert chunking.looks_like_workflowy_markdown("plain prose\n- a bullet") is False
    assert chunking.looks_like_workflowy_markdown("") is False


def test_each_top_level_bullet_becomes_its_own_chunk() -> None:
    chunks = chunking.chunk_workflowy_section(NESTED_OUTLINE, max_chars=200)

    assert [chunk.headings for chunk in chunks] == [["Jobs"], ["Health"]]
    assert chunks[0].node_count == 3
    assert chunks[0].has_notes is True
    assert chunks[1].node_count == 2
    assert chunks[1].has_notes is False


def test_chunking_never_drops_a_bullet() -> None:
    root = chunking.parse_workflowy_markdown(NESTED_OUTLINE)
    chunks = chunking.chunk_workflowy_section(NESTED_OUTLINE, max_chars=40)
    combined = "\n".join(chunk.text for chunk in chunks)

    for text in _all_texts(root):
        assert text in combined, text


def test_a_bullet_longer_than_max_chars_is_emitted_whole() -> None:
    oversized = "x" * 300
    outline = f"- Topic\n  - {oversized}\n  - small one\n- Second\n  - also small\n"

    chunks = chunking.chunk_workflowy_section(outline, max_chars=100)

    # The budget yields rather than truncating: a single bullet that cannot fit
    # is still emitted intact, because losing outline text is worse than
    # exceeding the size hint.
    assert any(oversized in chunk.text for chunk in chunks)
    assert max(len(chunk.text) for chunk in chunks) > 100


def test_oversized_children_split_into_several_chunks_that_keep_their_parent() -> None:
    outline = "- Parent\n" + "".join(f"  - child {index} {'y' * 40}\n" for index in range(10))

    chunks = chunking.chunk_workflowy_section(outline, max_chars=120)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.headings[0] == "Parent"
        assert chunk.path_titles[0] == "Parent"
    combined = "\n".join(chunk.text for chunk in chunks)
    for index in range(10):
        assert f"child {index}" in combined


def test_chunk_payload_reports_counts_and_iso_timestamps() -> None:
    chunks = chunking.chunk_workflowy_section(NESTED_OUTLINE, max_chars=200)
    chunks[0].created_at_min = 1700000000
    chunks[0].modified_at_max = None

    payload = chunking.chunk_to_payload(chunks[0], 7)

    assert payload["chunk_idx"] == 7
    assert payload["top_level"] == "Jobs"
    assert payload["char_count"] == len(chunks[0].text)
    assert payload["created_at_min_iso"] == "2023-11-14T22:13:20Z"
    assert payload["modified_at_max_iso"] is None


def test_payload_top_level_falls_back_when_a_chunk_has_no_headings() -> None:
    chunk = chunking.WorkflowyRenderedChunk(headings=[], text="body", context_text="")

    assert chunking.chunk_to_payload(chunk, 0)["top_level"] == "(root)"


def test_note_text_accumulates_one_line_per_call() -> None:
    assert chunking.append_note_text(None, "a") == "a"
    assert chunking.append_note_text("a", "b") == "a\nb"


def test_canonical_text_folds_case_and_collapses_whitespace() -> None:
    assert chunking.canonical_text("  Hello   World ") == "hello world"
    assert chunking.canonical_text(None) == ""
    assert chunking.is_blank_text("   ") is True
    assert chunking.is_blank_text(" x ") is False

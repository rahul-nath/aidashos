# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""llama.cpp splits an assistant reply across two fields depending on how the
server parses thought tags. These cover the reads that made a full
transcription look like an empty response."""

from __future__ import annotations

from local_first_agent_os.model_manager import llama_message_text

TRANSCRIPTION = '<div data-bbox="1 2 3 4" data-label="Text"><p>hello</p></div>'


def test_reads_ordinary_content() -> None:
    assert llama_message_text({"content": TRANSCRIPTION}) == TRANSCRIPTION


def test_falls_back_to_reasoning_channel_when_content_is_empty() -> None:
    """chandra-ocr-2 under llama.cpp's default deepseek thought parsing: the
    whole transcription lands in `reasoning_content` and `content` is empty."""
    message = {"content": "", "reasoning_content": TRANSCRIPTION}
    assert llama_message_text(message) == TRANSCRIPTION


def test_whitespace_only_content_is_not_a_reply() -> None:
    message = {"content": "   \n ", "reasoning_content": TRANSCRIPTION}
    assert llama_message_text(message) == TRANSCRIPTION


def test_strips_think_tags_left_by_disabled_parsing() -> None:
    """With `reasoning-format = none` the same body arrives in `content`, still
    carrying the literal tag the model opened."""
    message = {"content": f"<think>\n{TRANSCRIPTION}\n</think>"}
    assert llama_message_text(message) == TRANSCRIPTION


def test_strips_unclosed_think_tag() -> None:
    message = {"content": f"<think>\n{TRANSCRIPTION}"}
    assert llama_message_text(message) == TRANSCRIPTION


def test_content_wins_when_both_channels_are_populated() -> None:
    """A model that genuinely reasons then answers must not have its answer
    replaced by its scratch work."""
    message = {"content": TRANSCRIPTION, "reasoning_content": "let me think..."}
    assert llama_message_text(message) == TRANSCRIPTION


def test_genuinely_empty_reply_stays_empty() -> None:
    """The empty-transcription guard downstream still needs to fire when the
    model produced nothing at all."""
    assert llama_message_text({"content": "", "reasoning_content": ""}) == ""
    assert llama_message_text({"content": None}) == ""

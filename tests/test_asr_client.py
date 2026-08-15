# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the ASR streaming client: rendering, transcript dump, and the
session-bounding logic that keeps the microphone from being held open."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from local_first_agent_os.asr_client import (
    _classify_transcription_stop_reason,
    _dump_transcript,
    _flush,
    _is_likely_silence_hallucination,
    _post_inference,
    _render_colored,
    maybe_dispatch_triggers,
)
from local_first_agent_os.constants import ASR_INACTIVITY_TIMEOUT_S, ASR_MAX_SESSION_S


def test_render_colored_survives_integer_token_ids() -> None:
    """whisper.cpp's verbose_json emits `tokens` as bare integer IDs.

    Regression: the renderer used to call `.get()` on each token, crashing
    the live ASR session with `'int' object has no attribute 'get'`.
    """
    segments = [{"text": " hello world", "avg_logprob": -0.1, "tokens": [50364, 1234, 9]}]
    out = _render_colored(segments, "hello world")
    assert "hello world" in out


def test_render_colored_uses_segment_avg_logprob() -> None:
    out = _render_colored([{"text": " sure", "avg_logprob": -2.0}], "sure")
    assert "sure" in out
    assert out != "sure"  # coloured with ANSI escapes


def test_render_colored_falls_back_to_plain_without_scores() -> None:
    assert _render_colored([{"text": " hi"}], "hi") == "hi"


def test_render_colored_plain_when_no_segments() -> None:
    assert _render_colored(None, "hi") == "hi"
    assert _render_colored([], "hi") == "hi"


def test_trigger_router_is_logging_only(capsys: pytest.CaptureFixture[str]) -> None:
    maybe_dispatch_triggers(
        "please store this note",
        [{"phrase": "store this", "directive": "/store", "args": ["/tmp/note.md"]}],
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "would dispatch /store /tmp/note.md" in captured.err


def test_render_colored_survives_non_dict_segment() -> None:
    malformed_segments = cast(list[dict[str, Any]], [42])
    assert _render_colored(malformed_segments, "hi") == "hi"


def test_silence_hallucination_filter_matches_observed_empty_audio_text() -> None:
    assert _is_likely_silence_hallucination(" Thank you.\n")
    assert _is_likely_silence_hallucination(" .\n")
    assert not _is_likely_silence_hallucination("thank you for checking")


def test_dump_transcript_writes_accumulated_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _dump_transcript(["hello world", "   ", "second line"])
    assert path is not None
    assert path.parent == tmp_path
    assert path.name.startswith("asr-transcript-") and path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "hello world\nsecond line\n"


def test_dump_transcript_returns_none_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _dump_transcript([]) is None
    assert _dump_transcript(["  ", ""]) is None
    assert list(tmp_path.iterdir()) == []


# --- session bounding -------------------------------------------------------


def test_classify_transcription_stop_reason_none_while_session_is_fresh() -> None:
    # Just started, speech a moment ago — keep going.
    assert (
        _classify_transcription_stop_reason(now=10.0, last_speech_t=9.0, session_start_t=0.0)
        is None
    )


def test_classify_transcription_stop_reason_fires_on_inactivity() -> None:
    # No transcribed speech for longer than the inactivity window.
    reason = _classify_transcription_stop_reason(
        now=ASR_INACTIVITY_TIMEOUT_S + 5.0,
        last_speech_t=0.0,
        session_start_t=0.0,
    )
    assert reason is not None and "inactivity" in reason


def test_classify_transcription_stop_reason_absolute_cap_fires_even_with_constant_speech() -> None:
    # The runaway case: speech keeps landing (last_speech_t stays fresh), so
    # inactivity never fires — but the absolute cap must still end it.
    now = ASR_MAX_SESSION_S + 5.0
    reason = _classify_transcription_stop_reason(
        now=now, last_speech_t=now - 1.0, session_start_t=0.0
    )
    assert reason is not None and "max session" in reason


def test_absolute_cap_exceeds_inactivity_timeout() -> None:
    # The cap is a backstop — it must sit above the inactivity window, or it
    # would always fire first and make the inactivity timeout meaningless.
    assert ASR_MAX_SESSION_S > ASR_INACTIVITY_TIMEOUT_S


def test_post_inference_sends_temp_wav_path_for_current_whisper_server() -> None:
    class _Response:
        text = '{"text": "ok"}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"text": "ok"}

    class _Client:
        def __init__(self) -> None:
            self.seen_path: Path | None = None

        def post(self, _path: str, *, files: dict[str, tuple[None, str]]) -> _Response:
            file_field = files["file"]
            assert file_field[0] is None
            temp_path = Path(file_field[1])
            assert temp_path.exists()
            assert temp_path.read_bytes().startswith(b"RIFF")
            self.seen_path = temp_path
            return _Response()

    client = _Client()
    assert _post_inference(client, b"\x00\x00" * 480)["text"] == "ok"  # type: ignore[arg-type]
    assert client.seen_path is not None
    assert not client.seen_path.exists()


def test_flush_returns_true_on_transcribed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.asr_client._post_inference",
        lambda client, pcm: {"text": " hello there", "segments": []},
    )
    accumulated: list[str] = []
    null_client = cast(httpx.Client, None)
    assert _flush(null_client, b"\x00\x00", accumulated, [], False) is True
    assert accumulated == ["hello there"]


def test_flush_returns_false_on_empty_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.asr_client._post_inference",
        lambda client, pcm: {"text": "   "},
    )
    accumulated: list[str] = []
    null_client = cast(httpx.Client, None)
    assert _flush(null_client, b"\x00\x00", accumulated, [], False) is False
    assert accumulated == []


def test_flush_suppresses_observed_silence_hallucination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.asr_client._post_inference",
        lambda client, pcm: {"text": " Thank you.\n", "segments": []},
    )
    accumulated: list[str] = []
    null_client = cast(httpx.Client, None)
    assert _flush(null_client, b"\x00\x00", accumulated, [], False) is False
    assert accumulated == []


def test_flush_suppresses_punctuation_only_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.asr_client._post_inference",
        lambda client, pcm: {"text": " .\n", "segments": []},
    )
    accumulated: list[str] = []
    null_client = cast(httpx.Client, None)
    assert _flush(null_client, b"\x00\x00", accumulated, [], False) is False
    assert accumulated == []


def test_flush_returns_false_when_whisper_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(client: object, pcm: bytes) -> dict[str, object]:
        raise httpx.ConnectError("whisper-server down")

    monkeypatch.setattr("local_first_agent_os.asr_client._post_inference", _boom)
    null_client = cast(httpx.Client, None)
    assert _flush(null_client, b"\x00\x00", [], [], False) is False

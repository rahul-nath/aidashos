# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Streaming ASR client backed by whisper.cpp's HTTP server.

Wakes the default input device, runs client-side VAD (webrtcvad) so only
voiced segments are sent to whisper, and prints rolling transcription with
per-segment confidence colouring to stdout.

Lifecycle (driven by ``pi /start /asr`` via ``AudioTranscriber.stream``):

1. ``AudioTranscriber.load_active_model`` has already swapped the resident
   whisper-server to the active large model before this function runs.
2. We open the microphone, frame audio into 30 ms chunks, classify each
   with webrtcvad, and accumulate voiced runs into "utterances".
3. Each utterance is wrapped in a WAV header and POSTed to
   ``/inference``; the response is rendered with per-segment confidence
   colouring (verbose_json carries avg_logprob).
4. The session always ends, and is always bounded: Ctrl-C, or
   ``ASR_INACTIVITY_TIMEOUT_S`` seconds without *transcribed* speech, or
   the absolute ``ASR_MAX_SESSION_S`` wall-clock cap (which nothing
   resets). On every exit the transcript is dumped to a timestamped file
   in the caller's CWD.
5. Each transcribed chunk runs through a trigger router that looks for
   phrases in ``configs/asr_triggers.toml``; matches log "would dispatch
   <directive>" — actual pi dispatch is intentionally a TODO so this
   ships safe without surprise side-effects.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import logging
import math
import queue
import re
import sys
import tempfile
import time
import tomllib
import wave
from pathlib import Path
from typing import Any

import httpx

from .constants import (
    ASR_FRAME_MS,
    ASR_INACTIVITY_TIMEOUT_S,
    ASR_MAX_SEGMENT_SECONDS,
    ASR_MAX_SESSION_S,
    ASR_MIN_VOICED_FRAMES,
    ASR_SAMPLE_RATE_HZ,
    ASR_SILENCE_FRAMES_TO_CUT,
    ASR_VAD_AGGRESSIVENESS,
)
from .settings import Settings

logger = logging.getLogger(__name__)

FRAME_SAMPLES = (ASR_SAMPLE_RATE_HZ * ASR_FRAME_MS) // 1000  # 480 @ 30 ms / 16 kHz
SAMPLE_WIDTH = 2  # int16
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH
_TRANSCRIPT_WORD_RE = re.compile(r"[a-z0-9']+")
_SILENCE_HALLUCINATION_PHRASES = frozenset(
    {
        "thank you",
        "thanks for watching",
    }
)


# ----------------------------------------------------------------------------
# Trigger router (config-driven, no triggers wired by default)
# ----------------------------------------------------------------------------


def load_triggers(path: Path) -> list[dict[str, Any]]:
    """Read ``configs/asr_triggers.toml``.

    Returns a list of ``{"phrase": str, "directive": str, "args": list[str]}``
    entries (lowercased phrase). Missing file or empty triggers list returns
    ``[]`` — the router becomes a no-op.
    """
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    triggers: list[dict[str, Any]] = []
    for entry in data.get("triggers", []) or []:
        phrase = (entry.get("phrase") or "").lower().strip()
        directive = entry.get("directive")
        if not phrase or not directive:
            continue
        triggers.append(
            {
                "phrase": phrase,
                "directive": str(directive),
                "args": list(entry.get("args") or []),
            }
        )
    return triggers


def maybe_dispatch_triggers(text: str, triggers: list[dict[str, Any]]) -> None:
    """If any trigger phrase appears in ``text``, log a dispatch intent only.

    Wire-through to the pi runtime is intentionally absent. Voice-triggered
    side effects should remain disabled until the orchestration path has
    stronger false-positive handling, approval gates, and execution auditing.
    """
    if not triggers:
        return
    low = text.lower()
    for trig in triggers:
        if trig["phrase"] in low:
            sys.stderr.write(
                f"[asr] trigger '{trig['phrase']}' matched → would dispatch "
                f"{trig['directive']} {' '.join(trig['args'])}\n"
            )


# ----------------------------------------------------------------------------
# Per-segment confidence colouring (whisper-server verbose_json)
# ----------------------------------------------------------------------------

ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"


def _conf_color(p: float) -> str:
    if p >= 0.85:
        return ANSI_GREEN
    if p >= 0.55:
        return ANSI_YELLOW
    return ANSI_RED


def _render_colored(segments: list[dict[str, Any]] | None, plain: str) -> str:
    """Render whisper's transcript with per-segment confidence colours.

    whisper.cpp's OpenAI-compatible ``verbose_json`` reports confidence per
    segment via ``avg_logprob``; its ``tokens`` field is a list of bare integer
    token IDs, not per-token objects, so colouring is done at segment
    granularity. Falls back to the plain ``text`` when no segment carries a
    usable score (or the response format wasn't ``verbose_json``).
    """
    if not segments:
        return plain
    out: list[str] = []
    any_colored = False
    for seg in segments:
        if not isinstance(seg, dict):
            return plain
        text = seg.get("text")
        if not isinstance(text, str) or not text:
            continue
        lp = seg.get("avg_logprob")
        try:
            p = math.exp(float(lp)) if lp is not None else None
        except (TypeError, ValueError, OverflowError):
            p = None
        if p is None:
            out.append(text)
        else:
            out.append(f"{_conf_color(p)}{text}{ANSI_RESET}")
            any_colored = True
    return "".join(out) if any_colored else plain


def _normalize_transcript_text(text: str) -> str:
    return " ".join(_TRANSCRIPT_WORD_RE.findall(text.lower()))


def _is_likely_silence_hallucination(text: str) -> bool:
    """Catch whisper.cpp's common empty-audio hallucinations.

    On pure silence, large-v3-turbo can emit phrases like "Thank you." while
    also reporting an unusable no_speech_prob. Keep this intentionally narrow
    so ordinary speech is not filtered aggressively.
    """
    if not any(ch.isalnum() for ch in text):
        return True
    return _normalize_transcript_text(text) in _SILENCE_HALLUCINATION_PHRASES


# ----------------------------------------------------------------------------
# WAV wrapping
# ----------------------------------------------------------------------------


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int = ASR_SAMPLE_RATE_HZ) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# whisper-server I/O
# ----------------------------------------------------------------------------


def _post_inference(client: httpx.Client, pcm: bytes) -> dict[str, Any]:
    wav_bytes = _pcm_to_wav_bytes(pcm)
    with tempfile.NamedTemporaryFile(suffix=".wav", prefix="local-agent-asr-") as fh:
        fh.write(wav_bytes)
        fh.flush()
        response = client.post(
            "/inference",
            files={
                # Current whisper-server reads this multipart field as a
                # filesystem path unless it was started with --convert.
                "file": (None, fh.name),
                "response_format": (None, "verbose_json"),
                "temperature": (None, "0.0"),
                "no_speech_thold": (None, "0.6"),
            },
        )
    response.raise_for_status()
    return response.json()


# ----------------------------------------------------------------------------
# Main streaming loop
# ----------------------------------------------------------------------------


def _now_iso_compact() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _dump_transcript(accumulated: list[str]) -> Path | None:
    """Write the session transcript to a timestamped file in the caller's CWD.

    Returns the path written, or ``None`` if nothing was transcribed.
    """
    lines = [line for line in accumulated if line.strip()]
    if not lines:
        return None
    dump_path = Path.cwd() / f"asr-transcript-{_now_iso_compact()}.txt"
    dump_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dump_path


def _classify_transcription_stop_reason(
    now: float, last_speech_t: float, session_start_t: float
) -> str | None:
    """Decide whether the streaming session must end.

    Returns a human-readable reason, or ``None`` to keep going. Two
    independent guarantees:

    * **inactivity** — no *transcribed* speech for ``ASR_INACTIVITY_TIMEOUT_S``
      (``last_speech_t`` is advanced only by a `_flush` that returns text, so
      ambient noise alone cannot keep the session alive);
    * **absolute cap** — ``ASR_MAX_SESSION_S`` of wall clock since the session
      began, never reset by anything, so the microphone can never be held
      open indefinitely.
    """
    if now - last_speech_t > ASR_INACTIVITY_TIMEOUT_S:
        return f"inactivity timeout ({ASR_INACTIVITY_TIMEOUT_S}s)"
    if now - session_start_t > ASR_MAX_SESSION_S:
        return f"max session duration ({ASR_MAX_SESSION_S}s)"
    return None


def run_stream(settings: Settings, triggers_path: Path | None = None) -> int:
    """Open the mic, stream voiced segments to whisper-server, print results.

    Returns a process-style exit code. 0 = clean end (user Ctrl-C); 1 =
    inactivity timeout or the absolute max-session cap fired; 2 = audio
    dependency missing. On every exit the session transcript is written
    to a timestamped file in the caller's CWD.
    """
    # Imported lazily so users without the audio extras can still import this
    # module (for the trigger loader, etc.) without crashing.
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
        import webrtcvad  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        sys.stderr.write(
            f"[asr] missing audio dependency: {exc.name}. "
            "Run: uv sync (adds sounddevice + webrtcvad-wheels). "
            "On macOS you may also need: brew install portaudio.\n"
        )
        return 2

    triggers = load_triggers(triggers_path or settings.asr_triggers_path)
    vad = webrtcvad.Vad(ASR_VAD_AGGRESSIVENESS)
    audio_q: queue.Queue[bytes] = queue.Queue()

    def _audio_cb(indata, _frames, _t, status) -> None:  # noqa: ANN001
        if status:
            sys.stderr.write(f"[asr] sounddevice status: {status}\n")
        audio_q.put(bytes(indata))

    voiced_buf = bytearray()
    voiced_frames = 0
    silence_frames = 0
    last_speech_t = time.monotonic()
    session_start_t = last_speech_t
    accumulated: list[str] = []
    timeout_hit = False
    stop_reason: str | None = None
    max_segment_bytes = int(ASR_MAX_SEGMENT_SECONDS * ASR_SAMPLE_RATE_HZ * SAMPLE_WIDTH)

    sys.stderr.write(
        f"[asr] listening on default input ({ASR_SAMPLE_RATE_HZ} Hz). "
        f"Inactivity timeout: {ASR_INACTIVITY_TIMEOUT_S}s, "
        f"max session: {ASR_MAX_SESSION_S}s. Speak to start.\n"
    )

    with (
        httpx.Client(base_url=settings.whisper_base_url, timeout=120) as client,
        sd.RawInputStream(
            samplerate=ASR_SAMPLE_RATE_HZ,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            callback=_audio_cb,
        ),
    ):
        try:
            while True:
                # Inactivity resets only on transcribed speech (the _flush
                # calls below); the absolute cap is never reset. Either way
                # the session — and the microphone — is always bounded.
                stop_reason = _classify_transcription_stop_reason(
                    time.monotonic(), last_speech_t, session_start_t
                )
                if stop_reason is not None:
                    timeout_hit = True
                    break
                try:
                    frame = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                # webrtcvad needs exact frame sizes.
                if len(frame) != FRAME_BYTES:
                    continue
                if vad.is_speech(frame, ASR_SAMPLE_RATE_HZ):
                    voiced_buf.extend(frame)
                    voiced_frames += 1
                    silence_frames = 0
                    # Force-flush very long monologues so chunks stream out.
                    if len(voiced_buf) >= max_segment_bytes:
                        if _flush(client, bytes(voiced_buf), accumulated, triggers, timeout_hit):
                            last_speech_t = time.monotonic()
                        voiced_buf = bytearray()
                        voiced_frames = 0
                else:
                    silence_frames += 1
                    if voiced_buf and silence_frames >= ASR_SILENCE_FRAMES_TO_CUT:
                        if voiced_frames >= ASR_MIN_VOICED_FRAMES and _flush(
                            client, bytes(voiced_buf), accumulated, triggers, timeout_hit
                        ):
                            last_speech_t = time.monotonic()
                        voiced_buf = bytearray()
                        voiced_frames = 0
                        silence_frames = 0
        except KeyboardInterrupt:
            sys.stderr.write("\n[asr] interrupted; finishing pending segment...\n")
            if voiced_buf and voiced_frames >= ASR_MIN_VOICED_FRAMES and not timeout_hit:
                _flush(client, bytes(voiced_buf), accumulated, triggers, timeout_hit)
            dump_path = _dump_transcript(accumulated)
            if dump_path is not None:
                sys.stderr.write(f"[asr] transcript saved to {dump_path}\n")
            else:
                sys.stderr.write("[asr] nothing transcribed; no transcript written.\n")
            return 0

    # Timeout / session-cap path: dump and report.
    if timeout_hit:
        dump_path = _dump_transcript(accumulated)
        suffix = (
            f"Transcript saved to {dump_path}"
            if dump_path is not None
            else "Nothing transcribed; no transcript written."
        )
        sys.stderr.write(f"\n[asr] {stop_reason} reached. {suffix}\n")
        return 1
    return 0


def _flush(
    client: httpx.Client,
    pcm: bytes,
    accumulated: list[str],
    triggers: list[dict[str, Any]],
    timeout_hit: bool,
) -> bool:
    """Send one voiced segment to whisper-server and surface the result.

    Returns ``True`` only if whisper returned text that survives empty-audio
    hallucination filtering. The caller uses that to reset the inactivity
    clock, so ambient noise that transcribes to nothing cannot keep the
    session — and the microphone — alive indefinitely.

    If the inactivity timeout has already fired, we don't print to stdout
    (we still record into ``accumulated`` so the dump on the calling side
    captures everything that was transcribed).
    """
    try:
        data = _post_inference(client, pcm)
    except httpx.HTTPError as exc:
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f" body={exc.response.text[:500]}"
        sys.stderr.write(f"[asr] whisper-server error: {exc}{detail}\n")
        return False
    text = (data.get("text") or "").strip()
    if not text:
        return False
    if _is_likely_silence_hallucination(text):
        sys.stderr.write(f"[asr] suppressed likely silence hallucination: {text!r}\n")
        return False
    accumulated.append(text)
    if not timeout_hit:
        colored = _render_colored(data.get("segments"), text)
        sys.stdout.write(colored + "\n")
        sys.stdout.flush()
    maybe_dispatch_triggers(text, triggers)
    return True


# ----------------------------------------------------------------------------
# Entry point (for python -m local_first_agent_os.asr_client)
# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asr_client", description=__doc__)
    parser.add_argument(
        "--triggers",
        type=Path,
        default=None,
        help="Override path to asr_triggers.toml (default: settings.asr_triggers_path)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    settings = Settings()
    try:
        return run_stream(settings, triggers_path=args.triggers)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

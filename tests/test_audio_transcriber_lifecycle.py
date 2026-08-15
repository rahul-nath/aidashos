# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from local_first_agent_os.audio_transcriber import AudioTranscriber
from local_first_agent_os.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        spool_dir=tmp_path / "spool",
        session_context_export_dir=tmp_path / "session-contexts",
        config_dir=tmp_path / "configs",
        mock_models=False,
        use_dbos=False,
    )


def test_load_active_model_starts_service_when_server_is_down(tmp_path: Path, monkeypatch) -> None:
    transcriber = AudioTranscriber(_settings(tmp_path))
    calls = []
    monkeypatch.setattr(transcriber, "is_available", lambda: False)
    monkeypatch.setattr(
        transcriber,
        "_run_service_action",
        lambda action: calls.append(action) or "started",
    )
    monkeypatch.setattr(
        transcriber,
        "_post_load",
        lambda _path: calls.append("load"),
    )

    result = transcriber.load_active_model()

    assert result["server"] == "started"
    assert calls == ["start"]


def test_stop_server_runs_service_shutdown(tmp_path: Path, monkeypatch) -> None:
    transcriber = AudioTranscriber(_settings(tmp_path))
    calls = []
    monkeypatch.setattr(
        transcriber,
        "_run_service_action",
        lambda action: calls.append(action) or "stopped",
    )

    result = transcriber.stop_server()

    assert result == {"status": "stopped", "output": "stopped"}
    assert calls == ["stop"]


def test_service_action_passes_whisper_settings(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    transcriber = AudioTranscriber(settings)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("local_first_agent_os.audio_transcriber.subprocess.run", fake_run)

    assert transcriber._run_service_action("stop") == "ok"
    assert captured["command"][-1] == "stop"
    assert captured["env"]["LOCAL_AGENT_WHISPER_PORT"] == str(settings.whisper_port)
    assert captured["timeout"] == 90

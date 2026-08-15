# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in host smoke test for real Chandra model loading and OCR.

Place sanitized image fixtures and expected text under tests/fixtures/chandra_ocr.
Enable with:

    LOCAL_AGENT_CHANDRA_HOST_SMOKE=1 uv run pytest -q tests/test_chandra_host_smoke.py
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from local_first_agent_os.contracts import SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import build_runtime
from local_first_agent_os.settings import Settings
from local_first_agent_os.workflow import WorkflowEngine

MODEL_NAME = "chandra-ocr-2-q8"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "chandra_ocr"
SUPPORTED_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

pytestmark = pytest.mark.skipif(
    os.getenv("LOCAL_AGENT_CHANDRA_HOST_SMOKE") != "1",
    reason="host smoke is opt-in; set LOCAL_AGENT_CHANDRA_HOST_SMOKE=1",
)


def _model_status(client: httpx.Client) -> dict[str, Any]:
    response = client.get("/models")
    response.raise_for_status()
    for item in response.json().get("data", []):
        if item.get("id") == MODEL_NAME:
            return item
    raise AssertionError(f"router does not advertise {MODEL_NAME}")


def _status_value(model: dict[str, Any]) -> str:
    status = model.get("status")
    if isinstance(status, dict):
        return str(status.get("value", "unknown"))
    return str(status or "unknown")


def _wait_until_loaded(client: httpx.Client, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        model = _model_status(client)
        status = _status_value(model)
        if status in {"loaded", "sleeping"}:
            modalities = model.get("architecture", {}).get("input_modalities", [])
            assert "image" in modalities
            return
        if status == "failed":
            raise AssertionError(f"{MODEL_NAME} failed to load: {model.get('status')}")
        time.sleep(1)
    raise AssertionError(f"timed out loading {MODEL_NAME}")


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def test_real_chandra_loads_and_runs_ocr_capture_workflow(tmp_path: Path) -> None:
    images = sorted(
        path
        for path in FIXTURE_ROOT.iterdir()
        if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
    )
    assert images, (
        f"no Chandra fixtures found in {FIXTURE_ROOT}; add an image and a matching "
        "<stem>.expected.txt file"
    )

    base_url = os.getenv("LOCAL_AGENT_LLAMA_BASE_URL", "http://127.0.0.1:8080")
    load_timeout = float(os.getenv("LOCAL_AGENT_CHANDRA_LOAD_TIMEOUT_SECONDS", "300"))
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'chandra-smoke.sqlite3'}",
            "artifact_root": tmp_path / "artifacts",
            "spool_dir": tmp_path / "spool",
            "session_context_export_dir": tmp_path / "session-contexts",
            "config_dir": Path(__file__).parent.parent / "configs",
            "llama_base_url": base_url,
            "mock_models": False,
            "use_dbos": False,
        }
    )
    runtime = build_runtime(settings)
    loaded_by_test = False
    with httpx.Client(base_url=base_url, timeout=load_timeout) as client:
        initial_status = _status_value(_model_status(client))
        if initial_status not in {"loaded", "sleeping"}:
            response = client.post("/models/load", json={"model": MODEL_NAME})
            response.raise_for_status()
            loaded_by_test = True
        try:
            _wait_until_loaded(client, load_timeout)
            event = normalize_scheduled_event(
                source_type=SourceType.MANUAL,
                workspace_id=WorkspaceId.GENERAL.value,
                event_type="pi.directive",
                payload={"directive": f"/hard-ocr {FIXTURE_ROOT.resolve()}"},
            )
            result = WorkflowEngine(runtime).model_directive(event)
            if result.status != WorkflowStatus.COMPLETED:
                manifest_ref = next(
                    artifact
                    for artifact in result.artifacts
                    if str(artifact.role) == "ocr_batch_manifest"
                )
                manifest = runtime.artifact_store.read_json(manifest_ref.artifact_id)
                pytest.fail(
                    "real Chandra OCR batch did not complete:\n"
                    + json.dumps(manifest, indent=2, sort_keys=True)
                )
            ocr_payloads = [
                runtime.artifact_store.read_json(artifact.artifact_id)
                for artifact in result.artifacts
                if str(artifact.role) == "ocr_text"
            ]
            assert len(ocr_payloads) == len(images)
            transcriptions = {
                Path(payload["source_path"]).name: str(payload["transcription"])
                for payload in ocr_payloads
            }
            for image_path in images:
                expected_path = image_path.with_suffix(".expected.txt")
                assert expected_path.is_file(), f"missing expected text file: {expected_path}"
                expected_phrases = [
                    line.strip()
                    for line in expected_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                assert expected_phrases, f"expected text file is empty: {expected_path}"
                content = transcriptions[image_path.name]
                normalized_content = _normalized(content)
                missing = [
                    phrase
                    for phrase in expected_phrases
                    if _normalized(phrase) not in normalized_content
                ]
                assert not missing, (
                    f"Chandra missed expected phrases for {image_path.name}: {missing}; "
                    f"output was: {content}"
                )
        finally:
            if loaded_by_test:
                with contextlib.suppress(httpx.HTTPError):
                    client.post("/models/unload", json={"model": MODEL_NAME})

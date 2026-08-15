# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from local_first_agent_os.contracts import ModelRole, SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.directives import DirectiveParser
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.pi_channel import plan_terminal_actions
from local_first_agent_os.tools import WorkflowyApiClient, WorkflowyDayBulletTool
from local_first_agent_os.workflow import WorkflowEngine


def _directive_event(directive: str):
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def test_asr_role_is_registered_as_whisper_runtime(runtime) -> None:
    spec = runtime.model_registry.resolve_model(ModelRole.ASR)
    assert spec.runtime == "whisper.cpp"
    assert spec.backend == "coreml+metal"
    assert spec.ggml_path and spec.ggml_path.endswith("ggml-large-v3-turbo.bin")


def test_directive_parser_resolves_asr_alias(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    assert parser.parse("/start /asr").model_role == ModelRole.ASR
    assert parser.parse("/start /audio").model_role == ModelRole.ASR


def test_directive_parser_resolves_dispatcher_start(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    spec = parser.parse("/start /dispatcher --tier staff --interval 0.5 --max-polls 1")
    actions = plan_terminal_actions("/start /dispatcher --tier staff --interval 0.5 --max-polls 1")
    assert spec.action == "dispatcher"
    assert spec.dispatcher_tier == "staff"
    assert spec.dispatcher_interval_seconds == 0.5
    assert spec.dispatcher_max_polls == 1
    assert len(actions) == 1
    assert actions[0].text == "/start /dispatcher --tier staff --interval 0.5 --max-polls 1"


def test_directive_parser_rejects_unbounded_dispatcher(runtime) -> None:
    parser = DirectiveParser(runtime.settings)

    with pytest.raises(ValueError, match="requires --max-polls"):
        parser.parse("/start /dispatcher")


def test_directive_parser_resolves_operator_shortcuts(runtime) -> None:
    parser = DirectiveParser(runtime.settings)

    retry = parser.parse("/try-milestone Retry after provider fallback repair")
    approve = parser.parse("/approve-most-recent")
    dispatch = parser.parse("/dispatch")
    review = parser.parse("/review-merge")
    approve_merge = parser.parse("/approve-merge approval-123")

    assert retry.action == "try_milestone"
    assert retry.query == "Retry after provider fallback repair"
    assert approve.action == "approve_most_recent"
    assert dispatch.action == "dispatch_once"
    assert dispatch.alias == "/dispatch"
    assert dispatch.dispatcher_max_polls == 1
    assert dispatch.alias == "/dispatch"
    assert review.action == "review_merge"
    assert review.query is None
    assert approve_merge.action == "approve_merge"
    assert approve_merge.query == "approval-123"


def test_audio_transcription_uses_centralized_loader_in_mock_mode(runtime, tmp_path: Path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFFmock-bytes")
    event = normalize_scheduled_event(
        source_type=SourceType.FILE,
        workspace_id=WorkspaceId.AUDIO.value,
        event_type="file.created",
        payload={"source_uri": f"file://{audio}"},
    )
    event = event.model_copy(update={"source_uri": f"file://{audio}"})
    result = WorkflowEngine(runtime).audio_transcription(event)
    assert result.status == WorkflowStatus.COMPLETED
    transcripts = [a for a in result.artifacts if str(a.role) == "transcript"]
    assert transcripts, "expected a transcript artifact when transcriber returns mock data"
    payload = runtime.artifact_store.read_json(transcripts[0].artifact_id)
    assert "Mock ASR transcript" in payload["text"]


def test_send_to_wf_audio_uses_real_transcript_text(runtime, tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"id3-mock")
    result = WorkflowEngine(runtime).model_directive(_directive_event(f"/send-to-wf {audio} 12/03"))
    assert result.status == WorkflowStatus.COMPLETED
    payloads = [a for a in result.artifacts if str(a.role) == "send_to_wf_payload"]
    assert payloads
    payload = runtime.artifact_store.read_json(payloads[0].artifact_id)
    assert payload["source_kind"] == "audio"
    assert "Mock ASR transcript" in payload["content"]
    assert payload["downstream"]["transcript_artifact_id"] is not None


def test_send_to_wf_image_records_durable_image_metadata(runtime, tmp_path: Path) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nmock")
    result = WorkflowEngine(runtime).model_directive(_directive_event(f"/send-to-wf {image} 04/28"))
    assert result.status == WorkflowStatus.COMPLETED
    payloads = [a for a in result.artifacts if str(a.role) == "send_to_wf_payload"]
    payload = runtime.artifact_store.read_json(payloads[0].artifact_id)
    downstream = payload["downstream"]
    assert downstream["image_artifact"].startswith("artifact:source_image:")
    assert downstream["image_artifact_uri"].startswith("file://")
    assert downstream["image_sha256"]
    artifact_ref = runtime.repository.get_artifact(downstream["image_artifact"])
    assert artifact_ref is not None
    assert Path(artifact_ref.path).exists()


def test_workflowy_api_client_dry_run_returns_empty_top_level(runtime) -> None:
    client = WorkflowyApiClient(runtime.settings)
    assert client.is_live is False
    assert client.list_top_level() == []


class _Recorder:
    def __init__(self, responses: list[dict[str, Any]]):
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        body = self._responses.pop(0)
        request = httpx.Request(method, url, headers=kwargs.get("headers"))
        return httpx.Response(
            status_code=body.get("status", 200),
            json=body["json"],
            request=request,
        )


def _patch_httpx(monkeypatch, recorder: _Recorder) -> None:
    def _get(url: str, **kwargs: Any) -> httpx.Response:
        return recorder.request("GET", url, **kwargs)

    def _post(url: str, **kwargs: Any) -> httpx.Response:
        return recorder.request("POST", url, **kwargs)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)


def test_workflowy_day_bullet_creates_parent_when_missing(monkeypatch, runtime) -> None:
    # Settings snapshots env at fixture construction, so setenv here is too late.
    runtime.settings.workflowy_api_key = "sk-test"
    runtime.settings.workflowy_dry_run = False
    client = WorkflowyApiClient(runtime.settings, top_level_ttl_seconds=0)
    recorder = _Recorder(
        [
            {
                "json": {
                    "nodes": [
                        {"id": "done-1", "name": "/done", "priority": 0},
                        {"id": "x-1", "name": "Inbox", "priority": 1},
                    ]
                }
            },
            {"json": {"item_id": "new-04-28"}},
            {"json": {"item_id": "child-1"}},
        ]
    )
    _patch_httpx(monkeypatch, recorder)

    day_tool = WorkflowyDayBulletTool(
        runtime.settings,
        runtime.tool_registry.tools["workflowy_fetch_nodes"],  # type: ignore[arg-type]
        runtime.tool_registry.tools["workflowy_insert_node"],  # type: ignore[arg-type]
        client=client,
    )
    response = day_tool.run(
        "wf-test",
        {
            "month_day": "04/28",
            "content": "Note about Pi",
            "content_sha256": "deadbeef",
        },
    )
    assert response["parent_created"] is True
    assert response["parent_node_id"] == "new-04-28"
    assert response["done_node_id"] == "done-1"
    assert response["live"] is True
    assert len(recorder.calls) == 3
    list_call = recorder.calls[0]
    assert list_call["method"] == "GET"
    assert list_call["url"].endswith("/nodes")
    assert list_call["params"] == {"parent_id": "None"}
    create_parent = recorder.calls[1]
    assert create_parent["method"] == "POST"
    assert create_parent["json"]["parent_id"] == "None"
    assert create_parent["json"]["name"] == "04/28"
    create_child = recorder.calls[2]
    assert create_child["json"]["parent_id"] == "new-04-28"
    assert create_child["json"]["name"] == "Note about Pi"


def test_workflowy_day_bullet_always_makes_a_new_parent(monkeypatch, runtime) -> None:
    """A second capture on a date gets its own bullet, not a shared one.

    The tool used to find-and-reuse an existing MM/DD. Folding a day's captures
    under one heading is a decision about the outline, and the operator wants
    each capture separate, so the listing is now read only to place the new
    bullet next to /done.
    """

    runtime.settings.workflowy_api_key = "sk-test"
    runtime.settings.workflowy_dry_run = False
    client = WorkflowyApiClient(runtime.settings, top_level_ttl_seconds=0)
    recorder = _Recorder(
        [
            {
                "json": {
                    "nodes": [
                        {"id": "done-1", "name": "/done", "priority": 0},
                        {"id": "may-04", "name": "04/28", "priority": 1},
                    ]
                }
            },
            {"json": {"item_id": "parent-new"}},
            {"json": {"item_id": "child-2"}},
        ]
    )
    _patch_httpx(monkeypatch, recorder)

    day_tool = WorkflowyDayBulletTool(
        runtime.settings,
        runtime.tool_registry.tools["workflowy_fetch_nodes"],  # type: ignore[arg-type]
        runtime.tool_registry.tools["workflowy_insert_node"],  # type: ignore[arg-type]
        client=client,
    )
    response = day_tool.run(
        "wf-test",
        {
            "month_day": "04/28",
            "content": "Another note",
            "content_sha256": "cafef00d",
        },
    )
    assert response["parent_created"] is True
    assert response["parent_node_id"] == "parent-new"
    # list, create parent, create child - the existing 04/28 is left alone.
    assert len(recorder.calls) == 3
    assert recorder.calls[1]["json"]["parent_id"] == "None"
    assert recorder.calls[2]["json"]["parent_id"] == "parent-new"


def test_workflowy_day_bullet_defaults_the_date_to_today(monkeypatch, runtime) -> None:
    """An omitted date means today, resolved once and reported back.

    The resolved value is in the response because this runs inside a durable
    workflow: a caller that needs the same bullet across a retry reads it from
    the first run rather than letting a replay recompute "today".
    """

    runtime.settings.workflowy_api_key = "sk-test"
    runtime.settings.workflowy_dry_run = False
    client = WorkflowyApiClient(runtime.settings, top_level_ttl_seconds=0)
    recorder = _Recorder(
        [
            {"json": {"nodes": [{"id": "done-1", "name": "/done", "priority": 0}]}},
            {"json": {"item_id": "parent-new"}},
            {"json": {"item_id": "child-1"}},
        ]
    )
    _patch_httpx(monkeypatch, recorder)

    day_tool = WorkflowyDayBulletTool(
        runtime.settings,
        runtime.tool_registry.tools["workflowy_fetch_nodes"],  # type: ignore[arg-type]
        runtime.tool_registry.tools["workflowy_insert_node"],  # type: ignore[arg-type]
        client=client,
    )
    response = day_tool.run("wf-test", {"content": "no date given"})

    assert response["month_day"] == datetime.now().strftime("%m/%d")
    assert recorder.calls[1]["json"]["name"] == response["month_day"]

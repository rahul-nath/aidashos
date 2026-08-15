# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from local_first_agent_os import workflowy_refresh


class _RecordingWorkflowId:
    seen: list[str] = []

    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id

    def __enter__(self) -> None:
        self.seen.append(self.workflow_id)

    def __exit__(self, *_args: object) -> None:
        return None


def _enable_fake_dbos(monkeypatch) -> None:
    _RecordingWorkflowId.seen = []
    monkeypatch.setattr(
        workflowy_refresh,
        "get_settings",
        lambda: SimpleNamespace(use_dbos=True),
    )
    monkeypatch.setattr(workflowy_refresh, "DBOS", object())
    monkeypatch.setattr(workflowy_refresh, "SetWorkflowID", _RecordingWorkflowId)
    monkeypatch.setattr(
        "local_first_agent_os.dbos_app.launch_dbos",
        lambda: None,
    )
    monkeypatch.setattr(
        workflowy_refresh,
        "workflowy_refresh_workflow",
        lambda input_path, chunks_path, max_chars, generation_id: {
            "generation_id": generation_id,
            "workflow_id": workflowy_refresh.build_workflowy_refresh_id(generation_id),
            "chunked": 1,
            "imported": 1,
        },
    )


def test_separate_refresh_requests_receive_distinct_durable_identities(monkeypatch) -> None:
    _enable_fake_dbos(monkeypatch)

    first = workflowy_refresh.run_workflowy_refresh()
    second = workflowy_refresh.run_workflowy_refresh()

    assert first["generation_id"] != second["generation_id"]
    assert first["workflow_id"] != second["workflow_id"]
    assert _RecordingWorkflowId.seen == [first["workflow_id"], second["workflow_id"]]


def test_retrying_one_generation_reuses_its_durable_identity(monkeypatch) -> None:
    _enable_fake_dbos(monkeypatch)
    generation_id = UUID("12345678-1234-5678-1234-567812345678")

    first = workflowy_refresh.run_workflowy_refresh(generation_id=generation_id)
    retry = workflowy_refresh.run_workflowy_refresh(generation_id=generation_id)

    assert retry == first
    assert _RecordingWorkflowId.seen == [first["workflow_id"], first["workflow_id"]]


def test_generation_identity_reaches_the_import_checkpoint(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    generation_id = "12345678-1234-5678-1234-567812345678"
    monkeypatch.setattr(workflowy_refresh, "_sync_step", lambda *_args: 7)
    monkeypatch.setattr(
        workflowy_refresh,
        "_import_step",
        lambda chunks_path, received_generation_id: (
            calls.append((chunks_path, received_generation_id)) or 6
        ),
    )

    result = workflowy_refresh._execute_workflowy_refresh(
        None,
        "chunks.jsonl",
        1200,
        generation_id,
    )

    assert calls == [("chunks.jsonl", generation_id)]
    assert result == {
        "generation_id": generation_id,
        "workflow_id": workflowy_refresh.build_workflowy_refresh_id(generation_id),
        "chunked": 7,
        "imported": 6,
    }

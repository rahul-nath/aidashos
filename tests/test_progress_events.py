# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging

from local_first_agent_os.progress_events import emit_progress, progress_event_sink


def test_progress_event_projects_to_terminal_and_structured_log(caplog) -> None:
    events: list[dict[str, object]] = []

    with caplog.at_level(logging.INFO), progress_event_sink(events.append):
        emit_progress(
            "starting staff turn: review_change",
            phase="task_started",
            intent_id="intent-1",
            task_id="task-1",
            task_name="review_change",
        )

    assert events == [
        {
            "type": "status",
            "message": "starting staff turn: review_change",
            "phase": "task_started",
            "intent_id": "intent-1",
            "task_id": "task-1",
            "task_name": "review_change",
        }
    ]
    record = next(record for record in caplog.records if record.msg == "dispatch_progress")
    assert record.phase == "task_started"
    assert record.intent_id == "intent-1"
    assert record.task_id == "task-1"
    assert record.task_name == "review_change"

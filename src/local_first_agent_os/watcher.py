# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time
from os import fsdecode
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .contracts import IngressEvent, SourceType, WorkflowType
from .ids import build_event_id, sha256_file
from .ingress import BoundsError, DiskSpool, normalize_file_event
from .runtime import get_runtime
from .workflow import run_workflow


class DurableIngressHandler(FileSystemEventHandler):
    def __init__(self, workspace_id: str, workflow_type: WorkflowType, stable: bool = True):
        self.workspace_id = workspace_id
        self.workflow_type = workflow_type
        self.stable = stable
        self.runtime = get_runtime()
        self.spool = DiskSpool(self.runtime.settings)

    def on_created(self, event: FileSystemEvent) -> None:
        self._route_file_event_to_workflow(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._route_file_event_to_workflow(event, "modified")

    def _route_file_event_to_workflow(
        self,
        event: FileSystemEvent,
        event_type: str,
    ) -> None:
        if event.is_directory:
            return
        path = Path(fsdecode(event.src_path))
        try:
            envelope = normalize_file_event(
                path=path,
                workspace_id=self.workspace_id,
                workflow_type=self.workflow_type,
                event_type=event_type,
                stable=self.stable,
            )
            if self.runtime.settings.use_dbos:
                from .dbos_app import run_workflow_durably

                run_workflow_durably(self.workflow_type, envelope)
            else:
                run_workflow(self.workflow_type, envelope)
        except BoundsError as exc:
            digest = sha256_file(path) if path.exists() and path.is_file() else None
            source_uri = f"file://{path.expanduser().resolve()}"
            envelope = IngressEvent(
                event_id=build_event_id(
                    SourceType.FILE,
                    self.workspace_id,
                    source_uri,
                    event_type,
                    digest,
                ),
                source_type=SourceType.FILE,
                event_type=event_type,
                workspace_id=self.workspace_id,
                source_uri=source_uri,
                content_sha256=digest,
                payload={
                    "workflow_type": self.workflow_type.value,
                    "bound_rejection": exc.reason,
                    "terminal_status": exc.terminal_status.value,
                },
            )
            self.runtime.repository.register_ingress_event(envelope)
            self.runtime.repository.mark_ingress_status(envelope.event_id, "rejected")
        except Exception:
            # If the database or workflow runtime is unavailable, keep the normalized event on disk.
            try:
                envelope = normalize_file_event(
                    path=path,
                    workspace_id=self.workspace_id,
                    workflow_type=self.workflow_type,
                    event_type=event_type,
                    stable=False,
                )
                self.spool.append(envelope.source_type, envelope)
            except Exception:
                raise


def watch_directory(path: Path, workspace_id: str, workflow_type: WorkflowType) -> None:
    path.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(
        DurableIngressHandler(workspace_id, workflow_type),
        str(path),
        recursive=False,
    )
    observer.start()
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()

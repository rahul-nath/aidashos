# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed cross-domain contract for workflow mixins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..contracts import IngressEvent, WorkflowResult, WorkflowType
from ..directives import DirectiveParser
from ..runtime import AppRuntime


class WorkflowMixinBase:
    runtime: AppRuntime

    def _start(self, workflow_type: WorkflowType, event: IngressEvent) -> str:
        raise NotImplementedError

    def _saga_delegate_fn(self, workflow_id: str) -> Callable[..., Mapping[str, Any]]:
        raise NotImplementedError

    def _fail_directory_embedding(
        self,
        workflow_id: str,
        directive: str,
        error: str,
        parser: DirectiveParser,
    ) -> WorkflowResult:
        raise NotImplementedError

    def audio_transcription(self, event: IngressEvent) -> WorkflowResult:
        raise NotImplementedError

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

import httpx
import pytest

from local_first_agent_os.model_probe import prove_model_answers


class _Client:
    def __init__(self, *, models: list[dict[str, Any]], answer: str):
        self.models = models
        self.answer = answer

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, path: str) -> httpx.Response:
        request = httpx.Request("GET", f"http://router{path}")
        return httpx.Response(200, request=request, json={"data": self.models})

    def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
        assert path == "/v1/chat/completions"
        assert json["model"] == "gemma4"
        request = httpx.Request("POST", f"http://router{path}")
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": self.answer}}]},
        )


def _factory(client: _Client):
    def create(**_kwargs):
        return client

    return create


def test_probe_requires_ready_exact_model_and_nonce_answer() -> None:
    client = _Client(
        models=[{"id": "gemma4", "status": {"value": "loaded"}}],
        answer="LOCAL_AGENT_MODEL_READY",
    )
    assert "LOCAL_AGENT_MODEL_READY" in prove_model_answers(
        base_url="http://router",
        model="gemma4",
        client_factory=_factory(client),
    )


@pytest.mark.parametrize(
    ("models", "answer", "message"),
    [
        ([{"id": "gemma4", "status": "unloaded"}], "LOCAL_AGENT_MODEL_READY", "not ready"),
        ([{"id": "qwen", "status": "loaded"}], "LOCAL_AGENT_MODEL_READY", "does not know"),
        ([{"id": "gemma4", "status": "loaded"}], "", "without the readiness token"),
    ],
)
def test_probe_rejects_false_positive_startup(
    models: list[dict[str, Any]], answer: str, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        prove_model_answers(
            base_url="http://router",
            model="gemma4",
            client_factory=_factory(_Client(models=models, answer=answer)),
        )

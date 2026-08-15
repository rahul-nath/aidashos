# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

import httpx

READY_STATES = {"loaded", "sleeping"}


def _status_value(item: dict[str, Any]) -> str:
    status = item.get("status")
    if isinstance(status, dict):
        return str(status.get("value") or "unknown")
    return str(status or "unknown")


def prove_model_answers(
    *,
    base_url: str,
    model: str,
    nonce: str = "LOCAL_AGENT_MODEL_READY",
    timeout_seconds: float = 120,
    client_factory: Callable[..., Any] = httpx.Client,
) -> str:
    """Require router residency and a nonce-bearing completion from one exact model."""
    with client_factory(base_url=base_url, timeout=timeout_seconds) as client:
        models_response = client.get("/models")
        models_response.raise_for_status()
        models = models_response.json().get("data", [])
        match = next((item for item in models if item.get("id") == model), None)
        if match is None:
            raise RuntimeError(f"router does not know model {model!r}")
        status = _status_value(match)
        if status not in READY_STATES:
            raise RuntimeError(f"model {model!r} is not ready; router status={status!r}")

        completion = client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Reply with this exact token and nothing else: {nonce}",
                    }
                ],
                "temperature": 0,
                "max_tokens": 32,
                "cache_prompt": False,
            },
        )
        completion.raise_for_status()
        content = str(completion.json()["choices"][0]["message"]["content"]).strip()
        if nonce not in content:
            raise RuntimeError(
                f"model {model!r} answered without the readiness token; response={content!r}"
            )
        return content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove one loaded llama-router model answers.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        prove_model_answers(
            base_url=args.base_url,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - CLI turns every failed proof into nonzero
        print(f"model readiness proof failed: {exc}", file=sys.stderr)
        return 1
    print(f"model readiness proof passed: {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

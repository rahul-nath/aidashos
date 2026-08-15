#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any


def _emit(name: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        value = "true" if value else "false"
    print(f"{name}={shlex.quote(str(value))}")


def _path_value(value: Any) -> Any:
    if isinstance(value, str):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"Whisper model paths must be absolute: {value}")
        return str(path)
    return value


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: whisper_registry_env.py <registry_toml>", file=sys.stderr)
        return 2

    registry_path = Path(sys.argv[1])
    data = tomllib.loads(registry_path.read_text(encoding="utf-8")) or {}

    for alias, meta in data.get("models", {}).items():
        if meta.get("role") != "asr":
            continue
        _emit("REGISTRY_WHISPER_ALIAS", alias)
        _emit("REGISTRY_WHISPER_RUNTIME", meta.get("runtime"))
        _emit("REGISTRY_WHISPER_BACKEND", meta.get("backend"))
        _emit(
            "REGISTRY_WHISPER_MODEL_PATH",
            _path_value(meta.get("ggml_path") or meta.get("gguf_path")),
        )
        _emit("REGISTRY_WHISPER_COREML_PATH", _path_value(meta.get("coreml_path")))
        _emit("REGISTRY_WHISPER_SERVER_URL", meta.get("server_url"))
        _emit("REGISTRY_WHISPER_PORT", meta.get("port"))
        _emit("REGISTRY_WHISPER_THREADS", meta.get("threads"))
        _emit("REGISTRY_WHISPER_LANGUAGE", meta.get("language"))
        _emit("REGISTRY_WHISPER_TRANSLATE", meta.get("translate"))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

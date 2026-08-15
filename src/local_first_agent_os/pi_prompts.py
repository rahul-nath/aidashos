# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PiPrompt:
    name: str
    version: str
    text: str
    defaults: dict[str, str] = field(default_factory=dict)

    def render(self, **substitutions: str) -> str:
        result = self.text
        for key, value in substitutions.items():
            result = result.replace(f"<<<{key.upper()}>>>", value)
        return result


class PiPromptRegistry:
    def __init__(self, path: Path):
        self._path = path
        self._prompts: dict[str, PiPrompt] = {}
        if not path.exists():
            return
        data = tomllib.loads(path.read_text(encoding="utf-8")) or {}
        for name, spec in (data.get("prompts") or {}).items():
            version = str(spec["version"])
            text = str(spec["text"])
            defaults = {k: str(v) for k, v in spec.items() if k not in {"version", "text"}}
            self._prompts[name] = PiPrompt(name=name, version=version, text=text, defaults=defaults)

    def get(self, name: str) -> PiPrompt:
        if name not in self._prompts:
            raise KeyError(f"Pi prompt '{name}' not registered. Check {self._path}.")
        return self._prompts[name]

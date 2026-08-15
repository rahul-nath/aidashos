#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path


def _resolve_model_artifact_path(alias: str, key: str, value: object) -> str:
    """Expand a registry path the same way ModelSpec does, then require absolute.

    llama.cpp receives this path as argv and expands nothing, so absolute is the
    invariant the preset file has to carry. The registry stores `~` so no
    operator's home directory is checked in, which means expansion happens here
    rather than being demanded of the config. This mirrors
    ModelSpec.expand_model_artifact_path; the two must agree, and the registry
    contract test asserts that they do.

    Existence is deliberately not checked. This generator runs on machines that
    have not downloaded every model, and refusing to emit a preset for a missing
    file would make the presets depend on which weights happen to be present.
    """

    if not isinstance(value, str):
        raise ValueError(f"models.{alias}.{key} must be a string path")
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        raise ValueError(f"models.{alias}.{key} must resolve to an absolute path: {value}")
    return str(expanded)


REASONING_SHORTHAND = re.compile(r"^(?:(off|full)|bounded\(\s*(\d+)\s*\))$")


def _reasoning_preset_lines(alias: str, raw: object) -> list[str]:
    """Translate the registry's `reasoning` shorthand into llama.cpp's own flags.

    The server takes two: `--reasoning on|off|auto` and `--reasoning-budget N`,
    where -1 is unrestricted and 0 ends thinking immediately. The registry says
    `off`, `bounded(N)`, or `full` instead, so that a role can ask for less
    thinking without naming a model's private chat-template spelling.

    This mirrors ``ReasoningPolicy`` in ``contracts.py``. The two live apart for
    the same import-weight reason ``_resolve_model_artifact_path`` does, and a
    registry contract test pins them together.
    """

    if not isinstance(raw, str):
        raise ValueError(f"models.{alias}.reasoning must be a string")
    match = REASONING_SHORTHAND.match(raw.strip())
    if match is None:
        raise ValueError(
            f"models.{alias}.reasoning must be 'off', 'full', or 'bounded(N)'; got {raw!r}"
        )
    keyword, budget = match.groups()
    if keyword == "off":
        return ["reasoning = off"]
    if keyword == "full":
        return ["reasoning = on", "reasoning-budget = -1"]
    return ["reasoning = on", f"reasoning-budget = {int(budget)}"]


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: gen_llama_presets.py <registry_toml> <output_ini>", file=sys.stderr)
        return 2
    registry_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    data = tomllib.loads(registry_path.read_text(encoding="utf-8")) or {}
    models = data.get("models", {})
    default_parallel = os.environ.get("LOCAL_AGENT_LLAMA_PARALLEL", "4")

    lines = ["version = 1", ""]
    seen_names: set[str] = set()
    for alias, meta in models.items():
        if meta.get("runtime", "llama.cpp") != "llama.cpp":
            continue
        for key in ("gguf_path", "mmproj_path"):
            if meta.get(key) is not None:
                meta[key] = _resolve_model_artifact_path(alias, key, meta[key])
        name = meta.get("server_model_name")
        ctx = meta.get("context_window")
        if not name or ctx is None:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        lines.append(f"[{name}]")
        lines.append(f"c = {ctx}")
        gguf_path = meta.get("gguf_path")
        if gguf_path:
            lines.append(f"model = {gguf_path}")
        role = meta.get("role")
        if role == "embedder":
            # llama.cpp returns 501 on /v1/embeddings unless the model is
            # loaded in embedding mode; Qwen3-Embedding uses last-token pooling.
            lines.append("embeddings = on")
            lines.append("pooling = last")
            # Embedding requests are independent documents. Prompt-cache state
            # provides little reuse here and llama.cpp can recycle the model
            # child after returning pooled vectors for some multi-ubatch inputs.
            lines.append("cache-prompt = false")
            # Context is divided across parallel slots. Workflowy records are
            # already semantic atoms and must not be split, so reserve the full
            # configured context window for one embedding sequence. HTTP arrays
            # are still accepted and processed serially by the server.
            lines.append("parallel = 1")
            # Pooled embeddings require the complete sequence to fit in one
            # physical ubatch. The largest current ai_stack_local idea is 8,118
            # tokens, so 8,192 preserves every exported semantic atom intact.
            lines.append("batch-size = 8192")
            lines.append("ubatch-size = 8192")
        else:
            lines.append(f"parallel = {meta.get('parallel', default_parallel)}")
        mmproj_path = meta.get("mmproj_path")
        if mmproj_path:
            lines.append(f"mmproj = {mmproj_path}")
        else:
            # The router auto-attaches any mmproj.gguf it finds next to the
            # model file; the registry is the source of truth for which roles
            # are multimodal, so disable auto-attach when none is declared.
            lines.append("mmproj-auto = 0")
        reasoning_format = meta.get("reasoning_format")
        if reasoning_format:
            # Models that write their answer inside a `<think>` tag need thought
            # parsing off, or the body is routed to `reasoning_content` and the
            # response's `content` field arrives empty.
            lines.append(f"reasoning-format = {reasoning_format}")
        reasoning = meta.get("reasoning")
        if reasoning:
            # Whether the model thinks at all, which `reasoning_format` does not
            # control: that one only decides where the thinking is put. Measured
            # on gemma4 classifying one milestone into a seven-value enum, 938
            # completion tokens in 87.6s with thinking on against 46 tokens in
            # 4.9s with it off - and the fast answer was the correct one, because
            # deliberation gave the model room to talk itself out of it.
            lines.extend(_reasoning_preset_lines(alias, reasoning))
        speculative = meta.get("speculative")
        if speculative:
            lines.append(f"spec-type = {speculative['type']}")
            draft_gguf_path = speculative.get("draft_gguf_path")
            if draft_gguf_path is not None:
                # A draft head living in its own file has to be named, or the
                # server takes `spec-type` and drafts from nothing. Models whose
                # head is inside the main GGUF declare no path and need no flag,
                # which is why this is emitted conditionally rather than always.
                lines.append(
                    "model-draft = "
                    f"{_resolve_model_artifact_path(alias, 'draft_gguf_path', draft_gguf_path)}"
                )
            if speculative.get("draft_n_max") is not None:
                lines.append(f"spec-draft-n-max = {speculative['draft_n_max']}")
            if speculative.get("draft_n_min") is not None:
                lines.append(f"spec-draft-n-min = {speculative['draft_n_min']}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

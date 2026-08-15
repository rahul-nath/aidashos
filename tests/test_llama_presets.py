# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError


def test_embedding_preset_reserves_atomic_sequence_capacity(tmp_path: Path) -> None:
    registry = tmp_path / "models.toml"
    output = tmp_path / "presets.ini"
    registry.write_text(
        """
[models.embedder]
role = "embedder"
server_model_name = "qwen-embed"
runtime = "llama.cpp"
context_window = 32768

[models.generator]
role = "general"
server_model_name = "gemma"
runtime = "llama.cpp"
context_window = 65536
""".strip(),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "gen_llama_presets.py"
    env = {**os.environ, "LOCAL_AGENT_LLAMA_PARALLEL": "3"}

    subprocess.run(
        [sys.executable, str(script), str(registry), str(output)],
        check=True,
        env=env,
    )

    presets = output.read_text(encoding="utf-8")
    embedder = presets.split("[qwen-embed]", 1)[1].split("[gemma]", 1)[0]
    generator = presets.split("[gemma]", 1)[1]
    assert "cache-prompt = false" in embedder
    assert "parallel = 1" in embedder
    assert "batch-size = 8192" in embedder
    assert "ubatch-size = 8192" in embedder
    assert "parallel = 3" in generator


def test_registry_owns_mmproj_parallel_and_speculation(tmp_path: Path) -> None:
    registry = tmp_path / "models.toml"
    output = tmp_path / "presets.ini"
    gguf = tmp_path / "chandra-q8.gguf"
    gguf.touch()
    mmproj = tmp_path / "chandra-mmproj.gguf"
    mmproj.touch()
    registry.write_text(
        f"""
[models.fallback]
role = "general_fallback"
server_model_name = "qwen-mtp"
runtime = "llama.cpp"
context_window = 8192
parallel = 1

[models.fallback.speculative]
type = "draft-mtp"
draft_n_max = 2

[models.ocr]
role = "ocr"
server_model_name = "chandra"
runtime = "llama.cpp"
context_window = 32768
gguf_path = "{gguf}"
mmproj_path = "{mmproj}"
reasoning_format = "none"

[models.generator]
role = "general"
server_model_name = "gemma"
runtime = "llama.cpp"
context_window = 65536
""".strip(),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "gen_llama_presets.py"

    subprocess.run(
        [sys.executable, str(script), str(registry), str(output)],
        check=True,
        env={**os.environ, "LOCAL_AGENT_LLAMA_PARALLEL": "4"},
    )

    presets = output.read_text(encoding="utf-8")
    fallback = presets.split("[qwen-mtp]", 1)[1].split("[chandra]", 1)[0]
    ocr = presets.split("[chandra]", 1)[1].split("[gemma]", 1)[0]
    generator = presets.split("[gemma]", 1)[1]
    assert "parallel = 1" in fallback
    assert "spec-type = draft-mtp" in fallback
    assert "spec-draft-n-max = 2" in fallback
    assert "spec-draft-n-min" not in fallback
    # A draft head built into the main GGUF declares no path and must not be
    # given a `-md` flag pointing at nothing.
    assert "model-draft" not in fallback
    assert f"mmproj = {mmproj}" in ocr
    assert f"model = {gguf}" in ocr
    assert "mmproj-auto" not in ocr
    # A model that writes its answer inside a `<think>` tag needs thought
    # parsing off, or llama.cpp routes the body to `reasoning_content` and the
    # response's `content` field arrives empty.
    assert "reasoning-format = none" in ocr
    # A projector file sitting next to the GGUF must not become an implicit
    # vision capability: undeclared means auto-attach is disabled.
    assert "mmproj-auto = 0" in fallback
    assert "mmproj-auto = 0" in generator
    assert "spec-type" not in generator
    assert "parallel = 4" in generator
    # Roles that declare no reasoning_format keep the server default.
    assert "reasoning-format" not in generator


def test_an_external_draft_head_reaches_the_preset(tmp_path: Path) -> None:
    """qwen3.6-27b-mtp kept its MTP draft head inside the main GGUF; the
    qwen3.8-27b-mtp that replaced it ships one as a separate file. Nothing fails
    loudly when the path is dropped: llama.cpp accepts `spec-type` on its own and
    drafts from nothing, so the whole cost of the omission is throughput that
    quietly never arrives, which no startup check would catch.
    """

    registry = tmp_path / "models.toml"
    output = tmp_path / "presets.ini"
    gguf = tmp_path / "qwen38.gguf"
    gguf.touch()
    draft = tmp_path / "qwen38-draft.gguf"
    draft.touch()
    registry.write_text(
        f"""
[models.fallback]
role = "general_fallback"
server_model_name = "qwen3.8-27b-mtp"
runtime = "llama.cpp"
context_window = 8192
parallel = 1
gguf_path = "{gguf}"

[models.fallback.speculative]
type = "draft-mtp"
draft_n_max = 2
draft_gguf_path = "{draft}"
""".strip(),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "gen_llama_presets.py"

    subprocess.run(
        [sys.executable, str(script), str(registry), str(output)],
        check=True,
        env={**os.environ, "LOCAL_AGENT_LLAMA_PARALLEL": "4"},
    )

    presets = output.read_text(encoding="utf-8")
    assert f"model = {gguf}" in presets
    assert f"model-draft = {draft}" in presets
    assert "spec-type = draft-mtp" in presets


def test_reasoning_shorthand_becomes_llama_cpp_flags(tmp_path: Path) -> None:
    """`off`/`bounded(N)`/`full` is the registry's vocabulary; llama.cpp's is
    `--reasoning on|off` plus `--reasoning-budget N`, where -1 is unrestricted.
    Emitting the shorthand verbatim would put `reasoning = bounded(256)` in the
    preset, which the server cannot read.
    """

    registry = tmp_path / "models.toml"
    output = tmp_path / "presets.ini"
    registry.write_text(
        """
[models.quiet]
role = "general"
server_model_name = "quiet"
context_window = 8192
reasoning = "off"

[models.capped]
role = "general_fallback"
server_model_name = "capped"
context_window = 8192
reasoning = "bounded(256)"

[models.loud]
role = "deliberator"
server_model_name = "loud"
context_window = 8192
reasoning = "full"
""".strip(),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "gen_llama_presets.py"

    subprocess.run([sys.executable, str(script), str(registry), str(output)], check=True)

    presets = output.read_text(encoding="utf-8")
    quiet = presets.split("[quiet]", 1)[1].split("[capped]", 1)[0]
    capped = presets.split("[capped]", 1)[1].split("[loud]", 1)[0]
    loud = presets.split("[loud]", 1)[1]
    assert "reasoning = off" in quiet
    assert "reasoning-budget" not in quiet
    assert "reasoning = on" in capped
    assert "reasoning-budget = 256" in capped
    assert "reasoning = on" in loud
    assert "reasoning-budget = -1" in loud


def test_an_unreadable_reasoning_value_stops_the_generator(tmp_path: Path) -> None:
    """The generator refuses rather than passing the string through. A preset
    carrying a value llama.cpp cannot parse is a startup failure at best and a
    silently-ignored setting at worst."""

    registry = tmp_path / "models.toml"
    registry.write_text(
        """
[models.broken]
role = "general"
server_model_name = "broken"
context_window = 8192
reasoning = "kind of"
""".strip(),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "gen_llama_presets.py"

    result = subprocess.run(
        [sys.executable, str(script), str(registry), str(tmp_path / "out.ini")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "kind of" in result.stderr


def test_generator_and_model_spec_read_reasoning_shorthand_identically() -> None:
    """Same pin as the artifact-path validators, for the same reason: two readers
    of one config field drift apart unless something asserts they agree."""

    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    try:
        from gen_llama_presets import _reasoning_preset_lines
    finally:
        sys.path.pop(0)
    from local_first_agent_os.contracts import ReasoningPolicy

    for shorthand in ("off", "bounded(256)", "full"):
        policy = ReasoningPolicy.model_validate(shorthand)
        lines = _reasoning_preset_lines("alias", shorthand)
        if policy.mode == "off":
            assert lines == ["reasoning = off"]
        elif policy.mode == "full":
            assert lines == ["reasoning = on", "reasoning-budget = -1"]
        else:
            assert lines == ["reasoning = on", f"reasoning-budget = {policy.budget_tokens}"]

    for bad in ("bounded()", "sometimes"):
        with pytest.raises(ValueError):
            ReasoningPolicy.model_validate(bad)
        with pytest.raises(ValueError):
            _reasoning_preset_lines("alias", bad)


def test_generator_runs_against_the_checked_in_registry(tmp_path: Path) -> None:
    """The two tests above build synthetic registries, so neither one ever ran
    the generator against the file the runtime actually loads. A validator that
    rejected the checked-in registry therefore passed lint, types, and the whole
    suite while breaking startup for anyone who ran the generator.
    """

    repo_root = Path(__file__).parents[1]
    registry = repo_root / "configs" / "model_registry.toml"
    output = tmp_path / "presets.ini"
    script = repo_root / "scripts" / "gen_llama_presets.py"

    subprocess.run(
        [sys.executable, str(script), str(registry), str(output)],
        check=True,
    )

    presets = output.read_text(encoding="utf-8")
    assert presets.startswith("version = 1")
    model_lines = [line for line in presets.splitlines() if line.startswith("model = ")]
    assert model_lines, "the checked-in registry should yield at least one llama.cpp preset"
    for line in model_lines:
        emitted = line.removeprefix("model = ")
        # llama.cpp expands nothing, so every path reaching the preset file must
        # already be absolute even though the registry stores `~`.
        assert Path(emitted).is_absolute(), emitted
        assert "~" not in emitted, emitted


def test_generator_and_model_spec_expand_registry_paths_identically() -> None:
    """Two validators over the same field is one too many, so pin them together.

    ModelSpec.expand_model_artifact_path and the generator's
    _resolve_model_artifact_path both turn a registry path into an absolute one.
    They live in different modules for import-weight reasons, and this is what
    stops them drifting apart.
    """

    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    try:
        from gen_llama_presets import _resolve_model_artifact_path
    finally:
        sys.path.pop(0)

    from local_first_agent_os.contracts import ModelRole, ModelSpec

    for raw in ("~/models/gemma4/model.gguf", "$HOME/models/x/model.gguf"):
        spec = ModelSpec(
            alias="a",
            role=ModelRole.OCR,
            model_id="m",
            server_model_name="m",
            gguf_path=raw,
        )
        assert spec.gguf_path == _resolve_model_artifact_path("a", "gguf_path", raw)

    with pytest.raises(ValueError):
        _resolve_model_artifact_path("a", "gguf_path", "models/relative.gguf")
    with pytest.raises(ValidationError):
        ModelSpec(
            alias="a",
            role=ModelRole.OCR,
            model_id="m",
            server_model_name="m",
            gguf_path="models/relative.gguf",
        )

#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render the configuration reference from the Settings model.

`.env.example` is the hand-maintained version of this document, and it has
already drifted: most environment variables the scripts read are absent from it,
and four of the ones present contradict the model's defaults. A second
hand-written list would drift the same way, so this is generated and checked,
exactly as `dump_openapi.py` generates and checks the API schema. Checking is
likewise the default here and writing takes `--write`: an accidental check costs
a printed diagnosis, while an accidental write silently launders real drift into
"no changes".

Two things are rendered from one model. Feature flags come first, because they
are the decisions an operator actually makes; everything else follows, grouped
by prefix. A flag is whatever carries `feature_flag` in its `json_schema_extra`,
so the set is derivable from the code rather than from someone's judgement about
what counts.

The shell surface is listed too, and separately. Roughly fifty environment
variables are read only by `scripts/*.sh`, which is fine - most are parameters
to one script - but two of them decide something the model also decides, and a
reader deserves to see that rather than discover it.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS_PATH = _REPO_ROOT / "src" / "local_first_agent_os" / "settings.py"
_REFERENCE_PATH = _REPO_ROOT / "docs" / "configuration.md"
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"

# Environment variables read by scripts/ that the application never sees, and
# that decide something Settings also decides. These are the only shell-side
# entries worth calling out; the rest are parameters to a single script.
_SPLIT_DECISIONS: tuple[tuple[str, str, str], ...] = (
    (
        "LOCAL_AGENT_LLAMA_PORT",
        "llama_base_url",
        "An explicit override. The scripts otherwise take the port from "
        "LOCAL_AGENT_LLAMA_BASE_URL, so moving the model's URL moves what they "
        "start and stop; 8080 remains the default on both sides.",
    ),
    (
        "LOCAL_AGENT_MODEL_REGISTRY_PATH",
        "config_dir",
        "An explicit override. The scripts otherwise derive the registry from "
        "LOCAL_AGENT_CONFIG_DIR, the same way Settings.model_registry_path does.",
    ),
    (
        "LOCAL_AGENT_START_ASR",
        "(none)",
        "Decides whether ASR exists in the running system. It cannot live in Settings, "
        "because the decision is made before Python starts; see the shell flags below.",
    ),
    (
        "LOCAL_AGENT_PI_FORCE_DIRECT",
        "pi_handoff_to_daemon",
        "The legacy spelling, still honoured because the handoff docs tell operators "
        "to set it around the daemon's Bad file descriptor bug. Inverted: "
        "FORCE_DIRECT=1 means pi_handoff_to_daemon is false.",
    ),
)

_SHELL_FLAGS: tuple[tuple[str, str, str], ...] = (
    (
        "scripts/start-agent-runtime.sh",
        "--with-asr / --no-asr",
        "Start whisper.cpp for this run. Off by default: it holds a multi-gigabyte "
        "model resident for the whole session.",
    ),
)


def _isolate_runtime_environment() -> None:
    """Describe the code, not a developer's machine.

    The reference is a property of the model's declared defaults. Reading whoever
    ran this generator's `.env` would make the committed artifact depend on whose
    laptop produced it, which is the failure mode `.env.example` already has.
    """

    scratch = Path(tempfile.mkdtemp(prefix="config-reference-"))
    for name in list(os.environ):
        if name.startswith(("LOCAL_AGENT_", "AGENT_COORDINATION_")):
            del os.environ[name]
    os.environ["LOCAL_AGENT_ARTIFACT_ROOT"] = str(scratch / "artifacts")
    os.environ["LOCAL_AGENT_SPOOL_DIR"] = str(scratch / "spool")


def field_rationales() -> dict[str, str]:
    """The comment block written above each field, keyed by field name.

    A `Field(description=...)` holds one line, which is enough to say what a
    setting does and never enough to say why it exists. The reasoning that
    matters - why a reader-only database URL sits beside the DBOS system one,
    why a version is pinned - is already written as ordinary comments above the
    fields, where someone reading the code sees it. Reading them here means that
    prose reaches the reference without being duplicated into it, so there is
    still one place to change.
    """

    source = _SETTINGS_PATH.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    settings = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )

    # Strictly the comment directly above the field. A comment that introduces a
    # group of fields is indistinguishable from one describing a single field, so
    # this does not try to guess: it reports what is written where, and a group
    # header is fixed by splitting it in settings.py rather than by inference
    # here.
    rationales: dict[str, str] = {}
    for node in settings.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        collected: list[str] = []
        index = node.lineno - 2  # zero-based, and the line above the assignment
        while index >= 0:
            stripped = lines[index].strip()
            if not stripped.startswith("#"):
                break
            collected.append(stripped.lstrip("#").strip())
            index -= 1
        if collected:
            rationales[node.target.id] = " ".join(reversed(collected))
    return rationales


def _env_names(name: str, field: Any) -> list[str]:
    alias = field.validation_alias
    if alias is None:
        return [f"LOCAL_AGENT_{name.upper()}"]
    return [str(choice) for choice in getattr(alias, "choices", [alias])]


def _render_default(field: Any) -> str:
    from pydantic_core import PydanticUndefined

    if field.default is not PydanticUndefined and field.default is not None:
        return f"`{_cell(str(field.default))}`"
    if field.default_factory is not None:
        return "_derived_"
    if field.default is None:
        return "`None`"
    return "_required_"


def _render_type(field: Any) -> str:
    annotation = field.annotation
    text = getattr(annotation, "__name__", None) or str(annotation)
    text = text.replace("local_first_agent_os.settings.", "").replace("typing.", "")
    # A union's own separator is also the table's, so it has to be escaped or the
    # row silently gains a column.
    return _cell(text)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _group_of(name: str) -> str:
    head = name.split("_", 1)[0]
    return {
        "dbos": "DBOS",
        "otel": "Observability",
        "pyroscope": "Observability",
        "memory": "Observability",
        "lifecycle": "Lifecycle maintenance",
        "chrome": "Chrome",
        "whisper": "ASR",
        "llama": "Local models",
        "pi": "Pi daemon",
        "saga": "Saga execution",
        "ledger": "Ledger",
        "coordination": "Coordination ledger",
        "minio": "Artifact storage",
        "workflowy": "Workflowy",
    }.get(head, "General")


def render_reference() -> str:
    _isolate_runtime_environment()
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from local_first_agent_os.settings import Settings

    fields = Settings.model_fields
    flags = {
        name: field
        for name, field in fields.items()
        if (field.json_schema_extra or {}).get("feature_flag")
    }

    lines: list[str] = [
        "# Configuration reference",
        "",
        "<!-- Generated by scripts/dump_config_reference.py. Do not edit by hand. -->",
        "",
        "Every value the application reads comes from `src/local_first_agent_os/settings.py`.",
        "This file is generated from that model, so it cannot drift from it; "
        "`--check` fails when it has.",
        "",
        "## Feature flags",
        "",
        "These change what the product does, as opposed to where it points or how fast it runs.",
        "A field is listed here when it carries `feature_flag` in its "
        "`json_schema_extra`, so this set is derived rather than curated.",
        "",
        "| Setting | Environment | States | Default | What it decides |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in sorted(flags):
        field = flags[name]
        env = "<br>".join(f"`{item}`" for item in _env_names(name, field))
        description = _cell(" ".join((field.description or "_undescribed_").split()))
        lines.append(
            f"| `{name}` | {env} | `{_render_type(field)}` | "
            f"{_render_default(field)} | {description} |"
        )

    lines += [
        "",
        "## Everything else",
        "",
        "Endpoints, paths, credentials, and tuning values. Not flags: changing one "
        "moves the product, it does not alter what the product is.",
        "",
    ]
    grouped: dict[str, list[str]] = {}
    for name, field in sorted(fields.items()):
        if name in flags:
            continue
        env = "<br>".join(f"`{item}`" for item in _env_names(name, field))
        description = _cell(" ".join((field.description or "").split())) or "-"
        grouped.setdefault(_group_of(name), []).append(
            f"| `{name}` | {env} | {_render_default(field)} | {description} |"
        )
    for group in sorted(grouped):
        lines += [
            f"### {group}",
            "",
            "| Setting | Environment | Default | Notes |",
            "| --- | --- | --- | --- |",
            *grouped[group],
            "",
        ]

    rationales = field_rationales()
    if rationales:
        lines += [
            "## Why these exist",
            "",
            "Read from the comments above each field in `settings.py`, so the reasoning "
            "lives next to the code and reaches this file without being written twice. "
            "A setting missing here has no comment explaining it yet.",
            "",
        ]
        for name in sorted(rationales):
            marker = " _(flag)_" if name in flags else ""
            lines += [f"**`{name}`**{marker}", "", rationales[name], ""]

    lines += [
        "## Outside the model",
        "",
        "Configuration the application cannot see. Most environment variables in "
        "`scripts/` are parameters to a single script and belong there.",
        "These are the ones that decide something `Settings` also decides, which "
        "means two defaults can disagree.",
        "",
        "| Environment | Overlaps | Why it matters |",
        "| --- | --- | --- |",
    ]
    for env_name, setting, why in _SPLIT_DECISIONS:
        lines.append(f"| `{env_name}` | `{setting}` | {why} |")

    lines += [
        "",
        "### Shell flags",
        "",
        "Decisions made before Python starts, so they cannot be model fields.",
        "",
        "| Script | Flag | What it decides |",
        "| --- | --- | --- |",
    ]
    for script, flag, why in _SHELL_FLAGS:
        lines.append(f"| `{script}` | `{flag}` | {why} |")

    lines += [
        "",
        "### `.env.example`",
        "",
        "Generated by the same script, from the same defaults, so the two cannot "
        "disagree. It used to be hand-maintained and had drifted: most variables the "
        "scripts read were missing from it, and four contradicted the model.",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_env_example() -> str:
    """A copyable `.env` showing what the model actually defaults to.

    Machine-specific defaults are commented out rather than baked in: a home
    directory from whoever ran the generator is worse than no value at all.
    """

    from pydantic_core import PydanticUndefined

    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from local_first_agent_os.settings import Settings

    lines = [
        "# Generated by scripts/dump_config_reference.py. Do not edit by hand.",
        "#",
        "# Every value below is the model default from src/local_first_agent_os/settings.py.",
        "# Commented entries are derived at runtime (usually from your home directory) or",
        "# have no default; uncomment only what you mean to override.",
        "#",
        "# See docs/configuration.md for what each one decides.",
        "",
    ]
    rationales = field_rationales()
    flags: list[str] = []
    rest: list[str] = []
    for name, field in sorted(Settings.model_fields.items()):
        env_name = _env_names(name, field)[0]
        target = flags if (field.json_schema_extra or {}).get("feature_flag") else rest
        if field.description:
            for chunk in _wrap(" ".join(field.description.split())):
                target.append(f"# {chunk}")
        if name in rationales:
            if field.description:
                target.append("#")
            for chunk in _wrap(rationales[name]):
                target.append(f"# {chunk}")
        if field.default_factory is not None:
            target.append(f"# {env_name}=  # derived at runtime")
        elif field.default is PydanticUndefined:
            target.append(f"# {env_name}=  # no default; set this to use the feature")
        else:
            target.append(f"{env_name}={_env_value(field.default)}")
        target.append("")

    lines += ["# --- Feature flags ---------------------------------------------", ""]
    lines += flags
    lines += ["# --- Everything else ------------------------------------------", ""]
    lines += rest
    return "\n".join(lines).rstrip("\n") + "\n"


def _wrap(text: str, width: int = 76) -> list[str]:
    out: list[str] = []
    line = ""
    for word in text.split(" "):
        candidate = f"{line} {word}".strip()
        if len(candidate) <= width or not line:
            line = candidate
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out


def _env_value(default: Any) -> str:
    if isinstance(default, bool):
        return "true" if default else "false"
    if default is None:
        return ""
    if isinstance(default, (list, tuple, dict, set)):
        import json

        return json.dumps(default if not isinstance(default, (tuple, set)) else list(default))
    if hasattr(default, "value"):
        return str(default.value)
    return str(default)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="Overwrite the committed reference and .env.example from the model.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="The default: fail when the committed reference differs from the model.",
    )
    args = parser.parse_args()

    if not args.write:
        stale = [
            path
            for path, rendered in _render_in_subprocess().items()
            if (path.read_text(encoding="utf-8") if path.exists() else "") != rendered
        ]
        if stale:
            print(
                f"Out of date: {', '.join(str(path) for path in stale)}\n"
                "Run: uv run python scripts/dump_config_reference.py --write",
                file=sys.stderr,
            )
            return 1
        print("docs/configuration.md and .env.example match the settings model")
        return 0

    _REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REFERENCE_PATH.write_text(render_reference(), encoding="utf-8")
    _ENV_EXAMPLE_PATH.write_text(render_env_example(), encoding="utf-8")
    print(f"wrote {_REFERENCE_PATH} and {_ENV_EXAMPLE_PATH}")
    return 0


def _render_in_subprocess() -> dict[Path, str]:
    """Render both outputs with a clean interpreter, without touching the tree.

    `--check` may run inside a process that already imported settings under a
    developer's environment, and a cached model would compare the wrong thing.
    """

    script = (
        "import json, sys;"
        "sys.path.insert(0, 'scripts');"
        "import dump_config_reference as d;"
        "print(json.dumps([d.render_reference(), d.render_env_example()]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("LOCAL_AGENT_")},
    )
    import json

    reference, env_example = json.loads(completed.stdout)
    return {_REFERENCE_PATH: reference, _ENV_EXAMPLE_PATH: env_example}


if __name__ == "__main__":
    raise SystemExit(main())

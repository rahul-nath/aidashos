# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`KEY=` in a .env file means unset, and the generated template relies on it.

`.env.example` is rendered from the settings model, and an optional field with
no default renders as a bare `KEY=` line. Copying that template to `.env` is
the first step of every install, so whatever those lines mean is what every
freshly installed machine gets. They used to mean "the empty value", which
broke two fields outright:

- `dict | None` rejects `''` during field validation, so `Settings()` could not
  be constructed at all and every caller inherited the failure.
- `Path | None` accepts `''` and resolves it to `.`, so a fetch-script setting
  pointed a subprocess at the current directory.

The second is why a fresh clone's `uv run pytest` failed sixteen tests right
after `make` wrote the `.env`, while the same suite passed before it existed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from local_first_agent_os.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
_BARE_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=$", re.MULTILINE)


def _bare_assignment_keys() -> frozenset[str]:
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    return frozenset(match.group(1) for match in _BARE_ASSIGNMENT.finditer(example))


def _field_for_env_name(env_name: str):
    """The settings field a .env line sets, however that line names it.

    Two spellings reach the same field. A field declaring
    `validation_alias=AliasChoices(...)` is set by one of those alias strings
    verbatim; a field declaring none is set by `env_prefix` plus its own name.
    """

    from pydantic import AliasChoices

    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, str) and alias == env_name:
            return field
        if isinstance(alias, AliasChoices) and env_name in alias.choices:
            return field
        if alias is None and env_name == f"LOCAL_AGENT_{name.upper()}":
            return field
    return None


def test_the_template_only_leaves_optional_fields_blank() -> None:
    """The pin that keeps the rule honest as fields are added.

    A required field rendered as a bare `KEY=` line would be handed an empty
    string that this rule deliberately does not strip, and the failure would
    appear on a stranger's machine at install time rather than here.
    """

    from local_first_agent_os.settings import _field_accepts_none

    not_optional = []
    for env_name in sorted(_bare_assignment_keys()):
        field = _field_for_env_name(env_name)
        assert field is not None, f"{env_name} in .env.example matches no settings field"
        if not _field_accepts_none(field):
            not_optional.append(env_name)

    assert not not_optional, (
        "these .env.example lines assign a blank value to a field that is not "
        f"optional: {not_optional}. Give the field a default, or make it optional."
    )


@pytest.mark.parametrize("env_name", sorted(_bare_assignment_keys()))
def test_every_blank_template_line_constructs_settings(
    env_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_name, "")

    Settings()


def test_blank_optional_path_is_unset_rather_than_the_current_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Path('')` resolves to `.`, which is a directory a subprocess cannot exec."""

    monkeypatch.setenv("LOCAL_AGENT_WORKFLOWY_FETCH_SCRIPT", "")
    monkeypatch.setenv("LOCAL_AGENT_APPLE_NOTES_FETCH_SCRIPT", "")

    settings = Settings()

    assert settings.workflowy_fetch_script is None
    assert settings.apple_notes_fetch_script is None


def test_a_real_value_still_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule strips blanks, not values, and not whitespace-bearing ones."""

    monkeypatch.setenv("LOCAL_AGENT_WORKFLOWY_FETCH_SCRIPT", "/tmp/fetch.sh")
    monkeypatch.setenv("WF_API_KEY", " secret ")

    settings = Settings()

    assert settings.workflowy_fetch_script == Path("/tmp/fetch.sh")
    assert settings.workflowy_api_key == " secret "


def test_a_non_optional_field_keeps_the_blank_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule is scoped to fields whose type says absent is representable.

    `llama_base_url` is a plain `str` with a default. Stripping its blank would
    quietly restore that default, which reads as the operator's setting being
    honored when it was discarded. It keeps the empty string instead, and
    whatever validates it downstream is what complains.
    """

    monkeypatch.setenv("LOCAL_AGENT_LLAMA_BASE_URL", "")

    assert Settings().llama_base_url == ""

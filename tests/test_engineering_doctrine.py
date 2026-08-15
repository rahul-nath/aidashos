# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE


def test_engineering_doctrine_has_stable_content_addressed_provenance() -> None:
    provenance = CURRENT_ENGINEERING_DOCTRINE.provenance_payload()

    assert provenance["contract_name"] == "engineering_doctrine"
    assert provenance["schema_version"] == "engineering_doctrine.v2"
    assert len(provenance["sha256"]) == 64
    assert CURRENT_ENGINEERING_DOCTRINE.matches_provenance(provenance)
    assert not CURRENT_ENGINEERING_DOCTRINE.matches_provenance({**provenance, "sha256": "0" * 64})


def test_engineering_doctrine_is_bounded_and_excludes_source_texts() -> None:
    rendered = CURRENT_ENGINEERING_DOCTRINE.render_prompt()

    assert len(rendered) < 6_000
    assert "logic level" in rendered
    assert "representable states aligned with valid states" in rendered
    assert "confidential source texts are local, on-demand references only" in rendered

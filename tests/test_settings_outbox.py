# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_first_agent_os.settings import Settings


def test_ledger_outbox_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.ledger_outbox_destination is None


def test_ledger_outbox_requires_a_complete_destination() -> None:
    settings = Settings.model_validate(
        {
            "ledger_outbox": {
                "mode": "configured",
                "consumer": "reactor",
                "topic": "coordination",
            }
        }
    )

    assert settings.ledger_outbox_destination == ("reactor", "coordination")


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "configured", "consumer": "", "topic": "coordination"},
        {"mode": "configured", "consumer": "  ", "topic": "coordination"},
        {"mode": "configured", "consumer": "reactor", "topic": ""},
        {"mode": "configured", "consumer": "reactor", "topic": "  "},
        {"mode": "configured", "consumer": "reactor"},
        {"mode": "configured", "topic": "coordination"},
        {"mode": "disabled", "consumer": "reactor", "topic": "coordination"},
    ],
)
def test_ledger_outbox_rejects_partial_destinations(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"ledger_outbox": payload})

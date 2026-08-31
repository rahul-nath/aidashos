# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The single-operator trust root for privileged mutations."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

OPERATOR_TOKEN_ENV: Final = "LOCAL_AGENT_OPERATOR_TOKEN"
OPERATOR_TOKEN_FILE_ENV: Final = "LOCAL_AGENT_OPERATOR_TOKEN_FILE"
DEFAULT_OPERATOR_TOKEN_FILE: Final = Path.home() / ".local-agent" / "operator.token"
_OPERATOR_PROOF = object()


class OperatorIdentityRefused(PermissionError):
    code = "operator_token_required"

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file
        super().__init__(f"operator token required; load {token_file} into {OPERATOR_TOKEN_ENV}")


@dataclass(frozen=True)
class OperatorActor:
    principal: str
    _proof: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class AgentActor:
    principal: str
    session_id: str


def operator_token_file() -> Path:
    configured = os.environ.get(OPERATOR_TOKEN_FILE_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_OPERATOR_TOKEN_FILE


def verify_operator_actor(principal: str) -> OperatorActor:
    """Verify the process-held token and return the only valid operator actor."""

    token_file = operator_token_file()
    presented = os.environ.get(OPERATOR_TOKEN_ENV, "")
    try:
        stored = token_file.read_text(encoding="utf-8").strip()
        mode = token_file.stat().st_mode & 0o777
    except OSError as exc:
        raise OperatorIdentityRefused(token_file) from exc
    if mode & 0o077 or not stored or not hmac.compare_digest(presented, stored):
        raise OperatorIdentityRefused(token_file)
    return OperatorActor(principal=principal, _proof=_OPERATOR_PROOF)


def require_verified_operator(actor: OperatorActor) -> None:
    if actor._proof is not _OPERATOR_PROOF:
        raise OperatorIdentityRefused(operator_token_file())


__all__ = [
    "AgentActor",
    "DEFAULT_OPERATOR_TOKEN_FILE",
    "OPERATOR_TOKEN_ENV",
    "OPERATOR_TOKEN_FILE_ENV",
    "OperatorActor",
    "OperatorIdentityRefused",
    "operator_token_file",
    "require_verified_operator",
    "verify_operator_actor",
]

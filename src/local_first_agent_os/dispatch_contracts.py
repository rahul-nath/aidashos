# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Closed diagnostics for invalid report ingress, separate from execution failure."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DispatchContractCode(StrEnum):
    MALFORMED_JSON = "MALFORMED_JSON"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    INCONSISTENT_OUTCOME = "INCONSISTENT_OUTCOME"


class DispatchIngressFailureCode(StrEnum):
    REPORT_CONTRACT_VIOLATION = "dispatch_report_contract_violation"
    DIAGNOSTIC_PERSISTENCE_UNAVAILABLE = "dispatch_diagnostic_persistence_unavailable"


class DispatchContractEvent(BaseModel):
    """The retained diagnostic schema shared by admission and operator attention."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["dispatch_contract_violation.v1"] = "dispatch_contract_violation.v1"
    intent_id: str = Field(min_length=1)
    code: DispatchContractCode
    payload_sha256: str | None = Field(pattern="^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class InvalidDispatchReport:
    """Diagnostic evidence, never a substitute execution outcome or receipt.

    A digest identifies the rejected serialized input without copying its
    credentials, output, attacker-controlled field names, or exception text.
    Nonserializable Python objects have no wire digest.
    """

    code: DispatchContractCode
    payload_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.code, DispatchContractCode):
            raise TypeError("dispatch contract diagnostics require a declared code")
        if self.payload_sha256 is not None and (
            len(self.payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.payload_sha256)
        ):
            raise ValueError("dispatch diagnostic digest must be a lowercase SHA-256")

    @classmethod
    def from_input(cls, code: DispatchContractCode, value: object) -> InvalidDispatchReport:
        try:
            serialized = (
                value
                if isinstance(value, str)
                else json.dumps(value, sort_keys=True, allow_nan=False)
            )
            digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, RecursionError):
            return cls(code, None)
        return cls(code, digest)


class DispatchContractViolation(ValueError):
    """Typed stop at a contract boundary; callers can retain the safe diagnostic."""

    def __init__(self, diagnostic: InvalidDispatchReport) -> None:
        self.diagnostic = diagnostic
        super().__init__(f"dispatch report contract violation: {diagnostic.code.value}")

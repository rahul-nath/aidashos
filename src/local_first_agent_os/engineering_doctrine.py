# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Versioned engineering doctrine injected into frontier-agent tasks.

The complete course notes remain local reference material. This module owns the
bounded, model-facing contract and the content-addressed provenance stamped by
the host into durable execution and review records.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineeringDoctrine:
    """One immutable model-facing doctrine contract."""

    schema_version: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def provenance_payload(self) -> dict[str, str]:
        return {
            "contract_name": "engineering_doctrine",
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }

    def render_prompt(self) -> str:
        return (
            "Engineering doctrine contract:\n"
            f"Version: {self.schema_version}\n"
            f"SHA-256: {self.sha256}\n\n"
            f"{self.text}"
        )

    def matches_provenance(self, value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        return all(
            value.get(key) == expected for key, expected in self.provenance_payload().items()
        )


ENGINEERING_DOCTRINE_V1 = "\n".join(
    (
        ("This contract governs all senior and staff work, independent of the target repository."),
        "",
        "Design obligations:",
        (
            "- Reason at the logic level. For each material line or contract, identify the "
            "assumptions and design decisions elsewhere that make it correct."
        ),
        (
            "- Minimize code knowledge. Keep one owner for each design decision, hide "
            "changeable implementation secrets behind that owner, and deduplicate knowledge "
            "rather than merely similar text."
        ),
        (
            "- Embed design in structure, types, and names. Design data contracts before "
            "control flow so another engineer can reconstruct the assumption tree without "
            "the original author."
        ),
        (
            "- Keep representable states aligned with valid states. Prefer typed IDs, "
            "parameter objects, enums, sum types, and explicit variants over strings, "
            "boolean-plus-optional-field products, and nullable ambiguity."
        ),
        (
            "- Enforce strict boundaries. Make programmer errors and impossible states fail "
            "visibly. Handle genuine runtime failures as runtime failures instead of silently "
            "coercing contract violations."
        ),
        (
            "- Design durable edges carefully. Public APIs, persisted artifacts, file formats, "
            "approval boundaries, and observable behavior must remain explicit, compatible, "
            "and hard to misuse."
        ),
        (
            "- Future-proof by hiding likely-to-change assumptions and providing narrow "
            "extension seams. Do not build speculative components or create combinatorial "
            "flag/version states."
        ),
        "",
        "Practice obligations:",
        (
            "- Preserve the repository's accepted architecture unless current repository "
            "evidence proves a concrete contradiction."
        ),
        (
            "- Put behavior in the module that owns the relevant decision. Do not add "
            "cross-module conditionals that leak another module's secret."
        ),
        (
            "- Prefer the smallest data-model or interface change that makes the desired state "
            "explicit over an ad hoc branch."
        ),
        (
            "- Name concepts by their real role. Do not create generic Utils, Misc, or Helpers "
            "modules and do not use string keys where a typed concept is available."
        ),
        ("- Before adding a runtime assertion, try to make the invalid state unrepresentable."),
        (
            "- De-risk the riskiest boundary first and validate the invariant with focused "
            "tests plus the repository's required checks."
        ),
        (
            "- Keep durable workflow truth in the ledger and typed artifacts, dependencies in "
            "explicit edges, and operator authority in approval gates."
        ),
        "",
        "Source-handling boundary:",
        (
            "- This is the complete model-facing doctrine for routine work. The confidential "
            "source texts are local, on-demand references only. Do not load, copy, quote, or "
            "inject them wholesale."
        ),
    )
)


# v2 is v1 plus the execution boundary, expressed as an extension rather than a
# second copy so the shared text has one owner.
#
# A new version rather than an edit to v1, because the version string and the
# content hash are stamped together into durable review provenance, and
# `matches_provenance` gates merge eligibility off them. Editing v1 in place
# would make one version name two different texts and would silently disqualify
# reviews conducted under the earlier one. There are no v1-stamped reviews in
# this ledger yet, which makes now the cheap moment to establish the rule rather
# than a reason to skip it.
ENGINEERING_DOCTRINE_V2 = "\n".join(
    (
        ENGINEERING_DOCTRINE_V1,
        "",
        "Execution boundary:",
        (
            "- You may fan out into your own harness-local subagents. They are yours to "
            "direct, they inherit the tool permissions this process was given, and the "
            "system does not schedule or observe them."
        ),
        (
            "- What the system judges is the diff your leased worktree contains when you "
            "finish. Work done by a subagent and work done by you are indistinguishable to "
            "it, and both face the same verification commands and the same reviewer."
        ),
        (
            "- Do not spawn agents outside your worktree, and do not start processes intended "
            "to outlive your task. A tiered subagent in this system is a scheduled ledger "
            "task, which only the host may create."
        ),
        (
            "- Execution history is a query, not an inference. When an `agent_ledger` tool "
            "surface is available to you, read it before asserting that something has or has "
            "not run: it answers what has executed, which resident loops are live, and what "
            "dispatch intents exist. It is read-only, and its absence is not an error."
        ),
    )
)


CURRENT_ENGINEERING_DOCTRINE = EngineeringDoctrine(
    schema_version="engineering_doctrine.v2",
    text=ENGINEERING_DOCTRINE_V2,
)


__all__ = [
    "CURRENT_ENGINEERING_DOCTRINE",
    "ENGINEERING_DOCTRINE_V1",
    "ENGINEERING_DOCTRINE_V2",
    "EngineeringDoctrine",
]

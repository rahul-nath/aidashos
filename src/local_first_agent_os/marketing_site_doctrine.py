# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Versioned, cross-harness doctrine for marketing-site work.

The source course material remains local. This module owns the bounded text that
the host may inject into either Claude or Codex when a durable task explicitly
selects the marketing-site reference pack.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketingSiteDoctrine:
    schema_version: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def provenance_payload(self) -> dict[str, str]:
        return {
            "contract_name": "marketing_site_doctrine",
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }

    def render_prompt(self) -> str:
        return (
            "Marketing-site doctrine contract:\n"
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


MARKETING_SITE_DOCTRINE_V1 = "\n".join(
    (
        (
            "This contract governs senior and staff work on marketing websites, "
            "independent of harness."
        ),
        "",
        "Evidence boundary:",
        (
            "- Separate business facts, reasonable non-factual framing, design "
            "references, and unknowns."
        ),
        "- Every material business claim needs a source handle; unsupported claims remain blocked.",
        (
            "- Never invent or embellish licenses, insurance, reviews, customers, "
            "awards, years, prices, response times, guarantees, service coverage, "
            "outcomes, or statistics."
        ),
        (
            "- Competitor pages may inform hierarchy and interaction patterns, but "
            "their identity, copy, claims, images, tracking code, and distinctive "
            "phrases must not be copied."
        ),
        "",
        "Landing-page obligations:",
        (
            "- Define one visitor, one intent, one primary next action, likely "
            "objections, and the evidence available to answer them."
        ),
        (
            "- Make the hero describe the service, audience or location, concrete "
            "benefit, and primary CTA before adding a hook."
        ),
        "- Order sections to increase desire and confidence while reducing effort and confusion.",
        (
            "- Lead with customer benefits, then explain the process or feature that "
            "makes each benefit credible."
        ),
        "- Repeat the primary CTA at natural decision points with action-specific labels.",
        "- Omit unavailable proof instead of rendering realistic placeholders.",
        "",
        "Copy obligations:",
        (
            "- Research before prose. Use business pages, services, FAQs, social "
            "profiles, supplied notes, and verified reviews."
        ),
        (
            "- Prefer copy that is visual, falsifiable, and distinctive to the "
            "business over generic superlatives."
        ),
        (
            "- Use familiar words, active voice, short paragraphs, and headings that "
            "tell a coherent story when skimmed."
        ),
        "- If evidence is insufficient, return unresolved questions rather than plausible filler.",
        "",
        "Implementation and review obligations:",
        (
            "- Preserve conventional navigation, bounded logos, readable contrast, "
            "accessible controls, legal links, and contact routes."
        ),
        (
            "- Inspect the rendered page at explicit desktop and mobile viewports. "
            "HTML assertions alone do not establish visual correctness."
        ),
        (
            "- Check overflow, clipping, tap targets, hierarchy, CTA visibility, "
            "image relevance, console errors, and failed network requests."
        ),
        (
            "- Keep source evidence, generated copy, selected archetype, browser "
            "captures, and approval state as separate durable artifacts."
        ),
    )
)


CURRENT_MARKETING_SITE_DOCTRINE = MarketingSiteDoctrine(
    schema_version="marketing_site_doctrine.v1",
    text=MARKETING_SITE_DOCTRINE_V1,
)


__all__ = [
    "CURRENT_MARKETING_SITE_DOCTRINE",
    "MARKETING_SITE_DOCTRINE_V1",
    "MarketingSiteDoctrine",
]

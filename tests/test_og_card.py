# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The social card is a claim about the repo, so it is pinned like one.

`landing_page_website/public/og.png` is the first thing a person sees when the link is
shared, and it renders a command a reader may retype. It was hand-made once and
drifted: it showed `git clone github.com/rahul-nath/aidashos`, with no scheme
and no `.git` suffix, which fails when run. Nothing caught that, because an
image is opaque to every text check in this suite.

`landing_page_website/scripts/og-card.mjs` now generates the card from
`docs/onboarding/prompts.json`, and writes an SVG next to the PNG. The SVG is
text, so these tests can read it: the card must quote the clone command
verbatim, the committed SVG must be what the generator produces today, and the
PNG must be a real render of it at the declared Open Graph size.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBSITE = REPO_ROOT / "landing_page_website"
PROMPTS = REPO_ROOT / "docs" / "onboarding" / "prompts.json"
GENERATOR = WEBSITE / "scripts" / "og-card.mjs"
CARD_SVG = WEBSITE / "public" / "og.svg"
CARD_PNG = WEBSITE / "public" / "og.png"

# The values declared to crawlers in landing_page_website/index.html.
OG_WIDTH = 1200
OG_HEIGHT = 630


@pytest.fixture(scope="module")
def clone_command() -> str:
    return json.loads(PROMPTS.read_text(encoding="utf-8"))["clone_command"]


@pytest.fixture(scope="module")
def card_svg() -> str:
    return CARD_SVG.read_text(encoding="utf-8")


def test_card_quotes_the_clone_command_verbatim(clone_command: str, card_svg: str) -> None:
    assert escape(clone_command) in card_svg, (
        "landing_page_website/public/og.svg no longer renders docs/onboarding/prompts.json's "
        "clone_command. Run `node scripts/og-card.mjs` from landing_page_website/ instead of "
        "editing the card by hand."
    )


def test_card_shows_a_runnable_clone_command(clone_command: str) -> None:
    """The original defect, stated directly rather than by comparison.

    A reader retypes what the card shows. `git clone github.com/...` without a
    scheme is not a working command, whatever the JSON happens to say.
    """
    assert clone_command.startswith("git clone https://"), (
        f"the clone command must carry a scheme a reader can retype: {clone_command!r}"
    )


def test_committed_svg_is_what_the_generator_produces(card_svg: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; cannot re-run the card generator")

    regenerated = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "import { renderCard, cloneCommandFromRepo } from "
            f"{str(GENERATOR)!r};"
            "process.stdout.write(renderCard(await cloneCommandFromRepo()));",
        ],
        capture_output=True,
        text=True,
        cwd=WEBSITE,
        check=True,
    ).stdout

    assert regenerated == card_svg, (
        "landing_page_website/public/og.svg is not what "
        "landing_page_website/scripts/og-card.mjs produces "
        "today. Re-run `node scripts/og-card.mjs` from landing_page_website/ and commit both "
        "og.svg and og.png."
    )


def test_png_is_a_render_of_the_card_at_the_declared_size() -> None:
    header = CARD_PNG.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "landing_page_website/public/og.png is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    assert (width, height) == (OG_WIDTH, OG_HEIGHT), (
        f"og.png is {width}x{height}; landing_page_website/index.html declares "
        f"{OG_WIDTH}x{OG_HEIGHT} to crawlers"
    )


def test_card_tokens_match_the_stylesheet() -> None:
    """The card cannot read CSS, so it copies the palette. Pin the copy."""
    stylesheet = (WEBSITE / "src" / "styles.css").read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")

    declared = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});", stylesheet))
    for css_name, js_name in (
        ("bg", "BG"),
        ("bg-inset", "BG_INSET"),
        ("border", "BORDER"),
        ("text", "TEXT"),
        ("muted", "MUTED"),
        ("accent", "ACCENT"),
        ("green", "GREEN"),
    ):
        expected = declared[css_name]
        match = re.search(rf'^const {js_name} = "(#[0-9a-fA-F]{{6}})";', generator, re.M)
        assert match, f"landing_page_website/scripts/og-card.mjs no longer defines {js_name}"
        assert match.group(1).lower() == expected.lower(), (
            f"og-card.mjs {js_name} is {match.group(1)} but styles.css --{css_name} "
            f"is {expected}; the card and the page would not match"
        )

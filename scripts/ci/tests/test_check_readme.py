# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

import pytest

from scripts.ci.check_readme import check_document


def _write(root: Path, name: str, text: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def test_markdown_html_media_and_anchors_resolve(tmp_path: Path) -> None:
    _write(tmp_path, "docs/guide.md", "# Hello, `world`!\n# Hello, `world`!\n")
    _write(tmp_path, "media/preview (small).gif", "GIF89a")
    _write(
        tmp_path,
        "README.md",
        "## Get started\n"
        '<a href="#get-started">Start</a>\n'
        "[Guide](docs/guide.md#hello-world-1)\n"
        '![Demo](<media/preview (small).gif> "Small preview")\n'
        '<img src="media/preview%20(small).gif" />\n'
        "[More][guide]\n[guide]: docs/guide.md#hello-world\n"
        "[Online](https://example.invalid/not-contacted)\n",
    )
    assert check_document(tmp_path, Path("README.md")) == []


@pytest.mark.parametrize(
    ("markup", "reason"),
    [
        ("[Gone](absent.md)", "local target missing"),
        ('<img src="missing.gif" />', "local target missing"),
        ("[Start](#no-such-heading)", "anchor missing"),
        ("[Guide](guide.md#removed-heading)", "anchor missing"),
        ("[Guide][undefined]", "undefined link reference"),
        ("[Escape](../outside.md)", "local link escapes checkout"),
        ("[Escape](%2e%2e/outside.md)", "local link escapes checkout"),
    ],
)
def test_invalid_local_links_fail_closed(tmp_path: Path, markup: str, reason: str) -> None:
    _write(tmp_path, "README.md", markup)
    _write(tmp_path, "guide.md", "# Current heading\n")
    issues = check_document(tmp_path, Path("README.md"))
    assert len(issues) == 1
    assert issues[0].reason == reason


def test_symlink_outside_checkout_is_not_a_valid_asset(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _write(root, "README.md", "![asset](escape.gif)")
    _write(tmp_path, "outside.gif", "GIF89a")
    (root / "escape.gif").symlink_to(tmp_path / "outside.gif")
    assert check_document(root, Path("README.md"))[0].reason == "local link escapes checkout"


def test_fenced_and_inline_examples_are_not_links(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "```md\n[example](missing.md)\n```\n"
        "~~~html\n<img src='missing.gif'>\n~~~\n"
        "`[example](missing.md)`\n``<a href='missing.md'>``\n",
    )
    assert check_document(tmp_path, Path("README.md")) == []


def test_setext_unicode_and_explicit_anchors(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "Café & tools\n============\n"
        '<a id="explicit-anchor"></a>\n'
        "[Title](#caf%C3%A9--tools) [Explicit](#explicit-anchor)\n",
    )
    assert check_document(tmp_path, Path("README.md")) == []


def test_media_notes_can_link_back_within_checkout(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "## Demo\n")
    _write(tmp_path, "docs/media/readme/README.md", "[Demo](../../../README.md#demo)")
    assert check_document(tmp_path, Path("docs/media/readme/README.md")) == []


def test_missing_entrypoint_is_failure(tmp_path: Path) -> None:
    assert check_document(tmp_path, Path("README.md"))[0].reason == (
        "document missing or outside checkout"
    )

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A link in a published document has to resolve where it was published.

The private tree and the published tree are different trees. `public_import.toml`
carries an allowlist, so most of `docs/` stays private while `src/`, `tests/`,
and `skills/` travel whole, and a relative link from a traveling file into a
document that stayed behind resolves perfectly for the person who wrote it and
dangles for everyone who reads it.

That is not hypothetical. `docs/design_tradeoffs.md` was published on 2026-08-17
with two broken links on its first screen, both of them found by hand after the
push, and the handoff for that session recorded "no guard exists for broken
relative links in documents that travel" as its clearest remaining gap.

This is that guard, and it can only run where the manifest is: the snapshot.
In the private checkout there is no `public_import.toml` - the manifest lives in
the snapshot repository, which is what pulls from here - so there is no
allowlist to check against and every link resolves by construction. The
parametrized test below collects nothing there and skips.

That is the same bargain `test_operator_command_surface.py` makes and it is
sound, because the snapshot suite is a step in the publishing sequence rather
than an afterthought:

    cd <snapshot> && uv run python scripts/import_public_snapshot.py --apply
    cd <snapshot> && uv run pytest -q

A broken link is caught between applying and pushing, which is the last moment
it is still free to fix.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _REPOSITORY_ROOT / "public_import.toml"

# Fenced blocks are stripped before links are read. A markdown link inside a code
# fence is an illustration of a link, not one, and asking the filesystem about it
# would fail on documents that are correct.
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_REFERENCE_LINK = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)


def _traveling_markdown() -> list[Path]:
    """Every markdown file the manifest publishes, file entries and directories.

    Not only `docs/`. `README.md` is the first thing a stranger opens and
    `skills/` is what an agent reads before it does anything, so a dangling link
    in either costs more than one in a design document.
    """

    if not _MANIFEST.is_file():
        return []
    payload = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    found: list[Path] = []
    for entry in payload.get("allow", []):
        raw = entry.get("path")
        if not isinstance(raw, str):
            continue
        target = _REPOSITORY_ROOT / raw
        if target.is_dir():
            found.extend(sorted(target.rglob("*.md")))
        elif target.is_file() and target.suffix == ".md":
            found.append(target)
    return sorted(set(found))


def _is_relative_path_link(target: str) -> bool:
    """Whether this link is a path into the tree rather than somewhere else.

    A bare `#anchor` addresses the same document and a scheme addresses the
    network. Neither is a claim about a file, so neither is this test's business;
    reachability of an external URL is a different check with a different failure
    mode, and one that would make an offline suite red.
    """

    if target.startswith(("http://", "https://", "mailto:", "#", "<")):
        return False
    return "://" not in target


def _relative_links() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for document in _traveling_markdown():
        text = _FENCE.sub("", document.read_text(encoding="utf-8"))
        for raw in _INLINE_LINK.findall(text) + _REFERENCE_LINK.findall(text):
            if not _is_relative_path_link(raw):
                continue
            # `path.md#section` addresses a file and a place in it. The file is
            # the part that can be missing from a snapshot.
            if not raw.split("#", 1)[0]:
                continue
            found.append((str(document.relative_to(_REPOSITORY_ROOT)), raw))
    return found


@pytest.mark.parametrize(("document", "link"), _relative_links())
def test_every_relative_link_in_a_published_document_resolves(document: str, link: str) -> None:
    """Resolved against the containing file, which is how a reader follows it."""

    source = _REPOSITORY_ROOT / document
    target = (source.parent / link.split("#", 1)[0]).resolve()

    assert target.exists(), (
        f"{document} links to `{link}`, which does not exist in this tree.\n"
        "If this is the public snapshot, the target is a file that did not "
        "travel: either add it to public_import.toml, or stop linking to it "
        "from a document that does travel."
    )


def test_the_link_check_has_something_to_check_when_the_manifest_is_present() -> None:
    """A regex that silently matched nothing would pass this file forever.

    The parametrized test above is skipped by an empty collection, and an empty
    collection is the correct state in the private checkout and a defect in the
    snapshot. Only the manifest can tell those apart, so it decides here too.
    """

    if not _MANIFEST.is_file():
        pytest.skip("no public_import.toml: this checkout is not the published snapshot")

    documents = _traveling_markdown()
    assert documents, "the manifest publishes no markdown at all, which cannot be right"
    assert _relative_links(), (
        f"{len(documents)} published markdown files and not one relative link between "
        "them. The link pattern probably stopped matching."
    )

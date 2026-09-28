#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check public README links and media without contacting external websites.

The importer intentionally does not own the public README or its media notes.
These entrypoints therefore need a separate check from traveling-document tests.
Local URLs must remain inside the checkout; Markdown targets also prove anchors.
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

_DEFAULT_DOCUMENTS = (Path("README.md"), Path("docs/media/readme/README.md"))
# README prose uses inline/full-reference links and ATX/setext headings.
# Fenced examples and inline code are not navigable document links.
_FENCE_START = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+).*?\1", re.DOTALL)
_DESTINATION = r"(<[^>\n]+>|(?:\\.|[^()\s]|\([^()\n]*\))+)"
_INLINE_LINK = re.compile(
    r"(?<!\\)!?\[([^\]\n]*)\]\(\s*" + _DESTINATION + r"(?:\s+['\"].*?['\"])?\s*\)"
)
_REFERENCE = re.compile(r"^ {0,3}\[([^]\n]+)\]:\s*" + _DESTINATION, re.MULTILINE)
_REFERENCE_LINK = re.compile(r"(?<!\\)!?\[([^]\n]+)\]\[([^]\n]*)\]")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)\s*$")


@dataclass(frozen=True)
class LinkIssue:
    document: Path
    target: str
    reason: str


class _HtmlDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.anchors: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value is None:
                continue
            if name in {"href", "src"}:
                self.links.append(value)
            if name == "id" or (tag == "a" and name == "name"):
                self.anchors.add(value)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _without_fences(text: str) -> str:
    marker: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        fence = _FENCE_START.match(line)
        if marker is None and fence:
            marker = fence.group(1)
        elif marker is not None:
            stripped = line.strip()
            if stripped and set(stripped) == {marker[0]} and len(stripped) >= len(marker):
                marker = None
        else:
            lines.append(line)
    return "\n".join(lines)


def _label(value: str) -> str:
    return " ".join(value.casefold().split())


def _destination(value: str) -> str:
    return re.sub(r"\\([\\`()\[\]<> ])", r"\1", value.removeprefix("<").removesuffix(">"))


def _anchors(text: str) -> set[str]:
    prose = _without_fences(text)
    parser = _HtmlDocument()
    parser.feed(prose)
    anchors = parser.anchors.copy()
    headings: list[str] = []
    lines = prose.splitlines()
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading:
            headings.append(heading.group(1))
        elif index and _SETEXT.match(line) and lines[index - 1].strip():
            headings.append(lines[index - 1].strip())
    generated: set[str] = set()
    for heading in headings:
        heading = _INLINE_LINK.sub(r"\1", heading)
        parser = _HtmlDocument()
        parser.feed(heading.replace("`", "").replace("*", ""))
        plain = html.unescape("".join(parser.text)).lower()
        slug = "".join(
            char
            for char in plain
            if char in "-_ " or unicodedata.category(char)[0] not in {"P", "S", "C"}
        ).replace(" ", "-")
        candidate = slug
        suffix = 0
        while candidate in generated:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        generated.add(candidate)
    return anchors | generated


def check_document(root: Path, document: Path) -> list[LinkIssue]:
    root = root.resolve()
    source = (root / document).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return [LinkIssue(document, str(document), "document missing or outside checkout")]
    prose = _INLINE_CODE.sub("", _without_fences(source.read_text(encoding="utf-8")))
    parser = _HtmlDocument()
    parser.feed(prose)
    links = parser.links + [_destination(match[1]) for match in _INLINE_LINK.findall(prose)]
    references = {_label(label): _destination(url) for label, url in _REFERENCE.findall(prose)}
    links.extend(references.values())
    issues: list[LinkIssue] = []
    for title, label in _REFERENCE_LINK.findall(prose):
        if _label(label or title) not in references:
            issues.append(LinkIssue(document, f"[{label or title}]", "undefined link reference"))
    for raw in links:
        parsed = urlsplit(html.unescape(raw))
        if parsed.scheme or parsed.netloc:
            continue
        local_path = unquote(parsed.path)
        target = (source.parent / local_path).resolve() if local_path else source
        if not target.is_relative_to(root):
            reason = "local link escapes checkout"
        elif not target.exists():
            reason = "local target missing"
        elif parsed.fragment and target.suffix.lower() in {".md", ".markdown", ".html"}:
            if unquote(parsed.fragment) in _anchors(target.read_text(encoding="utf-8")):
                continue
            reason = "anchor missing"
        else:
            continue
        issues.append(LinkIssue(document, raw, reason))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("documents", type=Path, nargs="*", default=_DEFAULT_DOCUMENTS)
    arguments = parser.parse_args()
    issues = [
        issue
        for document in arguments.documents
        for issue in check_document(arguments.root, document)
    ]
    for issue in issues:
        print(f"{issue.document}: {issue.target}: {issue.reason}")
    if not issues:
        print(f"README links and local media passed ({len(arguments.documents)} documents)")
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())

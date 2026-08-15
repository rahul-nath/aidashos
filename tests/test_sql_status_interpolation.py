# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard the one silent failure mode of interpolating enums into SQL.

Statuses live in contracts.py and are rendered into query text, which keeps the
enum the single source of truth for values that would otherwise be copied into
every query. The cost is a new way to be wrong: drop the ``f`` prefix and the
query carries the literal characters ``{SagaStatus.ACTIVE}`` instead of
``ACTIVE``. Nothing raises. The query simply matches nothing, and a sweep or a
guard quietly stops doing its job.

Two real instances of exactly this survived a hand review during the change that
introduced them, which is why it is checked here rather than left to eyes.

The check keys on the string looking like SQL, not on a list of known status
names. An earlier version listed the names it knew about and missed both cases.
Route patterns and deliberate format templates elsewhere in the codebase use the
same braces legitimately, so narrowing by SQL keyword is what separates a
forgotten prefix from an intended placeholder.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "local_first_agent_os"

_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\}")
_LOOKS_LIKE_SQL = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WHERE|VALUES|SET)\b",
    re.IGNORECASE,
)


def _sql_strings_with_unrendered_placeholders() -> list[str]:
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # An f-string parses as JoinedStr, so a plain Constant holding a
            # placeholder is a prefix someone forgot.
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not _PLACEHOLDER.search(node.value):
                continue
            if not _LOOKS_LIKE_SQL.search(node.value):
                continue
            offenders.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}")
    return offenders


def test_no_sql_string_carries_an_unrendered_placeholder() -> None:
    offenders = _sql_strings_with_unrendered_placeholders()

    assert offenders == [], (
        "these SQL strings contain a placeholder but are not f-strings, so the "
        f"query would carry braces where a status belongs: {offenders}"
    )

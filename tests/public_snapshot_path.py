# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Make the repository root importable, for the public snapshot's own tests.

`tests/test_import_public_snapshot.py` imports `scripts.import_public_snapshot`,
which resolves only when the repository root is on `sys.path`. The obvious home
for that is `pythonpath` in `pyproject.toml`, but this snapshot's
`pyproject.toml` is overwritten wholesale on every import from the private
checkout, so a public-only entry there would not survive the next sync.

This module is deliberately absent from the import allowlist, which makes it one
of the few places the public snapshot can state a requirement of its own and
still be here afterward.

It used to be a `conftest.py` at the repository root, which worked but named the
requirement badly. A second module called `conftest` outranks `tests/conftest.py`
in pyright's search order, so `tests/postgres_support.py` could not resolve the
suite fixtures it imports by that name, and the public snapshot carried a type
error the private checkout did not have. A file whose only job is to extend
`sys.path` should say so in its name rather than borrow pytest's.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

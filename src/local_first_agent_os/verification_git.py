# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Immutable Git interpretation shared by source selection and gate snapshots."""

from __future__ import annotations

import os


def verification_git_environment() -> dict[str, str]:
    """Read real objects without caller redirects, replacement refs, or global config."""
    return {
        **{key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }

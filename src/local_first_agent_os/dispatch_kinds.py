# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The one finite vocabulary shared by every dispatch boundary."""

from __future__ import annotations

from enum import StrEnum


class DispatchKind(StrEnum):
    ADVISORY = "advisory"
    CODE = "code"
    CAST = "cast"


__all__ = ["DispatchKind"]

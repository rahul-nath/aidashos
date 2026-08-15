# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stdio hygiene for resident daemon processes.

Only this module knows why a resident daemon must not keep the stdin it
inherited from the launching terminal: macOS revokes the tty descriptor when
that terminal exits, and every child process that later inherits the revoked
descriptor dies at interpreter startup (init_sys_streams, EBADF) before it can
produce any output.
"""

from __future__ import annotations

import os


def detach_inherited_stdin() -> None:
    """Rebind fd 0 to ``os.devnull`` so children never inherit a dead handle."""
    devnull = os.open(os.devnull, os.O_RDWR)
    if devnull == 0:
        return
    try:
        os.dup2(devnull, 0)
    finally:
        os.close(devnull)


__all__ = ["detach_inherited_stdin"]

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable compatibility shell for the packaged coordination ledger."""

from local_first_agent_os.coordination.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

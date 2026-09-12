# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicit installer setup for the dedicated local verifier database."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .project_center import load_project_center
from .verification_resources import (
    LOCAL_DATABASE,
    LOCAL_PORT,
    VerificationSetupOperation,
    _read_protected,
    check_configured_verification_resources,
    initialize_local_verification_resources,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=(*VerificationSetupOperation, "check"))
    parser.add_argument("--target-project-id")
    parser.add_argument("--owner-url-file", type=Path)
    args = parser.parse_args(argv)
    try:
        center = load_project_center()
        project = center.project_by_id(args.target_project_id or center.control_plane_project)
        if args.operation == "check":
            check_configured_verification_resources(project.id, project.expanded_path)
            print(f"Verification resource ready for {project.id}")
        else:
            # These are the public test-service defaults, never a production fallback.
            # The startup guard prevents a contained client from using this known owner login.
            secret = (
                _read_protected(args.owner_url_file).decode()
                if args.owner_url_file is not None
                else f"postgresql://postgres:postgres@127.0.0.1:{LOCAL_PORT}/{LOCAL_DATABASE}"
            )
            disposition = initialize_local_verification_resources(
                project.id,
                project.expanded_path,
                secret,
                operation=VerificationSetupOperation(args.operation),
            )
            print(f"Local verification resource {disposition.value} for {project.id}")
    except Exception as exc:
        # A connection error can contain a DSN. Report its type and the explicit remedy only.
        parser.exit(
            1,
            f"Local verification setup refused ({type(exc).__name__}); "
            "check the dedicated postgres-test service and protected binding.\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

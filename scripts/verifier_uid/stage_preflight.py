#!/usr/bin/env python3
"""Exercise operator staging and command compatibility before helper authentication.

This sidecar does not install, import, or invoke the privileged helper and cannot
write a qualification receipt. The frozen qualifier's call and environment are
kept unchanged; a parity test prevents this preflight from silently drifting.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import subprocess
import tempfile
import traceback
from pathlib import Path

from toolchain_compatibility import run_compatibility, validate_staged, write_output


def stage(project, python, destination, source, account):
    environment = {
        "HOME": account.pw_dir,
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "PYTHONPATH": str(project / "src"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    return subprocess.run(
        [
            str(python),
            "-m",
            "local_first_agent_os.verification_toolchain_staging",
            "--project",
            str(project),
            "--destination",
            str(destination),
            "--source",
            str(source),
        ],
        env=environment,
        cwd=project,
        capture_output=True,
        text=True,
        timeout=150,
    )


def preflight(project, python, log_path):
    if os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise PermissionError("staging preflight requires the unprivileged operator")
    if not all(path.is_absolute() for path in (project, python, log_path)):
        raise ValueError("project, operator Python, and log paths must be absolute")
    parent = log_path.parent.stat()
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise PermissionError("preflight log parent must be operator-owned and protected")
    account = pwd.getpwuid(os.getuid())
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    exit_code = 1
    with os.fdopen(descriptor, "wb") as log:
        try:
            with tempfile.TemporaryDirectory(prefix="aidashos-stage-preflight-") as temporary:
                anchor = Path(temporary).resolve()
                destination, source = anchor / "toolchain", anchor / "source"
                destination.mkdir(mode=0o700)
                source.mkdir(mode=0o700)
                log.write(
                    (
                        "Operator staging and compatibility; workspace: " + str(anchor) + "\n"
                    ).encode()
                )
                log.flush()
                try:
                    result = stage(project, python, destination, source, account)
                except subprocess.TimeoutExpired as failure:
                    write_output(log, failure.stdout, failure.stderr, "staging")
                    raise
                write_output(log, result.stdout, result.stderr, "staging")
                if result.returncode:
                    raise RuntimeError("staging subprocess exited " + str(result.returncode))
                staged = json.loads(result.stdout)
                validate_staged(staged, destination)
                log.write(b"=== staging outcome ===\nstaging passed\n=== compatibility phase ===\n")
                run_compatibility(staged, source, anchor, log)
                log.write(b"=== compatibility outcome ===\ncompatibility passed\n")
            exit_code = 0
        except Exception:
            log.write(b"=== preflight failure ===\n" + traceback.format_exc().encode())
        finally:
            log.write(
                b"=== outcome ===\n"
                + (b"operator preflight passed" if exit_code == 0 else b"operator preflight failed")
                + b"; native qualification not run\n"
            )
            log.flush()
            os.fsync(log.fileno())
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--operator-python", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    try:
        exit_code = preflight(args.project, args.operator_python, args.log)
    except Exception as failure:
        print(
            json.dumps(
                {
                    "scope": "operator_staging_and_compatibility",
                    "status": "refused",
                    "error_type": type(failure).__name__,
                    "error": str(failure),
                    "native_qualification": "not_run",
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "scope": "operator_staging_and_compatibility",
                "status": "passed" if exit_code == 0 else "failed",
                "log": str(args.log),
                "native_qualification": "not_run",
            }
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

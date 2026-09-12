"""Ordinary-user command compatibility for an already staged toolchain.

No helper is imported, staging is never repeated, and results cannot activate
native qualification. The first four probes track the frozen qualifier exactly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ProbeKind(Enum):
    UV = "uv"
    PYTHON = "python"
    NODE = "node"
    UV_PYTHON = "uv-python"
    GIT = "git"
    PYTHON_IDENTITY = "python-identity"


@dataclass(frozen=True)
class CommandProbe:
    kind: ProbeKind
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CompatibilityResult:
    """A zero-exit command whose required observable result was also verified."""

    kind: ProbeKind
    stdout: str
    stderr: str


def environment(staged):
    return {
        "PATH": ":".join(str(Path(path).parent) for path in staged["executables"]),
        "VIRTUAL_ENV": staged["environment"],
        "UV_PROJECT_ENVIRONMENT": staged["environment"],
        "UV_PYTHON": staged["python"],
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }


def command_plan(staged):
    executables = {Path(path).name: path for path in staged["executables"]}
    if len(executables) != len(staged["executables"]):
        raise ValueError("staged executable names are ambiguous")
    python = staged["python"]
    return (
        CommandProbe(ProbeKind.UV, (executables["uv"], "--version")),
        CommandProbe(
            ProbeKind.PYTHON,
            (python, "-c", "import ssl,sqlite3; print('python ready')"),
        ),
        CommandProbe(
            ProbeKind.NODE,
            (
                executables["node"],
                "-e",
                "const r=require('node:child_process').spawnSync('/usr/bin/true'); "
                "if(r.error || r.status!==0) throw r.error || Error('spawn failed'); "
                "console.log('node spawn ready')",
            ),
        ),
        CommandProbe(
            ProbeKind.UV_PYTHON,
            (
                executables["uv"],
                "run",
                "--offline",
                "--no-project",
                "--python",
                python,
                "python",
                "-c",
                "print('uv spawned Python')",
            ),
        ),
        CommandProbe(ProbeKind.GIT, (executables["git"], "--version")),
        CommandProbe(
            ProbeKind.PYTHON_IDENTITY,
            (
                python,
                "-c",
                "import json,sys; print(json.dumps({'executable':sys.executable}))",
            ),
        ),
    )


def validate_staged(staged, anchor):
    root = anchor.resolve(strict=True)
    for path in (staged["environment"], staged["python"], *staged["executables"]):
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.resolve(strict=True).is_relative_to(root)
        ):
            raise ValueError("staged executable escaped its declared anchor")


def write_output(log, stdout, stderr, label):
    def encoded(value):
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")

    log.write(("=== " + label + " stdout ===\n").encode() + encoded(stdout))
    log.write(("\n=== " + label + " stderr ===\n").encode() + encoded(stderr) + b"\n")
    log.flush()


def validate_result(probe, result, python_target):
    if result.returncode:
        raise RuntimeError(
            probe.kind.value + " compatibility command exited " + str(result.returncode)
        )
    output = result.stdout.strip()
    markers = {
        ProbeKind.PYTHON: "python ready",
        ProbeKind.NODE: "node spawn ready",
        ProbeKind.UV_PYTHON: "uv spawned Python",
    }
    if probe.kind in markers:
        valid = output == markers[probe.kind]
    elif probe.kind in (ProbeKind.UV, ProbeKind.GIT):
        prefix = "uv" if probe.kind == ProbeKind.UV else "git version"
        valid = re.fullmatch(re.escape(prefix) + r" \d+\.\d+\.\d+[^\n]*", output) is not None
    elif probe.kind == ProbeKind.PYTHON_IDENTITY:
        observed = Path(json.loads(result.stdout)["executable"])
        if not observed.is_absolute() or observed.resolve(strict=True) != python_target:
            raise ValueError("Python compatibility probe reported a different interpreter")
        valid = True
    else:
        raise ValueError("unknown compatibility probe kind")
    if not valid:
        raise RuntimeError(
            probe.kind.value + " compatibility command omitted its required success output"
        )
    return CompatibilityResult(probe.kind, result.stdout, result.stderr)


def run_compatibility(staged, source, anchor, log):
    """Run only compatibility, preserving the caller's staged files and source.

    The caller supplies an existing owned workspace and open protected log.
    Only the fresh HOME/TMPDIR directory created here is removed on return.
    """
    if os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise PermissionError("toolchain compatibility requires the unprivileged operator")
    if not source.is_absolute() or not source.is_dir() or not anchor.is_absolute():
        raise ValueError("compatibility requires absolute existing source and workspace paths")
    if anchor.stat().st_uid != os.geteuid():
        raise PermissionError("compatibility workspace must belong to the operator")
    validate_staged(staged, anchor)
    python_target = Path(staged["python"]).resolve(strict=True)
    probes = command_plan(staged)
    results = []
    with tempfile.TemporaryDirectory(prefix="compatibility-", dir=anchor) as temporary:
        home, scratch = Path(temporary) / "home", Path(temporary) / "scratch"
        home.mkdir(mode=0o700)
        scratch.mkdir(mode=0o700)
        values = dict(environment(staged), HOME=str(home), TMPDIR=str(scratch))
        for probe in probes:
            log.write(("Starting compatibility command: " + probe.kind.value + "\n").encode())
            log.flush()
            try:
                result = subprocess.run(
                    list(probe.argv),
                    cwd=source,
                    env=values,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except subprocess.TimeoutExpired as failure:
                write_output(
                    log, failure.stdout, failure.stderr, "compatibility " + probe.kind.value
                )
                raise
            write_output(log, result.stdout, result.stderr, "compatibility " + probe.kind.value)
            results.append(validate_result(probe, result, python_target))
    return tuple(results)

# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Concrete resident-service specifications and operating-system adapters."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, assert_never
from urllib.parse import urlparse

import httpx

from .pi_daemon import ensure_pi_daemon
from .runtime_source import revision_of
from .service_reconciliation import (
    RequiredHealthField,
    ServiceContract,
    ServiceController,
    ServiceHealthy,
    ServiceName,
    ServiceObservation,
    ServiceProbe,
    ServiceReconciliationFailed,
    ServiceUnavailable,
    reconcile_service,
)
from .settings import Settings, get_settings

PI_DAEMON_LAUNCHD_LABEL = "com.rahul.local-first-agent.pi-daemon"


def _run(
    argv: Sequence[str],
    *,
    timeout: float = 5,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceReconciliationFailed(f"could not run {argv[0]}: {exc}") from exc


@dataclass(frozen=True)
class LaunchdSupervisor:
    domain: str
    label: str
    plist_path: Path

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    def loaded(self) -> bool:
        return _run(("launchctl", "print", self.target)).returncode == 0

    def pid(self) -> int | None:
        result = _run(("launchctl", "print", self.target))
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                try:
                    return int(stripped.removeprefix("pid = "))
                except ValueError:
                    return None
        return None

    def working_directory(self, fallback: Path) -> Path:
        try:
            with self.plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (FileNotFoundError, OSError, plistlib.InvalidFileException):
            return fallback
        value = payload.get("WorkingDirectory")
        return Path(value).expanduser() if isinstance(value, str) and value else fallback

    def kickstart(self) -> None:
        # `kickstart -k` blocks until the old process has exited, so this
        # timeout bounds a graceful shutdown, not a launch. A resident daemon
        # draining on SIGTERM (the pi-daemon flushing session memory) exceeded
        # 15 seconds on 2026-08-30 and the restart script reported failure
        # while launchd finished the restart fine. 90 seconds covers a slow
        # drain and still fails on a service that genuinely hangs.
        result = _run(("launchctl", "kickstart", "-k", self.target), timeout=90)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise ServiceReconciliationFailed(f"launchd could not start {self.label}: {detail}")


def _listener_pids(port: int) -> tuple[int, ...]:
    result = _run(
        ("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"),
    )
    if result.returncode not in (0, 1):
        return ()
    pids: list[int] = []
    for value in result.stdout.split():
        try:
            pids.append(int(value))
        except ValueError:
            continue
    return tuple(pids)


def _pid_descends_from(pid: int, ancestor: int) -> bool:
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        if current == ancestor:
            return True
        visited.add(current)
        result = _run(("ps", "-o", "ppid=", "-p", str(current)))
        try:
            current = int(result.stdout.strip())
        except ValueError:
            return False
    return False


class HttpHealthProbe(ServiceProbe):
    def __init__(
        self,
        url: str,
        *,
        ownership_probe: Callable[[], bool],
        timeout_seconds: float = 0.5,
    ) -> None:
        self._url = url.rstrip("/") + "/health"
        self._ownership_probe = ownership_probe
        self._timeout_seconds = timeout_seconds

    def observe(self) -> ServiceObservation:
        try:
            response = httpx.get(self._url, timeout=self._timeout_seconds)
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError):
            return ServiceUnavailable()
        if not isinstance(payload, Mapping):
            return ServiceUnavailable()
        return ServiceHealthy(
            health={str(key): value for key, value in payload.items()},
            supervisor_owned=self._ownership_probe(),
        )


class PiDaemonController(ServiceController):
    """Pi's lifecycle adapter for the generic reconciliation engine."""

    _COMMAND_MARKERS = ("pi-daemon", "run_pi_daemon")

    def __init__(
        self,
        *,
        settings: Settings,
        port: int,
        repo_root: Path,
        launchd: LaunchdSupervisor | None,
        probe: ServiceProbe,
    ) -> None:
        self._settings = settings
        self._port = port
        self._repo_root = repo_root
        self._launchd = launchd
        self._probe = probe

    def start(self) -> None:
        if self._launchd is not None:
            self._launchd.kickstart()
            return
        ensure_pi_daemon(self._settings, wait_seconds=40)

    def restart(self) -> None:
        if self._launchd is None or not self._is_launchd_owned():
            self._stop_conflicting_listeners()
            self._wait_until_stopped()
        self._remove_legacy_pid_files()
        self.start()

    def _is_launchd_owned(self) -> bool:
        if self._launchd is None or (launchd_pid := self._launchd.pid()) is None:
            return False
        return any(
            _pid_descends_from(listener_pid, launchd_pid)
            for listener_pid in _listener_pids(self._port)
        )

    def _stop_conflicting_listeners(self) -> None:
        for pid in _listener_pids(self._port):
            command = _run(("ps", "-o", "command=", "-p", str(pid))).stdout.strip()
            if not any(marker in command for marker in self._COMMAND_MARKERS):
                raise ServiceReconciliationFailed(
                    f"port {self._port} is owned by unexpected process {pid}: {command}"
                )
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise ServiceReconciliationFailed(
                    f"could not stop stale pi-daemon process {pid}: {exc}"
                ) from exc

    def _wait_until_stopped(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if isinstance(self._probe.observe(), ServiceUnavailable):
                return
            time.sleep(0.25)
        raise ServiceReconciliationFailed(f"pi-daemon on port {self._port} did not stop within 10s")

    def _remove_legacy_pid_files(self) -> None:
        for path in (
            self._repo_root / ".local_agent" / "run" / "pi-daemon.pid",
            Path.home() / ".local-agent" / "daemon" / "pi-daemon.pid",
        ):
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ConfiguredService:
    contract: ServiceContract
    probe: ServiceProbe
    controller: ServiceController
    checkout: Path


def configure_pi_daemon_service(
    *,
    repo_root: Path,
    launch_domain: str,
    launch_plist_dir: Path,
    settings: Settings | None = None,
) -> ConfiguredService:
    """Instantiate Pi as one value understood by the generic service engine."""

    resolved_settings = settings or get_settings()
    launchd_candidate = LaunchdSupervisor(
        domain=launch_domain,
        label=PI_DAEMON_LAUNCHD_LABEL,
        plist_path=launch_plist_dir / f"{PI_DAEMON_LAUNCHD_LABEL}.plist",
    )
    launchd = launchd_candidate if launchd_candidate.loaded() else None
    checkout = (
        launchd.working_directory(repo_root) if launchd is not None else repo_root
    ).resolve()
    revision = revision_of(checkout)
    required_health = [
        RequiredHealthField("service_name", ServiceName.PI_DAEMON.value),
        RequiredHealthField("coordination_backend", "postgres"),
        RequiredHealthField("runtime_checkout", str(checkout)),
    ]
    if revision is not None:
        required_health.append(RequiredHealthField("runtime_revision", revision))

    parsed_url = urlparse(resolved_settings.pi_daemon_base_url)
    port = parsed_url.port or resolved_settings.pi_daemon_port

    def launchd_owns_listener() -> bool:
        if launchd is None or (launchd_pid := launchd.pid()) is None:
            return False
        return any(
            _pid_descends_from(listener_pid, launchd_pid) for listener_pid in _listener_pids(port)
        )

    probe = HttpHealthProbe(
        resolved_settings.pi_daemon_base_url,
        ownership_probe=launchd_owns_listener if launchd is not None else lambda: True,
    )
    contract = ServiceContract(
        name=ServiceName.PI_DAEMON,
        required_health=tuple(required_health),
        require_supervisor_ownership=launchd is not None,
    )
    controller = PiDaemonController(
        settings=resolved_settings,
        port=port,
        repo_root=repo_root,
        launchd=launchd,
        probe=probe,
    )
    return ConfiguredService(contract, probe, controller, checkout)


def reconcile_runtime_service(
    service_name: ServiceName,
    *,
    repo_root: Path,
    launch_domain: str,
    launch_plist_dir: Path,
    settings: Settings | None = None,
) -> ServiceHealthy:
    match service_name:
        case ServiceName.PI_DAEMON:
            configured = configure_pi_daemon_service(
                repo_root=repo_root,
                launch_domain=launch_domain,
                launch_plist_dir=launch_plist_dir,
                settings=settings,
            )
        case _:
            assert_never(service_name)
    result = reconcile_service(
        configured.contract,
        configured.probe,
        configured.controller,
    )
    if configured.checkout != repo_root.resolve():
        print(
            f"pi-daemon is pinned to {configured.checkout}; `pi` runs that checkout, not this one."
        )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", type=ServiceName, choices=tuple(ServiceName))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--launch-domain", required=True)
    parser.add_argument("--launch-plist-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = reconcile_runtime_service(
        args.service,
        repo_root=args.repo_root.resolve(),
        launch_domain=args.launch_domain,
        launch_plist_dir=args.launch_plist_dir.expanduser(),
    )
    print(json.dumps(dict(result.health), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConfiguredService",
    "HttpHealthProbe",
    "LaunchdSupervisor",
    "PI_DAEMON_LAUNCHD_LABEL",
    "PiDaemonController",
    "configure_pi_daemon_service",
    "main",
    "reconcile_runtime_service",
]

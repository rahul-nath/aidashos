# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

DEFAULT_SOUND_PATH = Path(
    os.environ.get(
        "LOCAL_AGENT_TIMER_SOUND_PATH",
        (
            "/System/Library/Sounds/Glass.aiff"
            if sys.platform == "darwin"
            else "~/.local-agent/sounds/timer.wav"
        ),
    )
)
DEFAULT_HOLD_SECONDS = 60 * 60
_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TimerLaunch:
    duration_seconds: int
    ends_at_ms: int
    log_path: str
    pid: int
    port: int
    sound_path: str
    started_at_ms: int
    url: str


def is_timer_directive(text: str) -> bool:
    tokens = _split(text)
    return bool(tokens and tokens[0] in {"/timer", "timer"})


def run_timer_directive(text: str) -> dict[str, Any]:
    try:
        tokens = _split(text)
        if not tokens or tokens[0] not in {"/timer", "timer"}:
            raise ValueError("Timer directive must start with /timer.")
        if len(tokens) < 2:
            raise ValueError("Usage: pi /timer 50")
        duration_seconds = parse_duration_seconds(tokens[1])
        launch = launch_timer(duration_seconds=duration_seconds)
        return _launch_result(launch)
    except Exception as exc:
        return {
            "schema_version": "timer_launch.v1",
            "workflow_type": "timer",
            "status": "failed",
            "error": str(exc),
            "terminal_message": f"timer failed: {exc}",
        }


def parse_duration_seconds(raw: str) -> int:
    match = _DURATION_RE.match(raw)
    if match is None:
        raise ValueError(f"Timer duration must be a number of minutes, seconds, or hours: {raw}")
    value = float(match.group("value"))
    unit = (match.group("unit") or "minutes").lower()
    if value <= 0:
        raise ValueError("Timer duration must be greater than zero.")
    if unit.startswith("s"):
        seconds = value
    elif unit.startswith("h"):
        seconds = value * 60 * 60
    else:
        seconds = value * 60
    return max(1, int(round(seconds)))


def launch_timer(
    *,
    duration_seconds: int,
    sound_path: Path = DEFAULT_SOUND_PATH,
    hold_seconds: int = DEFAULT_HOLD_SECONDS,
    open_browser: bool = True,
    port: int | None = None,
) -> TimerLaunch:
    if duration_seconds <= 0:
        raise ValueError("Timer duration must be greater than zero.")
    sound_path = sound_path.expanduser()
    if not sound_path.exists():
        raise FileNotFoundError(f"Timer sound does not exist: {sound_path}")
    dist_dir = _web_dist_dir()
    if not (dist_dir / "index.html").exists():
        raise FileNotFoundError(
            f"Timer UI build is missing at {dist_dir}. Run `cd web && npm run build`."
        )

    port = port or _find_free_port()
    started_at_ms = int(time.time() * 1000)
    ends_at_ms = started_at_ms + (duration_seconds * 1000)
    url = _timer_url(
        port=port,
        duration_seconds=duration_seconds,
        started_at_ms=started_at_ms,
        ends_at_ms=ends_at_ms,
    )

    log_dir = _repo_root() / ".local_agent" / "logs" / "timers"
    run_dir = _repo_root() / ".local_agent" / "run" / "timers"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    launch_id = f"{int(time.time())}-{port}"
    log_path = log_dir / f"timer-{launch_id}.log"
    state_path = run_dir / f"timer-{launch_id}.json"

    cmd = [
        sys.executable,
        "-m",
        "local_first_agent_os.local_timer",
        "worker",
        "--port",
        str(port),
        "--duration-seconds",
        str(duration_seconds),
        "--ends-at-ms",
        str(ends_at_ms),
        "--sound-path",
        str(sound_path),
        "--dist-dir",
        str(dist_dir),
        "--hold-seconds",
        str(hold_seconds),
    ]
    env = os.environ.copy()
    src_path = str(_repo_root() / "src")
    env["PYTHONPATH"] = (
        src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    )
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=_repo_root(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    if not _wait_for_port(port, timeout_seconds=3.0):
        process.terminate()
        raise RuntimeError(f"Timer worker did not open localhost:{port}. Check {log_path}.")

    launch = TimerLaunch(
        duration_seconds=duration_seconds,
        ends_at_ms=ends_at_ms,
        log_path=str(log_path),
        pid=process.pid,
        port=port,
        sound_path=str(sound_path),
        started_at_ms=started_at_ms,
        url=url,
    )
    state_path.write_text(json.dumps(asdict(launch), indent=2), encoding="utf-8")
    if open_browser:
        _open_browser(url)
    return launch


def run_worker(
    *,
    port: int,
    duration_seconds: int,
    ends_at_ms: int,
    sound_path: Path,
    dist_dir: Path,
    hold_seconds: int,
) -> None:
    handler = partial(_TimerRequestHandler, directory=str(dist_dir))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    alarm_thread = threading.Thread(
        target=_alarm_then_shutdown,
        kwargs={
            "server": server,
            "duration_seconds": duration_seconds,
            "ends_at_ms": ends_at_ms,
            "sound_path": sound_path,
            "hold_seconds": hold_seconds,
        },
        daemon=True,
    )
    alarm_thread.start()
    print(
        json.dumps(
            {
                "event": "timer_worker_started",
                "port": port,
                "duration_seconds": duration_seconds,
                "ends_at_ms": ends_at_ms,
                "sound_path": str(sound_path),
            }
        ),
        flush=True,
    )
    server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local Pi timer worker.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--port", type=int, required=True)
    worker.add_argument("--duration-seconds", type=int, required=True)
    worker.add_argument("--ends-at-ms", type=int, required=True)
    worker.add_argument("--sound-path", type=Path, required=True)
    worker.add_argument("--dist-dir", type=Path, required=True)
    worker.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    args = parser.parse_args(argv)
    if args.command == "worker":
        run_worker(
            port=args.port,
            duration_seconds=args.duration_seconds,
            ends_at_ms=args.ends_at_ms,
            sound_path=args.sound_path,
            dist_dir=args.dist_dir,
            hold_seconds=args.hold_seconds,
        )


class _TimerRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        requested = parsed.path
        if requested == "/" or (requested and Path(requested).suffix == ""):
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _alarm_then_shutdown(
    *,
    server: ThreadingHTTPServer,
    duration_seconds: int,
    ends_at_ms: int,
    sound_path: Path,
    hold_seconds: int,
) -> None:
    seconds_until_alarm = max(0.0, (ends_at_ms / 1000) - time.time())
    time.sleep(seconds_until_alarm)
    _play_sound(sound_path)
    time.sleep(max(0, hold_seconds))
    server.shutdown()


def _play_sound(sound_path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["afplay", str(sound_path)], check=False, timeout=30)
        return
    for player in ("paplay", "aplay", "ffplay"):
        if _command_exists(player):
            cmd = [player, str(sound_path)]
            if player == "ffplay":
                cmd = [player, "-nodisp", "-autoexit", str(sound_path)]
            subprocess.run(cmd, check=False, timeout=30)
            return
    raise RuntimeError("No supported audio player was found for this platform.")


def _launch_result(launch: TimerLaunch) -> dict[str, Any]:
    ends_at = datetime.fromtimestamp(launch.ends_at_ms / 1000).astimezone()
    duration = _format_duration(launch.duration_seconds)
    return {
        "schema_version": "timer_launch.v1",
        "workflow_type": "timer",
        "status": "scheduled",
        "terminal_message": (
            f"timer scheduled for {duration}; alarm at {ends_at:%Y-%m-%d %I:%M:%S %p %Z}\n"
            f"UI: {launch.url}\n"
            f"sound: {launch.sound_path}"
        ),
        "timer": asdict(launch),
    }


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text.strip())
    except ValueError:
        return text.strip().split()


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} sec"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hr" if hours == 1 else f"{hours} hrs"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} min"
    return f"{seconds / 60:g} min"


def _timer_url(
    *,
    port: int,
    duration_seconds: int,
    started_at_ms: int,
    ends_at_ms: int,
) -> str:
    params = urlencode(
        {
            "view": "timer",
            "duration": str(duration_seconds),
            "minutes": f"{duration_seconds / 60:g}",
            "startedAt": str(started_at_ms),
            "endsAt": str(ends_at_ms),
        }
    )
    return f"http://127.0.0.1:{port}/?{params}"


def _open_browser(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(
            ["open", url],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    webbrowser.open(url)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _command_exists(name: str) -> bool:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    return any((Path(path) / name).exists() for path in paths if path)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _web_dist_dir() -> Path:
    return _repo_root() / "web" / "dist"


if __name__ == "__main__":
    main()

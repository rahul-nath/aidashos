"""Real owned-process exit regression; the fixture simulates UID and sandbox controls."""

from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class ParentAdmission(unittest.TestCase):
    def test_child_cannot_launch_after_prepared_parent_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("protocol_fixture.py")),
                    str(state),
                    "pause-before-child-admission",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            pending = bytearray()

            def send(message):
                process.stdin.write(json.dumps(message).encode() + b"\n")

            def receive(kind):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if b"\n" not in pending:
                        self.assertTrue(
                            select.select(
                                [process.stdout], [], [], max(0, deadline - time.monotonic())
                            )[0],
                            "protocol response deadline",
                        )
                        data = os.read(process.stdout.fileno(), 65536)
                        if not data:
                            return None
                        pending.extend(data)
                        continue
                    line, _, remainder = pending.partition(b"\n")
                    pending[:] = remainder
                    frame = json.loads(line)
                    if frame["kind"] == kind:
                        return frame
                self.fail("protocol frame deadline")

            def launch(prepared):
                send(
                    {
                        "kind": "launch",
                        "handle": prepared["handle"],
                        "argv": [
                            sys.executable,
                            "-c",
                            "import time; print('ready', flush=True); time.sleep(30)",
                        ],
                        "cwd": opened["source"],
                        "environment": {"HOME": prepared["home"], "TMPDIR": prepared["scratch"]},
                        "profile": "(version 1)(allow default)",
                        "source_binding": "a" * 64,
                    }
                )

            try:
                send(
                    {
                        "kind": "open",
                        "schema": "aidashos.uid-gate.v1",
                        "source_binding": "a" * 64,
                        "duration": 15,
                        "process_limit": 64,
                    }
                )
                opened = receive("opened")
                self.assertEqual(stat.S_IMODE(Path(opened["staging"]).stat().st_mode), 0o755)
                send({"kind": "prepare", "parent_handle": None, "scratch": None})
                parent = receive("prepared")
                launch(parent)
                running = receive("launched")
                receive("stdout")
                send(
                    {
                        "kind": "prepare",
                        "parent_handle": parent["handle"],
                        "scratch": str(Path(parent["scratch"]).relative_to(opened["staging"])),
                    }
                )
                child = receive("prepared")
                for _ in range(500):
                    if (state / "paused").exists():
                        break
                    time.sleep(0.005)
                else:
                    self.fail("helper did not enter selector barrier")
                # The paused direct owner cannot reap or recycle this child PID.
                os.kill(running["pid"], signal.SIGKILL)
                for _ in range(100):
                    status = subprocess.check_output(
                        ["/bin/ps", "-o", "stat=", "-p", str(running["pid"])], text=True
                    )
                    if status.strip().startswith("Z"):
                        break
                    time.sleep(0.01)
                else:
                    self.fail("parent exit was not observed before child launch")
                launch(child)
                (state / "released").write_text("release")
                self.assertIsNone(receive("launched"), "child launched after parent exited")
                self.assertNotEqual(process.wait(timeout=5), 0)
                self.assertIn(
                    b"parent launch exited before dependent admission", process.stderr.read()
                )
            finally:
                if process.poll() is None:
                    (state / "released").write_text("release")
                    process.stdin.close()
                process.wait(timeout=20)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()

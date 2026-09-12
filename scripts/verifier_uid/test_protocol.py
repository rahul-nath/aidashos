"""Real pipes/processes verify protocol semantics; identity enforcement is not mocked proof."""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class Protocol(unittest.TestCase):
    def test_child_cancellation_preserves_parent_and_binds_unique_occurrences(self):
        with tempfile.TemporaryDirectory() as temporary:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("protocol_fixture.py")), temporary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            assert process.stdin is not None and process.stdout is not None
            pending = bytearray()
            frames = []

            def send(payload):
                process.stdin.write(json.dumps(payload).encode() + b"\n")

            def receive(kind, handle=None):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if b"\n" not in pending:
                        self.assertTrue(select.select([process.stdout], [], [], 2)[0])
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            self.fail("helper fixture closed: " + process.stderr.read().decode())
                        pending.extend(chunk)
                        continue
                    line, _, remainder = pending.partition(b"\n")
                    pending[:] = remainder
                    frame = json.loads(line)
                    frames.append(frame)
                    if frame["kind"] == kind and (handle is None or frame.get("handle") == handle):
                        return frame
                self.fail("bounded protocol response did not arrive")

            try:
                send(
                    {
                        "kind": "open",
                        "schema": "aidashos.uid-gate.v1",
                        "source_binding": "a" * 64,
                        "duration": 30,
                        "process_limit": 64,
                    }
                )
                opened = receive("opened")
                send({"kind": "prepare", "parent_handle": None, "scratch": None})
                parent = receive("prepared")

                def start(prepared):
                    request = {
                        "kind": "launch",
                        "handle": prepared["handle"],
                        "argv": [
                            sys.executable,
                            "-c",
                            "import time;print('ready',flush=True);time.sleep(30)",
                        ],
                        "cwd": opened["source"],
                        "environment": {"HOME": prepared["home"], "TMPDIR": prepared["scratch"]},
                        "profile": "(version 1)(allow default)",
                        "source_binding": "a" * 64,
                    }
                    send(request)
                    launched = receive("launched", prepared["handle"])
                    payload = {
                        key: value
                        for key, value in request.items()
                        if key not in ("kind", "handle")
                    }
                    payload["environment"] = sorted(payload["environment"].items())
                    expected = hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    self.assertEqual(launched["requested_digest"], expected)
                    self.assertNotEqual(launched["digest"], expected)
                    return launched

                running_parent = start(parent)
                receive("stdout", parent["handle"])
                send(
                    {
                        "kind": "prepare",
                        "parent_handle": parent["handle"],
                        "scratch": str(Path(parent["scratch"]).relative_to(opened["staging"])),
                    }
                )
                child = receive("prepared")
                self.assertNotEqual(parent["uid"], child["uid"])
                self.assertEqual(parent["gid"], child["gid"])
                self.assertNotEqual(parent["handle"], child["handle"])
                start(child)
                receive("stdout", child["handle"])
                send({"kind": "cancel", "handle": child["handle"]})
                exited_child = receive("exit", child["handle"])
                self.assertEqual(exited_child["code"], -9)
                self.assertEqual(exited_child["receipt"]["uid"], child["uid"])
                os.kill(running_parent["pid"], 0)
                self.assertLess(
                    next(
                        i
                        for i, item in enumerate(frames)
                        if item["kind"] == "stdout" and item["handle"] == child["handle"]
                    ),
                    next(
                        i
                        for i, item in enumerate(frames)
                        if item["kind"] == "exit" and item["handle"] == child["handle"]
                    ),
                )
                send({"kind": "terminate", "handle": parent["handle"]})
                exited_parent = receive("exit", parent["handle"])
                self.assertEqual(exited_parent["code"], -15)
                send({"kind": "close"})
                closed = receive("gate_closed")
                self.assertEqual(len(closed["launch_receipts"]), 2)
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()

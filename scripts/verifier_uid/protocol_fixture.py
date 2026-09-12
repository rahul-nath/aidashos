"""Unprivileged transport fixture; identity/sandbox ownership is explicitly simulated."""

import contextlib
import importlib.util
import os
import selectors
import signal
import sys
import time
from pathlib import Path

source = Path(__file__).with_name("uid_gate.py")
spec = importlib.util.spec_from_file_location("uid_gate", source)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
state = Path(sys.argv[1]).resolve()
(state / "work").mkdir()
processes = {}
original_fork = module.fork_launch


def fork(*args, **kwargs):
    result = original_fork(*args, **kwargs)
    processes[args[1]] = result[0]
    return result


class Membership:
    @staticmethod
    def empty(uid):
        pid = processes.get(uid)
        if pid is None:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False


def signal_fixture(uid, number=signal.SIGKILL):
    # Each PID is a direct child created by this fixture and not yet reaped.
    # This tests protocol routing only, never native UID cleanup correctness.
    pid = processes.get(uid)
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, number)


module.drop_identity = lambda *_: None
module.apply_sandbox = lambda *_: None
module.os.fchown = lambda *_: None
module.fork_launch = fork
module.sweep_uid = signal_fixture
if sys.argv[2:] == ["pause-before-child-admission"]:
    prepared_count = 0
    original_emit = module.emit

    def emit(message):
        global prepared_count
        if message["kind"] == "prepared":
            prepared_count += 1
        original_emit(message)

    class ControlFirst(selectors.DefaultSelector):
        """Hold a live owner before a legal control-first readiness ordering."""

        def select(self, *args, **kwargs):
            if prepared_count == 2 and not (state / "released").exists():
                (state / "paused").write_text("parent remains owned before selector readiness")
                deadline = time.monotonic() + 5
                while not (state / "released").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("protocol barrier deadline")
                    time.sleep(0.005)
            return sorted(
                super().select(*args, **kwargs), key=lambda pair: pair[0].data != "control"
            )

    module.emit = emit
    module.selectors.DefaultSelector = ControlFirst
os.umask(0o077)
os.set_blocking(1, False)
module.serve(state, Membership(), module.GateSpec.parse(module.read_declaration()), os.getuid())

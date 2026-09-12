# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real native processes prove both delegated progress and inherited denials."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic

import pytest

from local_first_agent_os.host_verification import _installed_toolchain, _sandbox_policy
from local_first_agent_os.native_verification_broker import (
    BROKER_ENV,
    NativeVerificationBroker,
    authenticated_contained_client,
)
from local_first_agent_os.seatbelt_policy import TcpGrant

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS Seatbelt boundary")

_IMPORTS = """import json,os,subprocess,sys,time
from pathlib import Path
from local_first_agent_os.process_containment import contained_frontier_process
from local_first_agent_os.native_verification_broker import BROKER_ENV
from local_first_agent_os.spawn_authority import ReadOnlyInspection,UnattendedImplementation
from local_first_agent_os.staffing import FrontierHarness
"""


@pytest.fixture(autouse=True)
def trusted_host_provisioning_authority():
    if authenticated_contained_client():
        pytest.skip(
            "requires trusted host broker provisioning; current UID is an authenticated gate client"
        )


@dataclass
class _Fixture:
    broker: NativeVerificationBroker
    snapshot: Path
    outputs: Path
    secret: Path

    def run(self, script: str):
        return self.broker.run((sys.executable, "-c", _IMPORTS + script), self.snapshot)


@contextmanager
def _gate(
    tmp_path: Path, *, seconds: float = 20, valid=lambda: True, port: int | None = None
) -> Iterator[_Fixture]:
    snapshot, outputs = tmp_path / "source", tmp_path / "outputs"
    snapshot.mkdir()
    outputs.mkdir()
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "src",
        snapshot / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (snapshot / "immutable.txt").write_text("sealed source")
    secret = tmp_path / "host-secret.txt"
    secret.write_text("must remain outside the gate")
    toolchain = _installed_toolchain(Path(__file__).resolve().parents[1])
    policy = _sandbox_policy(snapshot, outputs, toolchain.readable, (secret,))
    if port is not None:
        policy = replace(policy, outbound=((TcpGrant("localhost", port),),))
    environment = toolchain.gate_environment(snapshot, outputs)
    with NativeVerificationBroker(
        policy=policy,
        snapshot=snapshot,
        outputs=outputs,
        environment=environment,
        deadline=monotonic() + seconds,
        authority_valid=valid,
    ) as broker:
        yield _Fixture(broker, snapshot, outputs, secret)


def test_nested_native_children_preserve_outer_and_narrower_denials(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        workspace = gate.outputs / "workspace"
        workspace.mkdir()
        other = gate.outputs / "other.txt"
        other.write_text("outside the child worktree")
        child = f"""from pathlib import Path
import json,os
results={{}}
for name,action in {{
 'source_write':lambda:Path({str(gate.snapshot / "immutable.txt")!r}).write_text('tamper'),
 'secret_read':lambda:Path({str(gate.secret)!r}).read_text(),
 'sibling_read':lambda:Path({str(other)!r}).read_text(),
 'unrelated_device':lambda:open('/dev/zero','rb').read(1),
}}.items():
 try:action()
 except PermissionError:results[name]='denied'
 else:raise AssertionError(name+' escaped')
Path('allowed.txt').write_text('native child wrote in its leased worktree')
master,slave=os.openpty()
os.close(slave);os.close(master)
results['pty']='allowed'
print(json.dumps(results))
"""
        result = gate.run(f"""
with contained_frontier_process((sys.executable,'-c',{child!r}),Path({str(workspace)!r}),
 posture=UnattendedImplementation(),harness=FrontierHarness.CODEX) as child:
 result=subprocess.run(child.command,cwd={str(workspace)!r},env=child.environment,capture_output=True,text=True)
 print(result.stdout, end=''); print(result.stderr, file=sys.stderr,end='')
 raise SystemExit(result.returncode)
""")
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout) == {
            "source_write": "denied",
            "secret_read": "denied",
            "sibling_read": "denied",
            "unrelated_device": "denied",
            "pty": "allowed",
        }
        assert (
            workspace / "allowed.txt"
        ).read_text() == "native child wrote in its leased worktree"
        assert (gate.snapshot / "immutable.txt").read_text() == "sealed source"
    assert all(record["leader_reaped"] for record in gate.broker.records)
    assert len({record["policy_sha256"] for record in gate.broker.records}) == 2


def test_stripped_gate_environment_reenters_only_its_owned_native_transport(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        result = gate.run("""
from local_first_agent_os.pow_wow.process import run_captured_command
from local_first_agent_os.toolchains import verification_gate_environment
environment,stripped=verification_gate_environment(Path.cwd())
assert BROKER_ENV in stripped and BROKER_ENV not in environment
environment[BROKER_ENV]='caller-selected owner must not be used'
capture=run_captured_command(
 (sys.executable,'-c',
  'import os;from local_first_agent_os.native_verification_broker import BROKER_ENV;'
  'print(os.environ[BROKER_ENV])'),
 Path.cwd(),timeout_seconds=5,env=environment,complete_environment=True)
assert capture.exit_code==0,capture.stderr
assert capture.stdout.strip()==os.environ[BROKER_ENV]
print('stripped configuration restored only at the owned launch boundary')
""")
        assert result.exit_code == 0, result.stderr
        assert "owned launch boundary" in result.stdout
    assert [record["kind"] for record in gate.broker.records].count("native") == 1
    assert all(record["leader_reaped"] for record in gate.broker.records)


def test_descendant_cannot_reuse_parent_policy_to_relax_read_only_grants(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        workspace = gate.outputs / "workspace"
        workspace.mkdir()
        inner = f"""import base64,json,os,socket
configuration=json.loads(os.environ[{BROKER_ENV!r}])
request={{'version':1,'nonce':configuration['nonce'],
 'command':['/usr/bin/touch',{str(workspace / "escaped")!r}],
 'cwd':{str(workspace)!r},'scratch':os.environ['TMPDIR'],
 'environment':{{key:os.environ[key] for key in ('PATH','HOME','TMPDIR')}},
 'posture':'unattended_implementation','harness':'codex'}}
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
 connection.connect(configuration['socket'])
 connection.sendall(json.dumps(request).encode()+b'\\n')
 messages=[json.loads(line) for line in connection.makefile('rb')]
 assert messages[-1]['kind']=='exit' and messages[-1]['code']!=0,messages
 errors=''.join(base64.b64decode(item['data']).decode()
  for item in messages if item['kind']=='stderr')
 assert 'Operation not permitted' in errors or 'Permission denied' in errors,errors
 print('descendant retained its read-only parent boundary')
"""
        result = gate.run(f"""
with contained_frontier_process((sys.executable,'-c',{inner!r}),Path({str(workspace)!r}),
 posture=ReadOnlyInspection(),harness=FrontierHarness.CODEX) as child:
 result=subprocess.run(child.command,env=child.environment,capture_output=True,text=True)
 print(result.stdout,end='');print(result.stderr,file=sys.stderr,end='')
 raise SystemExit(result.returncode)
""")
        assert result.exit_code == 0, result.stderr
        assert "retained its read-only parent" in result.stdout
        assert not (workspace / "escaped").exists()
    assert len([item for item in gate.broker.records if item["kind"] != "metadata"]) == 3
    assert all(record["leader_reaped"] for record in gate.broker.records)


def test_real_network_oracle_allows_only_the_parent_pinned_endpoint(tmp_path: Path) -> None:
    with socket.socket() as allowed, socket.socket() as denied:
        allowed.bind(("127.0.0.1", 0))
        allowed.listen()
        denied.bind(("127.0.0.1", 0))
        denied.listen()
        port, denied_port = allowed.getsockname()[1], denied.getsockname()[1]

        def echo() -> None:
            connection, _ = allowed.accept()
            with connection:
                connection.sendall(connection.recv(16))

        worker = threading.Thread(target=echo, daemon=True)
        worker.start()
        with _gate(tmp_path, port=port) as gate:
            code = f"""import errno,socket
with socket.create_connection(('127.0.0.1',{port}),timeout=2) as connection:
 connection.sendall(b'permitted');assert connection.recv(16)==b'permitted'
try:socket.create_connection(('127.0.0.1',{denied_port}),timeout=1)
except OSError as error:assert error.errno in (errno.EPERM,errno.EACCES)
else:raise AssertionError('unrelated endpoint escaped')
print('pinned endpoint allowed; unrelated endpoint denied')
"""
            result = gate.run(f"""
with contained_frontier_process((sys.executable,'-c',{code!r}),Path(os.environ['TMPDIR']),
 posture=ReadOnlyInspection(),harness=FrontierHarness.CODEX,
 overrides={{'LOCAL_AGENT_LEDGER_READER_DATABASE_URL':
  'postgresql://fixture@127.0.0.1:{port}/fixture'}}) as child:
 result=subprocess.run(child.command,env=child.environment,capture_output=True,text=True)
 print(result.stdout,end='');print(result.stderr,file=sys.stderr,end='')
 raise SystemExit(result.returncode)
""")
            assert result.exit_code == 0, result.stderr
            assert "unrelated endpoint denied" in result.stdout
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("stop", ["deadline", "revoke"])
def test_gate_stop_reaps_native_children_and_preserves_output(tmp_path: Path, stop: str) -> None:
    valid = threading.Event()
    valid.set()
    with _gate(tmp_path, seconds=1 if stop == "deadline" else 10, valid=valid.is_set) as gate:
        code = (
            "import time,sys;print('retained child stdout',flush=True);"
            "print('retained child stderr',file=sys.stderr,flush=True);time.sleep(30)"
        )
        timer = threading.Timer(0.8, valid.clear) if stop == "revoke" else None
        if timer is not None:
            timer.start()
        started = monotonic()
        result = gate.run(f"""
with contained_frontier_process((sys.executable,'-c',{code!r}),Path(os.environ['TMPDIR']),
 posture=ReadOnlyInspection(),harness=FrontierHarness.CODEX) as child:
 subprocess.run(child.command,env=child.environment,check=False)
time.sleep(30)
""")
        if timer is not None:
            timer.join()
        assert monotonic() - started < 4
        assert result.exit_code != 0
        assert "retained child stdout" in result.stdout
        assert "retained child stderr" in result.stderr
    assert len([item for item in gate.broker.records if item["kind"] != "metadata"]) == 2
    assert all(record["leader_reaped"] for record in gate.broker.records)
    assert gate.broker.authority_invalidated == (stop == "revoke")


def test_nonce_does_not_admit_a_process_outside_the_owned_group(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        configuration = json.loads(gate.broker.configuration)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(configuration["socket"])
            messages = connection.makefile("rb")
            first = json.loads(messages.readline())
            second = json.loads(messages.readline())
            assert first["kind"] == "stderr"
            assert second == {"kind": "exit", "code": 125}
        assert gate.broker.records == []


def test_disconnected_proxy_cancels_its_owned_native_child(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        ready = gate.outputs / "ready"
        code = (
            f"from pathlib import Path;import os,time;"
            f"Path({str(ready)!r}).write_text(str(os.getpid()));time.sleep(30)"
        )
        result = gate.run(f"""
with contained_frontier_process((sys.executable,'-c',{code!r}),Path(os.environ['TMPDIR']),
 posture=UnattendedImplementation(),harness=FrontierHarness.CODEX) as child:
 proxy=subprocess.Popen(child.command,env=child.environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 deadline=time.monotonic()+3
 while not Path({str(ready)!r}).exists():
  assert time.monotonic()<deadline
  time.sleep(.01)
 proxy.terminate();proxy.communicate(timeout=2)
 time.sleep(.2)
print('caller disconnected')
""")
        assert result.exit_code == 0, result.stderr
        pid = int(ready.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert all(record["leader_reaped"] for record in gate.broker.records)


@pytest.mark.parametrize(
    "mutation",
    [
        "request['profile']='(allow default)'",
        "request['runtime_dependencies']=[{'path':'/tmp/caller-selected.dylib'}]",
        "request['environment']['DYLD_INSERT_LIBRARIES']='/tmp/host-injection.dylib'",
        "request['command']=['/usr/bin/true']*257",
        "request['command']=['/usr/bin/true','x'*66000]",
    ],
)
def test_closed_protocol_refuses_undeclared_authority(tmp_path: Path, mutation: str) -> None:
    with _gate(tmp_path) as gate:
        result = gate.run(f"""
import base64,socket
configuration=json.loads(os.environ[BROKER_ENV])
request={{'version':1,'nonce':configuration['nonce'],'command':['/usr/bin/true'],
 'cwd':str(Path.cwd()),'scratch':os.environ['TMPDIR'],
 'environment':{{'PATH':os.environ['PATH']}},
 'posture':'read_only_inspection','harness':'codex'}}
{mutation}
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
 connection.connect(configuration['socket'])
 connection.sendall(json.dumps(request).encode()+b'\\n')
 messages=[json.loads(line) for line in connection.makefile('rb')]
 assert messages[-1]=={{'kind':'exit','code':125}},messages
 print('request refused before native launch')
""")
        assert result.exit_code == 0, result.stderr
        assert "request refused" in result.stdout
    assert not any(record["kind"] == "native" for record in gate.broker.records)


@pytest.mark.parametrize("replacement", ["fifo", "host-secret"])
def test_executable_replacement_cannot_block_or_read_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    with _gate(tmp_path) as gate:
        script = gate.outputs / "mutable-executable"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o700)
        original = gate.broker._read_shebang

        def replace_before_read(parent, cwd, path):
            if path == script:
                script.unlink()
                if replacement == "fifo":
                    os.mkfifo(script)
                else:
                    script.symlink_to(gate.secret)
            return original(parent, cwd, path)

        monkeypatch.setattr(gate.broker, "_read_shebang", replace_before_read)
        started = monotonic()
        result = gate.run(f"""
with contained_frontier_process(({str(script)!r},),Path(os.environ['TMPDIR']),
 posture=ReadOnlyInspection(),harness=FrontierHarness.CODEX) as child:
 result=subprocess.run(child.command,env=child.environment,capture_output=True,text=True)
 assert result.returncode==125,result
 assert 'bounded executable inspection' in result.stderr,result.stderr
 print('mutable executable refused within bounded contained inspection')
""")
        assert result.exit_code == 0, result.stderr
        assert monotonic() - started < 4
        assert gate.secret.read_text() not in result.stdout + result.stderr
    assert not any(record["kind"] == "native" for record in gate.broker.records)


@pytest.mark.parametrize("mode", ["success", "signal", "timeout"])
def test_owned_capture_uses_broker_launch_and_preserves_termination(
    tmp_path: Path, mode: str
) -> None:
    code = "print('owned child output',flush=True);"
    code += {
        "success": "raise SystemExit(0)",
        "signal": "import os,signal;os.kill(os.getpid(),signal.SIGTERM)",
        "timeout": "import time;time.sleep(30)",
    }[mode]
    with _gate(tmp_path) as gate:
        result = gate.run(f"""
from local_first_agent_os.pow_wow.process import run_captured_command
capture=run_captured_command((sys.executable,'-c',{code!r}),Path.cwd(),timeout_seconds=.5)
assert capture.exit_code=={{'success':0,'signal':-15,'timeout':124}}[{mode!r}],capture
assert 'owned child output' in capture.stdout,capture
print('owned capture preserved termination and output')
""")
        assert result.exit_code == 0, result.stderr
        assert "owned capture preserved" in result.stdout
    assert any(record["kind"] == "native" for record in gate.broker.records)


def test_streaming_supervisor_launches_an_owned_native_child(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        result = gate.run("""
import asyncio
from types import SimpleNamespace
from local_first_agent_os.agent_execution_supervisor import StreamingCommandSupervisor
from local_first_agent_os.coordination.contracts import AcknowledgementResult,LedgerRecord
from local_first_agent_os.pow_wow.types import ExecutionAttemptLease
class Artifacts:
 def write_text(self,**kwargs):return SimpleNamespace(artifact_id='fixture-transcript')
supervisor=StreamingCommandSupervisor(
 coordination_command=lambda command:AcknowledgementResult(command.name,LedgerRecord({})),
 artifact_writer=Artifacts(),heartbeat_seconds=.1)
lease=ExecutionAttemptLease(idempotency_key='fixture',worker_id='fixture-worker',
 lease_id='fixture-lease',created=True,open_status='ACTIVE')
result=asyncio.run(supervisor.run((sys.executable,'-c',"print('supervised native output')"),
 Path.cwd(),lease=lease,harness='codex',timeout_seconds=3))
assert result.allows_task_completion,result
assert 'supervised native output' in result.capture.stdout,result
print('streaming supervisor reached legitimate completion')
""")
        assert result.exit_code == 0, result.stderr
        assert "legitimate completion" in result.stdout
    assert any(record["kind"] == "native" for record in gate.broker.records)


def test_fragmented_request_cannot_extend_the_admission_deadline(tmp_path: Path) -> None:
    with _gate(tmp_path) as gate:
        result = gate.run("""
import socket
configuration=json.loads(os.environ[BROKER_ENV])
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
 connection.connect(configuration['socket'])
 started=time.monotonic()
 for _ in range(30):
  try:connection.sendall(b' ')
  except BrokenPipeError:break
  time.sleep(.06)
 assert time.monotonic()-started<.95,'request drip reset its bounded admission timeout'
 messages=[json.loads(line) for line in connection.makefile('rb')]
 assert messages[-1]=={'kind':'exit','code':125},messages
 print('request deadline retained across partial reads')
""")
        assert result.exit_code == 0, result.stderr
        assert "deadline retained" in result.stdout
    assert not any(record["kind"] == "native" for record in gate.broker.records)

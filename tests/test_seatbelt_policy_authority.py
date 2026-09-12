# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A rendered parent policy remains the authority after caller-owned paths change."""

import json
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from local_first_agent_os.host_verification import _sandbox_policy
from local_first_agent_os.process_containment import (
    ProcessContainmentUnavailable,
    _reader_database_endpoints,
)
from local_first_agent_os.seatbelt_policy import PathGrant, SeatbeltPolicy, UnixGrant


def test_production_relay_and_broker_profile_passes_protected_helper_parser(tmp_path: Path) -> None:
    snapshot, outputs = tmp_path / "snapshot", tmp_path / "outputs"
    snapshot.mkdir()
    outputs.mkdir()
    # The exclusive-UID broker owns process lifetime, as in the production gate.
    policy = replace(
        _sandbox_policy(snapshot, outputs, (), (tmp_path / "secret",), relay_port=65432),
        fixed_process_group=False,
    ).with_broker(outputs / "native.sock")
    profile = policy.render()
    helper = Path(__file__).parents[1] / "scripts/verifier_uid/uid_gate.py"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            (
                "import json,runpy,sys; gate=runpy.run_path(sys.argv[1]); "
                "profile=sys.stdin.read(); "
                "assert gate['protected_profile'](profile)==profile+' '+gate['IDENTITY_RULES']; "
                "print(json.dumps({'accepted':True}))"
            ),
            str(helper),
        ],
        input=profile,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {"accepted": True}
    assert '(remote tcp "localhost:65432")' in profile
    assert "(deny network*)" in profile
    assert "(remote ip " not in profile


def test_symlink_swap_cannot_change_a_retained_parent_grant(tmp_path: Path) -> None:
    first, other = tmp_path / "first", tmp_path / "other"
    first.mkdir()
    other.mkdir()
    path = PathGrant(first)
    policy = SeatbeltPolicy(reads=((path,),), writes=((),), outbound=((),))
    rendered = policy.render()
    child = SeatbeltPolicy(reads=((PathGrant(other),),), writes=((),), outbound=((),))

    first.rmdir()
    first.symlink_to(other, target_is_directory=True)

    assert policy.render() == rendered
    assert path.path == tmp_path / "first"
    assert not policy.allows_read(other)
    assert not policy.allows_read(first)
    assert not policy.intersect(child).allows_read(other)


def test_unix_socket_grant_does_not_follow_a_replacement_symlink(tmp_path: Path) -> None:
    socket_path = tmp_path / "socket"
    grant = UnixGrant(socket_path)
    rendered = grant.render()
    socket_path.symlink_to(tmp_path / "unrelated-socket")
    assert grant.render() == rendered


def test_new_child_resources_require_parent_read_and_write_authority(tmp_path: Path) -> None:
    scratch, other = tmp_path / "scratch", tmp_path / "other"
    scratch.mkdir()
    other.mkdir()
    policy = SeatbeltPolicy(
        reads=((PathGrant(tmp_path),),),
        writes=((PathGrant(scratch),),),
        outbound=((),),
    )
    assert policy.allows_new_subtree(scratch)
    assert not policy.allows_new_subtree(other)
    assert not policy.allows_new_subtree(tmp_path)


def test_resource_subtree_cannot_cover_a_forbidden_descendant(tmp_path: Path) -> None:
    policy = SeatbeltPolicy(
        reads=((PathGrant(tmp_path),),),
        writes=((PathGrant(tmp_path),),),
        outbound=((),),
        forbidden_reads=(PathGrant(tmp_path / "secret"),),
    )
    assert not policy.allows_new_subtree(tmp_path)
    assert policy.allows_new_subtree(tmp_path / "fresh")


@pytest.mark.parametrize("hostname", ["secret.attacker.invalid", "192.0.2.1", "[2001:db8::1]"])
def test_reader_url_cannot_make_the_host_perform_dns(
    monkeypatch: pytest.MonkeyPatch, hostname: str
) -> None:
    def unexpected_dns(*args, **kwargs):
        raise AssertionError("uncontained host DNS lookup")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(ProcessContainmentUnavailable, match="loopback"):
        _reader_database_endpoints(
            {"LOCAL_AGENT_LEDGER_READER_DATABASE_URL": f"postgresql://fixture@{hostname}:5433/db"}
        )


@pytest.mark.parametrize("hostname", ["127.0.0.1", "[::1]", "localhost"])
def test_reader_url_retains_numeric_loopback_and_localhost_without_dns(
    monkeypatch: pytest.MonkeyPatch, hostname: str
) -> None:
    def unexpected_dns(*args, **kwargs):
        raise AssertionError("loopback grants do not require DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    assert _reader_database_endpoints(
        {"LOCAL_AGENT_LEDGER_READER_DATABASE_URL": f"postgresql://fixture@{hostname}:5433/db"}
    ) == ("localhost:5433",)

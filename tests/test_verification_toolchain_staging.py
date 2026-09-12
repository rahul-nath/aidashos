# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Relocation preserves installed bytes and refuses authority outside the declared roots."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from local_first_agent_os import host_verification
from local_first_agent_os import verification_resources as resources
from local_first_agent_os import verification_toolchain_staging as staging
from local_first_agent_os.uid_verifier_client import UidVerifierClient, UidVerifierUnavailable


@pytest.fixture
def installed(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ("environment", "python-base", "git", "bin")}
    for root in roots.values():
        (root / "bin").mkdir(parents=True)
    base_python = roots["python-base"] / "bin" / "python3"
    base_python.write_bytes(b"installed Python fixture bytes")
    python = roots["environment"] / "bin" / "python"
    python.symlink_to(base_python)
    (roots["environment"] / "pyvenv.cfg").write_text(
        f"home = {base_python.parent}\nexecutable = {base_python}\n"
    )
    (roots["environment"] / "bin" / "pytest").write_text(f"#!{python}\nprint('test')\n")
    executables = (roots["bin"] / "uv", roots["git"] / "bin" / "git", roots["bin"] / "node")
    for executable in executables:
        executable.write_bytes(b"installed " + executable.name.encode())
    original = host_verification._InstalledToolchain(
        roots["environment"], executables, tuple(roots.values())
    )
    monkeypatch.setattr(
        host_verification, "_installed_toolchain", lambda _project, **_kwargs: original
    )
    monkeypatch.setattr(staging.sys, "base_prefix", str(roots["python-base"]))
    monkeypatch.setattr(staging, "linked_runtime_files", lambda path: (path,))
    return original, roots


def test_relocated_node_pin_reaches_nested_containment_with_an_isolated_home(
    tmp_path, installed, monkeypatch
):
    from local_first_agent_os.process_containment import _allowed_environment

    original, _roots = installed
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / ".nvmrc").write_text("22.19.0\n")
    installed_node = tmp_path / "nvm" / "versions" / "node" / "v22.19.0" / "bin" / "node"
    installed_node.parent.mkdir(parents=True)
    installed_node.write_bytes(b"the exact pinned Node")
    original = replace(original, executables=(*original.executables[:2], installed_node))
    discoveries = []

    def discover(project_root, *, source_root=None):
        discoveries.append((project_root, source_root))
        return original

    monkeypatch.setattr(host_verification, "_installed_toolchain", discover)
    destination = tmp_path / "destination"
    destination.mkdir()
    result = staging.stage_installed_toolchain(tmp_path, destination, source_root=snapshot)
    assert discoveries == [(tmp_path.resolve(), snapshot)]
    staged_node = result.toolchain.executables[2]
    assert staged_node.read_bytes() == installed_node.read_bytes()
    installed_node.unlink()
    scratch = tmp_path / "isolated-home"
    scratch.mkdir()
    monkeypatch.setenv("HOME", str(scratch))
    monkeypatch.delenv("NVM_DIR", raising=False)
    gate_environment = result.toolchain.gate_environment(snapshot, scratch)
    nested = _allowed_environment(snapshot, gate_environment, scratch)
    assert Path(nested["NVM_DIR"]).is_relative_to(destination / "installed" / "native")
    assert Path(nested["PATH"].split(os.pathsep)[0]) / "node" == staged_node
    assert nested["HOME"] == str(scratch)
    manifest = json.loads(result.manifest.read_text())
    node_row = next(row for row in manifest if row.get("source") == str(installed_node))
    assert node_row["source_sha256"] == hashlib.sha256(staged_node.read_bytes()).hexdigest()
    assert node_row["staged_sha256"] == node_row["source_sha256"]


def test_relocation_records_bytes_and_preserves_virtual_environment_identity(tmp_path, installed):
    original, roots = installed
    destination = tmp_path / "destination"
    destination.mkdir()
    result = staging.stage_installed_toolchain(tmp_path, destination)
    final = destination / "installed"
    assert (
        final / "environment" / "bin" / "python"
    ).resolve() == final / "python-base" / "bin" / "python3"
    assert (final / "environment" / "bin" / "pytest").read_text().splitlines()[0] == (
        "#!" + str(final / "environment" / "bin" / "python")
    )
    assert str(final / "python-base" / "bin") in (final / "environment" / "pyvenv.cfg").read_text()
    assert result.toolchain.executables[0].read_bytes() == original.executables[0].read_bytes()
    assert result.manifest_digest == hashlib.sha256(result.manifest.read_bytes()).hexdigest()
    manifest = json.loads(result.manifest.read_text())
    assert any(
        row.get("source_link") == str(roots["python-base"] / "bin" / "python3") for row in manifest
    )
    assert all(not path.is_relative_to(roots["environment"]) for path in result.toolchain.readable)
    with pytest.raises(ValueError, match="fresh operator-owned"):
        staging.stage_installed_toolchain(tmp_path, destination)


def _ca_resource(tmp_path: Path) -> resources.VerificationResourceLease:
    private = tmp_path / "private-owner"
    private.mkdir(mode=0o700)
    certificate = private / "ca.pem"
    certificate.write_bytes(b"declared public trust bundle\n")
    certificate.chmod(0o400)
    (private / "owner-credential").write_text("must never be staged")
    identity = resources.NeonVerificationResourceIdentity(
        target_project_id="fixture_project",
        neon_project_id="fixture-neon",
        source_common_directory=str(tmp_path / ".git"),
        lease_id="a" * 32,
        role="aidashos_verify_" + "a" * 32,
        host="ep-fixture.us-east-2.aws.neon.tech",
        hostaddr="8.8.8.8",
        relay_port=12345,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    owner = resources._NeonConnection(
        identity.host, identity.hostaddr, "owner", "owner-secret", certificate
    )
    worker = replace(owner, username=identity.role, password="worker-secret")
    return resources.VerificationResourceLease(
        identity, owner, worker, private / "lease.json", Mock(spec=resources._PinnedLoopbackRelay)
    )


def test_public_ca_staging_projects_only_worker_trust_and_retains_cleanup_ownership(
    tmp_path, installed
):
    lease = _ca_resource(tmp_path)
    source = lease.public_ca
    assert source is not None
    original_bytes = source.path.read_bytes()
    destination = tmp_path / "destination"
    destination.mkdir()
    staged = staging.stage_installed_toolchain(tmp_path, destination, public_ca=source)
    assert staged.public_ca is not None
    copied = staged.public_ca.copied.path
    assert copied == destination / "installed" / "public-resources" / "postgres-ca.pem"
    assert copied.read_bytes() == original_bytes
    environment = lease.environment(staged_ca=staged.public_ca)
    url = urlsplit(environment["LOCAL_AGENT_TEST_DATABASE_URL"])
    query = parse_qs(url.query)
    assert set(environment) == {"LOCAL_AGENT_TEST_DATABASE_URL"}
    assert query["sslrootcert"] == [str(copied)]
    assert query["sslmode"] == ["verify-full"]
    assert query["channel_binding"] == ["require"]
    assert query["hostaddr"] == ["127.0.0.1"]
    assert isinstance(lease.identity, resources.NeonVerificationResourceIdentity)
    assert url.hostname == lease.identity.host and url.port == lease.identity.relay_port
    assert url.username == lease.identity.role and url.password == "worker-secret"
    assert lease._owner.username == "owner" and lease._owner.password == "owner-secret"
    assert isinstance(lease._worker, resources._NeonConnection)
    assert lease._worker.ca_file == source.path
    assert lease._manifest == source.path.parent / "lease.json"
    assert isinstance(lease._relay, Mock)
    assert lease._relay.mock_calls == []
    assert source.path.read_bytes() == original_bytes
    assert source.path.parent.stat().st_mode & 0o777 == 0o700
    assert source.path.stat().st_mode & 0o777 == 0o400
    manifest_text = staged.manifest.read_text()
    row = next(row for row in json.loads(manifest_text) if row.get("public_resource_kind"))
    assert row == {
        "path": "public-resources/postgres-ca.pem",
        "source": str(source.path),
        "source_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "staged_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "public_resource_kind": "postgres_tls_ca",
    }
    assert all(
        secret not in manifest_text
        for secret in ("owner-secret", "worker-secret", "owner-credential")
    )
    staged.cleanup_after(_ClosedOwner(destination, closed=True))
    assert not copied.exists()
    assert source.path.read_bytes() == original_bytes


@pytest.mark.parametrize("failure", ["missing", "wrong_source", "tampered"])
def test_neon_worker_refuses_missing_mismatched_or_changed_staged_ca(tmp_path, installed, failure):
    lease = _ca_resource(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    staged = staging.stage_installed_toolchain(tmp_path, destination, public_ca=lease.public_ca)
    ca = staged.public_ca
    assert ca is not None
    if failure == "missing":
        ca = None
    elif failure == "wrong_source":
        ca = replace(ca, source=staging.PublicVerificationCa(tmp_path / "other-ca.pem"))
    else:
        ca.copied.path.chmod(0o600)
        ca.copied.path.write_bytes(b"changed trust roots\n")
    with pytest.raises(ValueError, match="staged public CA|different resource|runtime dependency"):
        lease.environment(staged_ca=ca)
    assert isinstance(lease._relay, Mock)
    assert lease._relay.mock_calls == []


@pytest.mark.parametrize("kind", ["symlink", "directory", "empty", "oversized"])
def test_public_ca_refuses_invalid_sources_without_partial_publication(tmp_path, installed, kind):
    source = tmp_path / "public-ca"
    if kind == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(b"not a declared regular source")
        source.symlink_to(target)
    elif kind == "directory":
        source.mkdir()
    else:
        source.write_bytes(b"" if kind == "empty" else b"x" * (4 * 1024 * 1024 + 1))
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises((ValueError, OSError)):
        staging.stage_installed_toolchain(
            tmp_path, destination, public_ca=staging.PublicVerificationCa(source)
        )
    assert list(destination.iterdir()) == []


def test_local_worker_needs_no_ca_and_refuses_an_unrelated_staged_ca(tmp_path, installed):
    neon = _ca_resource(tmp_path)
    owner = resources._LocalPostgresConnection("owner", "owner-secret")
    identity = resources.LocalPostgresVerificationResourceIdentity(
        target_project_id=neon.identity.target_project_id,
        source_common_directory=neon.identity.source_common_directory,
        lease_id=neon.identity.lease_id,
        role=neon.identity.role,
        relay_port=neon.identity.relay_port,
        expires_at=neon.identity.expires_at,
    )
    lease = resources.VerificationResourceLease(
        identity, owner, replace(owner, username=identity.role), neon._manifest, neon._relay
    )
    assert lease.public_ca is None
    assert parse_qs(urlsplit(lease.environment()["LOCAL_AGENT_TEST_DATABASE_URL"]).query)[
        "sslmode"
    ] == ["disable"]
    destination = tmp_path / "destination"
    destination.mkdir()
    staged = staging.stage_installed_toolchain(tmp_path, destination, public_ca=neon.public_ca)
    with pytest.raises(ValueError, match="no public CA"):
        lease.environment(staged_ca=staged.public_ca)


def test_source_provenance_describes_copied_bytes_when_source_changes_after_copy(
    tmp_path, installed, monkeypatch
):
    original, _roots = installed
    source = original.executables[0]
    copied_bytes = source.read_bytes()
    source.chmod(0o4755)
    destination = tmp_path / "destination"
    destination.mkdir()
    chmod = Path.chmod

    def change_source_after_copy(path, mode, **kwargs):
        chmod(path, mode, **kwargs)
        if path.name == source.name and path.is_relative_to(destination):
            source.write_bytes(b"installed source changed after the copy completed")

    monkeypatch.setattr(Path, "chmod", change_source_after_copy)
    result = staging.stage_installed_toolchain(tmp_path, destination)
    copied = result.toolchain.executables[0]
    manifest = json.loads(result.manifest.read_text())
    row = next(item for item in manifest if item["source"] == str(source))
    assert source.read_bytes() != copied_bytes
    assert copied.read_bytes() == copied_bytes
    assert row["source_sha256"] == hashlib.sha256(copied_bytes).hexdigest()
    assert row["staged_sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()
    assert copied.stat().st_mode & 0o7777 == 0o755


def test_private_umask_keeps_staged_symlinks_readable_by_the_gate_group(tmp_path, installed):
    _, roots = installed
    original_link = roots["environment"] / "bin" / "python"
    original_mode = original_link.lstat().st_mode
    target_mode = original_link.stat().st_mode
    destination = tmp_path / "destination"
    destination.mkdir()
    previous = os.umask(0o077)
    try:
        result = staging.stage_installed_toolchain(tmp_path, destination)
    finally:
        os.umask(previous)
    link = destination / "installed/environment/bin/python"
    info = link.lstat()
    assert info.st_gid == destination.stat().st_gid
    assert info.st_mode & 0o040, "the gate group must be able to inspect the symlink"
    assert original_link.lstat().st_mode == original_mode
    assert link.stat().st_mode == target_mode, "link admission must never chmod its target"
    row = next(
        row
        for row in json.loads(result.manifest.read_text())
        if row["path"] == "environment/bin/python"
    )
    assert row["staged_link_mode"] == oct(info.st_mode & 0o777)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin symlink read authorization")
def test_native_link_following_does_not_prove_readlink_permission(tmp_path):
    link = tmp_path / "executable"
    link.symlink_to("/usr/bin/true")
    link.chmod(0o300, follow_symlinks=False)
    assert subprocess.run([str(link)], timeout=5, check=False).returncode == 0
    with pytest.raises(PermissionError):
        os.readlink(link)
    link.chmod(0o700, follow_symlinks=False)
    assert os.readlink(link) == "/usr/bin/true"
    assert link.resolve(strict=True) == Path("/usr/bin/true")


def test_external_runtime_symlink_is_refused_without_partial_publication(tmp_path, installed):
    _, roots = installed
    external = tmp_path / "external"
    external.write_text("outside the declared runtimes")
    (roots["environment"] / "external").symlink_to(external)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ValueError, match="symlink escapes"):
        staging.stage_installed_toolchain(tmp_path, destination)
    assert list(destination.iterdir()) == []
    assert external.read_text() == "outside the declared runtimes"


def test_privileged_staging_refused_before_reading_or_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(PermissionError, match="unprivileged operator"):
        staging.stage_installed_toolchain(tmp_path, tmp_path / "does-not-exist")
    assert not (tmp_path / "does-not-exist").exists()


def test_project_editable_path_requires_and_selects_frozen_source(tmp_path, installed):
    original, _roots = installed
    project_source = tmp_path / "src"
    project_source.mkdir()
    pth = original.environment / "project.pth"
    pth.write_text(str(project_source) + "\n")
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ValueError, match="frozen source root"):
        staging.stage_installed_toolchain(tmp_path, destination)
    assert list(destination.iterdir()) == []
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    result = staging.stage_installed_toolchain(tmp_path, destination, source_root=frozen)
    assert (result.toolchain.environment / "project.pth").read_text() == str(frozen / "src") + "\n"
    assert pth.read_text() == str(project_source) + "\n"


class _ClosedOwner(UidVerifierClient):
    """The actual receipt/UID-set parser is covered by transport contract tests."""

    def __init__(self, anchor: Path, *, closed: bool) -> None:
        self.anchor, self.closed = anchor, closed

    def require_closed_staging(self, directory: Path) -> None:
        if not self.closed:
            raise UidVerifierUnavailable("aggregate UID cleanup is unproven")
        assert directory == self.anchor


def _staged(tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    return staging.stage_installed_toolchain(tmp_path, destination)


def test_staged_tree_is_retained_until_aggregate_uid_closure(tmp_path, installed):
    result = _staged(tmp_path)
    owner = _ClosedOwner(result.anchor, closed=False)
    with pytest.raises(UidVerifierUnavailable, match="unproven"):
        result.cleanup_after(owner)
    assert (result.anchor / "installed").is_dir()
    owner.closed = True
    result.cleanup_after(owner)
    assert not (result.anchor / "installed").exists()
    assert result.manifest.is_file()
    assert result.manifest.parent == result.anchor
    assert hashlib.sha256(result.manifest.read_bytes()).hexdigest() == result.manifest_digest
    assert all(path.is_file() for path in installed[0].executables)


def test_symlinked_installed_root_cannot_redirect_cleanup(tmp_path, installed):
    result = _staged(tmp_path)
    original = result.anchor / "installed"
    retained = result.anchor / "retained"
    original.rename(retained)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "must-remain"
    canary.write_text("outside the cleanup authority")
    original.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        result.cleanup_after(_ClosedOwner(result.anchor, closed=True))
    assert canary.read_text() == "outside the cleanup authority"
    assert retained.is_dir()


def test_internal_symlink_is_unlinked_without_removing_its_target(tmp_path, installed):
    result = _staged(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "must-remain"
    canary.write_text("outside the cleanup authority")
    (result.anchor / "installed" / "redirect").symlink_to(outside, target_is_directory=True)
    result.cleanup_after(_ClosedOwner(result.anchor, closed=True))
    assert canary.read_text() == "outside the cleanup authority"
    assert result.manifest.is_file()


def test_replaced_anchor_is_not_a_cleanup_target(tmp_path, installed):
    result = _staged(tmp_path)
    retained = tmp_path / "retained"
    result.anchor.rename(retained)
    result.anchor.mkdir()
    (result.anchor / "installed").mkdir()
    with pytest.raises(ValueError, match="anchor identity changed"):
        result.cleanup_after(_ClosedOwner(result.anchor, closed=True))
    assert (retained / "installed").is_dir()
    assert (result.anchor / "installed").is_dir()


def test_relocation_preserves_native_sibling_library_layout(tmp_path, installed, monkeypatch):
    original, _roots = installed
    node = original.executables[2]
    library = node.parent.parent / "lib" / "libnode.dylib"
    library.parent.mkdir()
    library.write_bytes(b"native library")

    def dependencies(path):
        if path.name != "node":
            return (path,)
        linked = path.parent.parent / "lib" / "libnode.dylib"
        assert linked.is_file()
        return (path, linked)

    monkeypatch.setattr(staging, "linked_runtime_files", dependencies)
    destination = tmp_path / "destination"
    destination.mkdir()
    result = staging.stage_installed_toolchain(tmp_path, destination)
    copied_node = result.toolchain.executables[2]
    copied_library = copied_node.parent.parent / "lib" / "libnode.dylib"
    assert copied_library.read_bytes() == library.read_bytes()
    assert copied_library.is_relative_to(destination)
    assert any(row["source"] == str(library) for row in json.loads(result.manifest.read_text()))


def test_relocated_loader_cannot_add_a_host_grant(tmp_path, installed, monkeypatch):
    original, _roots = installed
    foreign = tmp_path / "private-file"
    foreign.write_bytes(b"not a declared tool")

    def dependencies(path):
        if path in original.executables:
            return (path,)
        return (path, foreign)

    monkeypatch.setattr(staging, "linked_runtime_files", dependencies)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ValueError, match="escaped its exact-file grants"):
        staging.stage_installed_toolchain(tmp_path, destination)
    assert not list(destination.iterdir())


def test_retained_git_loader_closure_is_bound_into_published_provenance(
    tmp_path, installed, monkeypatch
):
    original, _ = installed
    library = tmp_path / "declared.dylib"
    library.write_bytes(b"the declared native library")
    alias = tmp_path / "loader-alias.dylib"
    alias.symlink_to(library)
    original = replace(original, readable=(*original.readable, library))
    monkeypatch.setattr(host_verification, "_installed_toolchain", lambda *_args, **_kw: original)
    monkeypatch.setattr(
        staging,
        "linked_runtime_files",
        lambda path: (path, library) if path.name == "git" else (path,),
    )
    monkeypatch.setattr(
        staging, "linked_runtime_references", lambda path: {path: path, alias: library}
    )
    result = _staged(tmp_path)
    (closure,) = result.runtime_dependencies
    assert closure.executable.path == result.toolchain.executables[1]
    assert closure.verified_reads() == (library,)
    entry = next(
        row for row in json.loads(result.manifest.read_text()) if row["path"] == "git/bin/git"
    )
    assert entry["native_runtime_closure"] == {
        "executable": closure.executable.payload(),
        "dependencies": [dependency.payload() for dependency in closure.dependencies],
    }
    assert entry["staged_sha256"] == closure.executable.sha256
    assert closure.dependencies[0].references == (alias,)


def test_staging_canonicalizes_parent_alias_after_refusing_leaf_symlinks(tmp_path, installed):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    destination = alias / "destination"
    destination.mkdir()
    result = staging.stage_installed_toolchain(tmp_path, destination)
    assert result.anchor == destination.resolve()
    assert all(path.is_relative_to(result.anchor) for path in result.toolchain.executables)

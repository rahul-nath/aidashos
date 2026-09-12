# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native dependency grants follow loader metadata, never a directory search grant."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_first_agent_os import macho_dependencies as macho


@pytest.fixture
def native_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    images: dict[Path, str] = {}
    inspected: list[Path] = []

    def add(name: str, *commands: tuple[str, str]) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"native fixture")
        lines = [str(path) + ":", "Load command 0", "      cmd LC_SEGMENT_64", "  cmdsize 72"]
        for ordinal, (kind, value) in enumerate(commands, start=1):
            key = "path" if kind == "LC_RPATH" else "name"
            lines.extend(
                (
                    f"Load command {ordinal}",
                    f"          cmd {kind}",
                    "      cmdsize 80",
                    f"         {key} {value} (offset 24)",
                    "   time stamp 2 Wed Dec 31 19:00:02 1969",
                    "      current version 1.0.0",
                    "compatibility version 1.0.0",
                )
            )
        images[path] = "\n".join(lines) + "\n"
        return path

    def inspect(command, **kwargs):
        assert len(command) == 5
        assert command[:4] == ("/usr/bin/otool", "-arch", "arm64", "-l")
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        path = Path(command[4])
        inspected.append(path)
        assert path in images, f"unexpected native image inspection: {path}"
        return subprocess.CompletedProcess(command, 0, images[path], "")

    monkeypatch.setattr(macho.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(macho.subprocess, "run", inspect)
    return add, images, inspected


@pytest.mark.parametrize(
    "kind",
    (
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
    ),
)
def test_declared_native_load_commands_resolve_exact_files(native_images, kind: str):
    add, _, inspected = native_images
    library = add("libraries with spaces/dependency.dylib")
    executable = add("bin/tool", (kind, str(library)))
    assert macho.linked_runtime_files(executable) == tuple(sorted((executable, library)))
    assert inspected == [executable, library]


def test_install_name_is_identity_not_a_dependency_edge(native_images):
    add, _, inspected = native_images
    library = add("lib/owned.dylib", ("LC_ID_DYLIB", "@rpath/not-installed-identity.dylib"))
    executable = add("bin/tool", ("LC_LOAD_DYLIB", str(library)))
    assert set(macho.linked_runtime_files(executable)) == {executable, library}
    assert inspected == [executable, library]


def test_loader_and_executable_anchors_remain_distinct_in_transitive_images(native_images):
    add, _, _ = native_images
    local = add("plugins/private/local.dylib")
    entry = add("runtime/entry.dylib")
    plugin = add(
        "plugins/plugin.dylib",
        ("LC_LOAD_DYLIB", "@loader_path/private/local.dylib"),
        ("LC_LOAD_DYLIB", "@executable_path/../runtime/entry.dylib"),
    )
    executable = add("bin/tool", ("LC_LOAD_DYLIB", "@loader_path/../plugins/plugin.dylib"))
    assert set(macho.linked_runtime_files(executable)) == {executable, plugin, local, entry}


def test_runpath_order_prefers_current_loader_then_inherits_parent_paths(native_images):
    add, _, inspected = native_images
    selected = add("plugin/first/shared.dylib")
    later = add("plugin/second/shared.dylib")
    inherited_shadow = add("app-libs/shared.dylib")
    inherited = add("app-libs/inherited.dylib")
    plugin = add(
        "plugin/plugin.dylib",
        ("LC_RPATH", "@loader_path/missing"),
        ("LC_RPATH", "@loader_path/first"),
        ("LC_RPATH", "@loader_path/second"),
        ("LC_LOAD_DYLIB", "@rpath/shared.dylib"),
        ("LC_LOAD_DYLIB", "@rpath/inherited.dylib"),
    )
    executable = add(
        "bin/tool",
        ("LC_RPATH", "@executable_path/../app-libs"),
        ("LC_LOAD_DYLIB", str(plugin)),
    )
    assert set(macho.linked_runtime_files(executable)) == {executable, plugin, selected, inherited}
    assert later not in inspected and inherited_shadow not in inspected


def test_absolute_runpath_is_a_search_location_not_a_read_grant(native_images, tmp_path: Path):
    add, _, _ = native_images
    library = add("installed/lib/dependency.dylib")
    unrelated = add("installed/lib/unrelated.dylib")
    executable = add(
        "bin/tool",
        ("LC_RPATH", str(tmp_path / "installed/lib")),
        ("LC_LOAD_DYLIB", "@rpath/dependency.dylib"),
    )
    observed = macho.linked_runtime_files(executable)
    assert set(observed) == {executable, library}
    assert library.parent not in observed and unrelated not in observed


def test_loader_alias_is_retained_alongside_its_resolved_file(native_images, tmp_path: Path):
    add, _, _ = native_images
    library = add("cellar/library.dylib")
    alias = tmp_path / "library.dylib"
    alias.symlink_to(library)
    executable = add("bin/tool", ("LC_LOAD_DYLIB", str(alias)))
    assert macho.linked_runtime_references(executable)[alias] == library


@pytest.fixture
def runtime_closure(tmp_path: Path):
    executable = tmp_path / "bin/tool"
    executable.parent.mkdir()
    executable.write_bytes(b"native tool")
    executable.chmod(0o755)
    library = tmp_path / "library.dylib"
    library.write_bytes(b"original bytes")
    alias = tmp_path / "alias.dylib"
    alias.symlink_to(library)
    closure = macho.RuntimeDependencyClosure(
        macho.PinnedRuntimeFile.capture(executable),
        (macho.PinnedRuntimeFile.capture(library, references=(alias,)),),
    )
    return closure, library, alias


@pytest.mark.parametrize("change", ["bytes", "deleted", "replaced", "alias"])
def test_changed_runtime_dependency_refuses_before_granting_reads(runtime_closure, change):
    closure, library, alias = runtime_closure
    if change == "bytes":
        library.write_bytes(b"modified bytes")
    elif change == "deleted":
        library.unlink()
    elif change == "replaced":
        replacement = library.with_suffix(".new")
        replacement.write_bytes(library.read_bytes())
        replacement.replace(library)
    else:
        replacement = library.with_suffix(".other")
        replacement.write_bytes(library.read_bytes())
        alias.unlink()
        alias.symlink_to(replacement)
    with pytest.raises((ValueError, OSError)):
        macho.runtime_dependency_reads((closure,), (str(closure.executable.path),), {})


def test_unselected_closure_adds_no_read_even_when_its_dependency_changed(runtime_closure):
    closure, library, _ = runtime_closure
    library.write_bytes(b"changed")
    assert (
        macho.runtime_dependency_reads((closure,), ("/usr/bin/true",), {"PATH": "/usr/bin"}) == ()
    )


def test_selected_runtime_closure_grants_only_exact_declared_files(runtime_closure):
    closure, library, _ = runtime_closure
    paths = macho.runtime_dependency_reads(
        (closure,), ("/usr/bin/true",), {"PATH": str(closure.executable.path.parent)}
    )
    assert paths == (library,)
    assert library.parent not in paths


@pytest.mark.parametrize("reference", ("relative.dylib", "@unknown/library.dylib"))
def test_unsupported_references_are_refused(native_images, reference: str):
    add, _, _ = native_images
    executable = add("bin/tool", ("LC_LOAD_DYLIB", reference))
    with pytest.raises(ValueError, match="unsupported.*loader reference"):
        macho.linked_runtime_files(executable)


@pytest.mark.parametrize("reference", ("@rpath/missing.dylib", "@loader_path/missing.dylib"))
def test_missing_required_library_names_requesting_image(native_images, reference: str):
    add, _, _ = native_images
    executable = add("bin/tool", ("LC_LOAD_DYLIB", reference))
    with pytest.raises(ValueError, match="unresolved.*library") as failure:
        macho.linked_runtime_files(executable)
    assert str(executable) in str(failure.value)
    assert reference in str(failure.value)


def test_runpath_suffix_cannot_discard_its_declared_root(native_images):
    add, _, _ = native_images
    outside = add("outside/not-a-runpath-member.dylib")
    executable = add(
        "bin/tool",
        ("LC_RPATH", "@loader_path/declared"),
        ("LC_LOAD_DYLIB", "@rpath/" + str(outside)),
    )
    with pytest.raises(ValueError):
        macho.linked_runtime_files(executable)


def test_shared_cache_libraries_need_no_on_disk_file_or_additional_grant(native_images):
    add, _, inspected = native_images
    executable = add(
        "bin/tool",
        ("LC_LOAD_DYLIB", "/usr/lib/aidashos-nonexistent-cache-fixture.dylib"),
        ("LC_LOAD_DYLIB", "/System/Library/Frameworks/NoFile.framework/NoFile"),
    )
    assert macho.linked_runtime_files(executable) == (executable,)
    assert inspected == [executable]


@pytest.mark.parametrize("cache_root", ("/usr/lib", "/System/Library"))
def test_uncertain_shared_cache_runpath_cannot_select_a_later_vendor_file(
    native_images, tmp_path: Path, cache_root: str
):
    add, _, inspected = native_images
    name = "aidashos-shared-cache-order-fixture.dylib"
    later = add("vendor/" + name)
    executable = add(
        "bin/tool",
        ("LC_RPATH", cache_root),
        ("LC_RPATH", str(tmp_path / "vendor")),
        ("LC_LOAD_DYLIB", "@rpath/" + name),
    )
    with pytest.raises(ValueError, match="unresolved system-cache runpath candidate"):
        macho.linked_runtime_files(executable)
    assert inspected == [executable]
    assert later not in inspected


def test_library_cycles_terminate_without_repeating_image_inspection(native_images):
    add, _, inspected = native_images
    first = add("lib/first.dylib", ("LC_LOAD_DYLIB", "@loader_path/second.dylib"))
    second = add("lib/second.dylib", ("LC_LOAD_DYLIB", "@loader_path/first.dylib"))
    executable = add("bin/tool", ("LC_LOAD_DYLIB", str(first)))
    assert set(macho.linked_runtime_files(executable)) == {executable, first, second}
    assert inspected == [executable, first, second]


@pytest.mark.parametrize(
    "output",
    (
        "",
        "not a native object\n",
        "Load command 0\n cmd LC_LOAD_DYLIB\n cmdsize 80\n",
        "Load command 0\n cmd LC_RPATH\n path missing-offset\n",
        "Load command 0\n cmd LC_LOAD_DYLIB\n cmd LC_RPATH\n path /tmp (offset 12)\n",
    ),
)
def test_malformed_native_metadata_is_refused(native_images, output: str):
    add, images, _ = native_images
    executable = add("bin/tool")
    images[executable] = output
    with pytest.raises(ValueError):
        macho.linked_runtime_files(executable)


def test_embedded_loader_environment_cannot_expand_the_declared_closure(native_images):
    add, _, _ = native_images
    executable = add("bin/tool", ("LC_DYLD_ENVIRONMENT", "DYLD_LIBRARY_PATH=/untrusted"))
    with pytest.raises(ValueError, match="overrides its loader environment"):
        macho.linked_runtime_files(executable)


def test_otool_failure_does_not_admit_a_native_image(tmp_path: Path, monkeypatch):
    executable = tmp_path / "not-native"
    executable.write_text("plain text")
    monkeypatch.setattr(
        macho.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 1, "", "not an object"),
    )
    with pytest.raises(ValueError, match="not readable native code"):
        macho.linked_runtime_files(executable)

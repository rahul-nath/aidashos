from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import public_snapshot_path  # noqa: F401  # puts the repository root on sys.path

from scripts.import_public_snapshot import (
    ActionKind,
    ConflictError,
    ManifestError,
    SafetyError,
    apply_plan,
    build_plan,
    load_manifest,
    render_plan,
)


class PublicSnapshotImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "private"
        self.destination = self.root / "public"
        self.source.mkdir()
        self.destination.mkdir()
        self.manifest = self.root / "public_import.toml"

    def write_manifest(self, entries: str) -> None:
        self.manifest.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'source_root = "private"',
                    'destination_root = "public"',
                    "",
                    entries.strip(),
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def test_dry_run_plans_only_allowlisted_files_and_apply_copies_them(self) -> None:
        (self.source / "README.md").write_text("source readme\n", encoding="utf-8")
        package = self.source / "package"
        package.mkdir()
        (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        cache = package / "__pycache__"
        cache.mkdir()
        (cache / "module.pyc").write_bytes(b"generated")
        (self.source / "private.txt").write_text("not allowlisted\n", encoding="utf-8")
        self.write_manifest(
            """
[[allow]]
path = "README.md"
kind = "file"

[[allow]]
path = "package"
kind = "directory"
"""
        )

        plan = build_plan(load_manifest(self.manifest))

        self.assertEqual(plan.count(ActionKind.CREATE), 2)
        self.assertEqual(plan.count(ActionKind.REPLACE), 0)
        self.assertEqual(len(plan.ignored_paths), 1)
        self.assertFalse((self.destination / "README.md").exists())

        apply_plan(plan)

        self.assertEqual(
            (self.destination / "README.md").read_text(encoding="utf-8"),
            "source readme\n",
        )
        self.assertEqual(
            (self.destination / "package" / "module.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )
        self.assertFalse((self.destination / "private.txt").exists())
        self.assertFalse((self.destination / "package" / "__pycache__").exists())

        second_plan = build_plan(load_manifest(self.manifest))
        self.assertEqual(second_plan.count(ActionKind.UNCHANGED), 2)

    def test_different_destination_requires_explicit_overwrite(self) -> None:
        (self.source / "README.md").write_text("private source\n", encoding="utf-8")
        (self.destination / "README.md").write_text("public edits\n", encoding="utf-8")
        self.write_manifest(
            """
[[allow]]
path = "README.md"
kind = "file"
"""
        )

        with self.assertRaisesRegex(ConflictError, "overwrite is false"):
            build_plan(load_manifest(self.manifest))

        self.write_manifest(
            """
[[allow]]
path = "README.md"
kind = "file"
overwrite = true
"""
        )
        plan = build_plan(load_manifest(self.manifest))
        self.assertEqual(plan.count(ActionKind.REPLACE), 1)

        apply_plan(plan)

        self.assertEqual(
            (self.destination / "README.md").read_text(encoding="utf-8"),
            "private source\n",
        )

    def test_apply_rejects_destination_changes_after_planning(self) -> None:
        (self.source / "new.txt").write_text("source\n", encoding="utf-8")
        self.write_manifest(
            """
[[allow]]
path = "new.txt"
kind = "file"
"""
        )
        create_plan = build_plan(load_manifest(self.manifest))
        (self.destination / "new.txt").write_text("concurrent edit\n", encoding="utf-8")

        with self.assertRaisesRegex(ConflictError, "appeared after planning"):
            apply_plan(create_plan)
        self.assertEqual(
            (self.destination / "new.txt").read_text(encoding="utf-8"),
            "concurrent edit\n",
        )

        (self.source / "existing.txt").write_text("source\n", encoding="utf-8")
        (self.destination / "existing.txt").write_text("old public\n", encoding="utf-8")
        self.write_manifest(
            """
[[allow]]
path = "existing.txt"
kind = "file"
overwrite = true
"""
        )
        replace_plan = build_plan(load_manifest(self.manifest))
        (self.destination / "existing.txt").write_text("new public\n", encoding="utf-8")

        with self.assertRaisesRegex(ConflictError, "changed after planning"):
            apply_plan(replace_plan)
        self.assertEqual(
            (self.destination / "existing.txt").read_text(encoding="utf-8"),
            "new public\n",
        )

    def test_manifest_rejects_traversal_and_overlapping_entries(self) -> None:
        (self.source / "package").mkdir()
        (self.source / "package" / "module.py").write_text("", encoding="utf-8")
        invalid_manifests = (
            """
[[allow]]
path = "../secret"
kind = "file"
""",
            """
[[allow]]
path = "package"
kind = "directory"

[[allow]]
path = "package/module.py"
kind = "file"
""",
        )

        for entries in invalid_manifests:
            with self.subTest(entries=entries):
                self.write_manifest(entries)
                with self.assertRaises(ManifestError):
                    load_manifest(self.manifest)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_allowed_directory_rejects_symlinks(self) -> None:
        package = self.source / "package"
        package.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("private\n", encoding="utf-8")
        os.symlink(outside, package / "escape.txt")
        self.write_manifest(
            """
[[allow]]
path = "package"
kind = "directory"
"""
        )

        with self.assertRaisesRegex(SafetyError, "Symlinks are forbidden"):
            build_plan(load_manifest(self.manifest))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_direct_file_rejects_symlinked_parent(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private\n", encoding="utf-8")
        os.symlink(outside, self.source / "linked")
        self.write_manifest(
            """
[[allow]]
path = "linked/private.txt"
kind = "file"
"""
        )

        with self.assertRaisesRegex(SafetyError, "Symlinks are forbidden"):
            build_plan(load_manifest(self.manifest))

    def test_allowed_directory_rejects_git_metadata_and_environment_files(self) -> None:
        package = self.source / "package"
        package.mkdir()
        self.write_manifest(
            """
[[allow]]
path = "package"
kind = "directory"
"""
        )

        git_directory = package / ".git"
        git_directory.mkdir()
        (git_directory / "config").write_text("private\n", encoding="utf-8")
        with self.assertRaisesRegex(SafetyError, "Version-control metadata"):
            build_plan(load_manifest(self.manifest))

        (git_directory / "config").unlink()
        git_directory.rmdir()
        (package / ".env.local").write_text("TOKEN=private\n", encoding="utf-8")
        with self.assertRaisesRegex(SafetyError, "Environment files are forbidden"):
            build_plan(load_manifest(self.manifest))

    def test_manifest_rejects_source_destination_overlap_and_kind_mismatch(self) -> None:
        (self.source / "README.md").write_text("source\n", encoding="utf-8")
        self.manifest.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'source_root = "private"',
                    'destination_root = "private/output"',
                    "",
                    "[[allow]]",
                    'path = "README.md"',
                    'kind = "file"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ManifestError, "must not overlap"):
            load_manifest(self.manifest)

        self.write_manifest(
            """
[[allow]]
path = "README.md"
kind = "directory"
"""
        )
        with self.assertRaisesRegex(ManifestError, "kind mismatch"):
            build_plan(load_manifest(self.manifest))

    def test_operator_state_is_withheld_and_the_destination_copy_survives(self) -> None:
        configs = self.source / "configs"
        configs.mkdir()
        (configs / "staffing.toml").write_text("tier = 'junior'\n", encoding="utf-8")
        (configs / "linked_projects.toml").write_text(
            'path = "~/ai_projects/private_business"\n', encoding="utf-8"
        )
        destination_configs = self.destination / "configs"
        destination_configs.mkdir()
        example = destination_configs / "linked_projects.toml"
        example.write_text('path = "~/ai_projects/example_web_app"\n', encoding="utf-8")

        self.write_manifest(
            """
[[allow]]
path = "configs"
kind = "directory"
overwrite = true
"""
        )
        plan = build_plan(load_manifest(self.manifest))

        withheld = [path.as_posix() for path in plan.withheld_paths]
        self.assertEqual(withheld, ["configs/linked_projects.toml"])
        planned = [action.relative_path.as_posix() for action in plan.actions]
        self.assertEqual(planned, ["configs/staffing.toml"])

        apply_plan(plan)

        self.assertEqual(
            example.read_text(encoding="utf-8"),
            'path = "~/ai_projects/example_web_app"\n',
        )

    def test_third_party_material_is_withheld_and_named_as_such(self) -> None:
        """The distillation stays home; the contract derived from it ships.

        `docs/design_principles.md` is distilled from course material and talk
        notes whose authors asked they not be shared, and it reached this
        repository's working tree once before anyone noticed the allowlist takes
        `docs` whole. It was never pushed. This is what makes that true a second
        time, and it asserts the reason text too: a report that called somebody
        else's material "operator state" would be filed under the wrong problem
        by the next person who read the log.
        """

        docs = self.source / "docs"
        docs.mkdir()
        (docs / "design_principles.md").write_text("Distilled from a course.\n", encoding="utf-8")
        (docs / "configuration.md").write_text("# Configuration\n", encoding="utf-8")

        self.write_manifest(
            """
[[allow]]
path = "docs"
kind = "directory"
overwrite = true
"""
        )
        plan = build_plan(load_manifest(self.manifest))

        withheld = [path.as_posix() for path in plan.withheld_paths]
        self.assertEqual(withheld, ["docs/design_principles.md"])
        planned = [action.relative_path.as_posix() for action in plan.actions]
        self.assertEqual(planned, ["docs/configuration.md"])
        self.assertIn(
            "WITHHELD  docs/design_principles.md (third-party material)",
            render_plan(plan, summary_only=True),
        )

        apply_plan(plan)

        self.assertFalse((self.destination / "docs" / "design_principles.md").exists())

    def test_withheld_paths_are_reported_even_in_summary_mode(self) -> None:
        configs = self.source / "configs"
        configs.mkdir()
        (configs / "linked_projects.toml").write_text("private = true\n", encoding="utf-8")
        self.write_manifest(
            """
[[allow]]
path = "configs"
kind = "directory"
overwrite = true
"""
        )

        rendered = render_plan(build_plan(load_manifest(self.manifest)), summary_only=True)

        self.assertIn("WITHHELD  configs/linked_projects.toml", rendered)
        self.assertIn("withheld=1", rendered)

    def test_operator_state_cannot_be_allowlisted_directly(self) -> None:
        configs = self.source / "configs"
        configs.mkdir()
        (configs / "linked_projects.toml").write_text("private = true\n", encoding="utf-8")
        self.write_manifest(
            """
[[allow]]
path = "configs/linked_projects.toml"
kind = "file"
overwrite = true
"""
        )

        with self.assertRaisesRegex(SafetyError, "Operator state cannot be allowlisted"):
            build_plan(load_manifest(self.manifest))

    def test_manifest_rejects_direct_git_metadata_and_environment_file_entries(self) -> None:
        invalid_entries = (
            """
[[allow]]
path = "package/.git/config"
kind = "file"
""",
            """
[[allow]]
path = "package/.env.local"
kind = "file"
""",
        )

        for entries in invalid_entries:
            with self.subTest(entries=entries):
                self.write_manifest(entries)
                with self.assertRaises(ManifestError):
                    load_manifest(self.manifest)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""End-to-end tests for the recipe manifest validator."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


VALIDATOR = Path(__file__).resolve().parents[1] / "scripts" / "validate-recipes.py"

VALID_MANIFEST = """\
schema_version = 1
id = "example-machine"
name = "Example Machine"
machine_config = "86box.cfg"
global_config = "86box_global.cfg"

[launchers]
macos = "launch-macos.command"

[[hard_disks]]
config_id = "hdd_01"
path = "disks/example.hdd"
bytes = 2111864832
sectors = 63
heads = 64
cylinders = 1023
bus = "ide"
channel = "0:0"

[requirements]
rom_subdirectories = ["machines"]
asset_files = ["sounds/hdd/hdd_audio_profiles.cfg"]
"""

VALID_CONFIG = """\
[Machine]
machine = example

[Hard disks]
hdd_01_fn = disks/example.hdd
hdd_01_ide_channel = 0:0
hdd_01_parameters = 63, 64, 1023, 0, ide
"""


class ValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "repository"
        self.machine = self.root / "machines" / "example-machine"
        (self.machine / "disks").mkdir(parents=True)
        self.manifest = self.machine / "recipe.toml"
        self.config = self.machine / "86box.cfg"
        self.gitignore = self.root / ".gitignore"

        self.manifest.write_text(VALID_MANIFEST, encoding="utf-8")
        self.config.write_text(VALID_CONFIG, encoding="utf-8")
        (self.machine / "86box_global.cfg").write_text(
            "[Emulator]\nconfirm_exit = 1\n", encoding="utf-8"
        )
        (self.machine / "README.md").write_text(
            "# Example Machine\n", encoding="utf-8"
        )
        (self.machine / "launch-macos.command").write_text(
            "#!/bin/zsh\nexit 0\n", encoding="utf-8"
        )
        (self.machine / "disks" / "README.md").write_text(
            "Runtime disks are private.\n", encoding="utf-8"
        )
        self.gitignore.write_text(
            "**/disks/*\n!**/disks/README.md\n", encoding="utf-8"
        )

        self.run_git("init", "--quiet")
        self.run_git("add", "--all")

    def run_git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stage(self, *paths: str) -> None:
        self.run_git("add", "--", *paths)

    def run_validator(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(VALIDATOR), "--root", str(self.root)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def assert_valid(self) -> None:
        result = self.run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Validated 1 recipe manifest(s).\n")

    def assert_invalid(self, field: str, message: str | None = None) -> None:
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        prefix = f"machines/example-machine/recipe.toml: {field}:"
        self.assertIn(prefix, result.stderr)
        if message is not None:
            self.assertIn(message, result.stderr)

    def replace_manifest(self, old: str, new: str, *, stage: bool = True) -> None:
        contents = self.manifest.read_text(encoding="utf-8")
        self.assertIn(old, contents)
        self.manifest.write_text(contents.replace(old, new, 1), encoding="utf-8")
        if stage:
            self.stage("machines/example-machine/recipe.toml")

    def replace_config(self, old: str, new: str, *, stage: bool = True) -> None:
        contents = self.config.read_text(encoding="utf-8")
        self.assertIn(old, contents)
        self.config.write_text(contents.replace(old, new, 1), encoding="utf-8")
        if stage:
            self.stage("machines/example-machine/86box.cfg")

    def add_second_disk(self, channel: str = "0:1") -> None:
        second_disk = f"""\
[[hard_disks]]
config_id = "hdd_02"
path = "disks/second.hdd"
bytes = 51609600
sectors = 63
heads = 16
cylinders = 100
bus = "ide"
channel = "{channel}"

"""
        self.replace_manifest("[requirements]\n", second_disk + "[requirements]\n")
        with self.config.open("a", encoding="utf-8") as config_file:
            config_file.write(
                "hdd_02_fn = disks/second.hdd\n"
                f"hdd_02_ide_channel = {channel}\n"
                "hdd_02_parameters = 63, 16, 100, 0, ide\n"
            )
        self.stage("machines/example-machine/86box.cfg")

    def test_valid_recipe(self) -> None:
        self.assert_valid()

    def test_missing_manifest(self) -> None:
        self.manifest.unlink()
        self.assert_invalid("manifest", "file not found")

    def test_index_only_machine_directory_is_rejected(self) -> None:
        index_only = self.root / "machines" / "index-only"
        index_only.mkdir()
        marker = index_only / "README.md"
        marker.write_text("# Index-only machine\n", encoding="utf-8")
        self.stage("machines/index-only/README.md")
        marker.unlink()
        index_only.rmdir()

        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "machines/index-only/recipe.toml: machines: "
            "worktree and Git index machine IDs differ (index-only: index-only)",
            result.stderr,
        )

    def test_manifest_must_be_tracked(self) -> None:
        self.run_git("rm", "--cached", "--quiet", "machines/example-machine/recipe.toml")
        self.assert_invalid("manifest", "is not tracked by Git")

    def test_staged_invalid_manifest_cannot_be_masked_by_valid_worktree(self) -> None:
        self.manifest.write_text("schema_version = [\n", encoding="utf-8")
        self.stage("machines/example-machine/recipe.toml")
        self.manifest.write_text(VALID_MANIFEST, encoding="utf-8")
        self.assert_invalid("manifest", "must match its indexed Git version")

    def test_invalid_toml(self) -> None:
        self.manifest.write_text("schema_version = [\n", encoding="utf-8")
        self.stage("machines/example-machine/recipe.toml")
        self.assert_invalid("manifest", "invalid TOML")

    def test_missing_field(self) -> None:
        self.replace_manifest('name = "Example Machine"\n', "")
        self.assert_invalid("manifest", "missing required field 'name'")

    def test_unknown_top_level_field(self) -> None:
        self.replace_manifest("schema_version = 1\n", "schema_version = 1\nsurprise = true\n")
        self.assert_invalid("manifest", "unknown field 'surprise'")

    def test_unknown_nested_field(self) -> None:
        self.replace_manifest('channel = "0:0"\n', 'channel = "0:0"\nsurprise = true\n')
        self.assert_invalid("hard_disks[0]", "unknown field 'surprise'")

    def test_unsupported_schema_version(self) -> None:
        self.replace_manifest("schema_version = 1", "schema_version = 2")
        self.assert_invalid("schema_version", "unsupported version 2")

    def test_boolean_is_not_an_integer(self) -> None:
        self.replace_manifest("bytes = 2111864832", "bytes = true")
        self.assert_invalid("hard_disks[0].bytes", "must be an integer")

    def test_string_must_not_contain_ascii_control(self) -> None:
        self.replace_manifest(
            'name = "Example Machine"', 'name = "Example\\tMachine"'
        )
        self.assert_invalid("name", "ASCII control characters")

    def test_path_must_not_contain_ascii_control(self) -> None:
        self.replace_manifest(
            'path = "disks/example.hdd"', 'path = "disks/example\\u007f.hdd"'
        )
        self.assert_invalid("hard_disks[0].path", "ASCII control characters")

    def test_wrong_array_item_type(self) -> None:
        self.replace_manifest(
            'rom_subdirectories = ["machines"]', "rom_subdirectories = [1]"
        )
        self.assert_invalid("requirements.rom_subdirectories[0]")

    def test_id_must_match_directory(self) -> None:
        self.replace_manifest('id = "example-machine"', 'id = "different-machine"')
        self.assert_invalid("id", "must match the machine directory name")

    def test_absolute_tracked_path(self) -> None:
        self.replace_manifest(
            'machine_config = "86box.cfg"', 'machine_config = "/tmp/86box.cfg"'
        )
        self.assert_invalid("machine_config", "must be relative")

    def test_windows_absolute_path(self) -> None:
        self.replace_manifest(
            'machine_config = "86box.cfg"', 'machine_config = "C:/86box.cfg"'
        )
        self.assert_invalid("machine_config", "must be relative")

    def test_runtime_path_traversal(self) -> None:
        self.replace_manifest(
            'path = "disks/example.hdd"', 'path = "../example.hdd"'
        )
        self.assert_invalid("hard_disks[0].path", "without '.' or '..'")

    def test_non_normalized_path(self) -> None:
        self.replace_manifest(
            'machine_config = "86box.cfg"', 'machine_config = "docs//../86box.cfg"'
        )
        self.assert_invalid("machine_config", "normalized relative path")

    def test_missing_referenced_file(self) -> None:
        self.config.unlink()
        self.assert_invalid("machine_config", "referenced file does not exist")

    def test_machine_readme_must_be_tracked(self) -> None:
        self.run_git("rm", "--cached", "--quiet", "machines/example-machine/README.md")
        self.assert_invalid("readme", "is not tracked by Git")

    def test_runtime_parent_readme_must_exist(self) -> None:
        (self.machine / "disks" / "README.md").unlink()
        self.assert_invalid("hard_disks[0].path", "referenced file does not exist")

    def test_runtime_parent_readme_must_be_tracked(self) -> None:
        self.run_git(
            "rm",
            "--cached",
            "--quiet",
            "machines/example-machine/disks/README.md",
        )
        self.assert_invalid("hard_disks[0].path", "is not tracked by Git")

    def test_referenced_file_must_be_tracked(self) -> None:
        untracked = self.machine / "untracked.cfg"
        untracked.write_text(VALID_CONFIG, encoding="utf-8")
        self.replace_manifest('machine_config = "86box.cfg"', 'machine_config = "untracked.cfg"')
        self.assert_invalid("machine_config", "is not tracked by Git")

    def test_staged_invalid_config_cannot_be_masked_by_valid_worktree(self) -> None:
        self.config.write_text("[Hard disks\n", encoding="utf-8")
        self.stage("machines/example-machine/86box.cfg")
        self.config.write_text(VALID_CONFIG, encoding="utf-8")
        self.assert_invalid("machine_config", "must match its indexed Git version")

    def test_referenced_file_symlink_is_rejected(self) -> None:
        outside = self.root / "outside.cfg"
        outside.write_text(VALID_CONFIG, encoding="utf-8")
        self.config.unlink()
        self.config.symlink_to(outside)
        self.assert_invalid("machine_config", "must not traverse symlink")

    def test_runtime_parent_symlink_is_rejected(self) -> None:
        outside = self.root / "outside-disks"
        outside.mkdir()
        (self.machine / "disks" / "README.md").unlink()
        (self.machine / "disks").rmdir()
        (self.machine / "disks").symlink_to(outside, target_is_directory=True)
        self.assert_invalid("hard_disks[0].path", "must not traverse symlink")

    def test_duplicate_ini_section(self) -> None:
        with self.config.open("a", encoding="utf-8") as config_file:
            config_file.write("\n[Hard disks]\n")
        self.stage("machines/example-machine/86box.cfg")
        self.assert_invalid("machine_config", "invalid INI")

    def test_duplicate_ini_key(self) -> None:
        with self.config.open("a", encoding="utf-8") as config_file:
            config_file.write("hdd_01_fn = disks/example.hdd\n")
        self.stage("machines/example-machine/86box.cfg")
        self.assert_invalid("machine_config", "invalid INI")

    def test_default_section_inheritance_is_rejected(self) -> None:
        disk_settings = (
            "hdd_01_fn = disks/example.hdd\n"
            "hdd_01_ide_channel = 0:0\n"
            "hdd_01_parameters = 63, 64, 1023, 0, ide\n"
        )
        inherited_config = VALID_CONFIG.replace(disk_settings, "").replace(
            "[Machine]\n", f"[DEFAULT]\n{disk_settings}\n[Machine]\n"
        )
        self.config.write_text(inherited_config, encoding="utf-8")
        self.stage("machines/example-machine/86box.cfg")
        self.assert_invalid("machine_config", "must not define values in [DEFAULT]")

    def test_disk_size_must_match_geometry(self) -> None:
        self.replace_manifest("bytes = 2111864832", "bytes = 2111864831")
        self.assert_invalid("hard_disks[0].bytes", "2111864832")

    def test_disk_path_must_match_config(self) -> None:
        self.replace_config("disks/example.hdd", "disks/different.hdd")
        self.assert_invalid("hard_disks[0].path", "does not match hdd_01_fn")

    def test_disk_geometry_must_match_config(self) -> None:
        self.replace_config(
            "63, 64, 1023, 0, ide", "62, 64, 1023, 0, ide"
        )
        self.assert_invalid("hard_disks[0]", "CHS geometry")

    def test_disk_bus_must_match_config(self) -> None:
        self.replace_config(
            "63, 64, 1023, 0, ide", "63, 64, 1023, 0, scsi"
        )
        self.assert_invalid("hard_disks[0].bus", "does not match")

    def test_disk_channel_must_match_config(self) -> None:
        self.replace_config("hdd_01_ide_channel = 0:0", "hdd_01_ide_channel = 1:0")
        self.assert_invalid("hard_disks[0].channel", "does not match")

    def test_two_disks_are_valid(self) -> None:
        self.add_second_disk()
        self.assert_valid()

    def test_undeclared_config_disk_is_rejected(self) -> None:
        with self.config.open("a", encoding="utf-8") as config_file:
            config_file.write(
                "hdd_02_fn = disks/second.hdd\n"
                "hdd_02_ide_channel = 0:1\n"
                "hdd_02_parameters = 63, 16, 100, 0, ide\n"
            )
        self.stage("machines/example-machine/86box.cfg")
        self.assert_invalid("hard_disks", "undeclared machine-config IDs: hdd_02")

    def test_duplicate_disk_channel_is_rejected(self) -> None:
        self.add_second_disk(channel="0:0")
        self.assert_invalid("hard_disks[1].channel", "must be unique")

    def test_runtime_path_must_be_ignored(self) -> None:
        self.gitignore.write_text("", encoding="utf-8")
        self.stage(".gitignore")
        self.assert_invalid("hard_disks[0].path", "not ignored by Git")

    def test_negated_runtime_path_is_not_treated_as_ignored(self) -> None:
        self.gitignore.write_text(
            "**/disks/*\n"
            "!**/disks/README.md\n"
            "!**/disks/example.hdd\n",
            encoding="utf-8",
        )
        self.stage(".gitignore")
        self.assert_invalid("hard_disks[0].path", "not ignored by Git")

    def test_info_exclude_does_not_satisfy_runtime_ignore_contract(self) -> None:
        self.gitignore.write_text("", encoding="utf-8")
        self.stage(".gitignore")
        (self.root / ".git" / "info" / "exclude").write_text(
            "**/disks/*\n", encoding="utf-8"
        )
        self.assert_invalid(
            "hard_disks[0].path", "tracked repository .gitignore"
        )

    def test_unstaged_gitignore_rule_does_not_satisfy_contract(self) -> None:
        self.gitignore.write_text("", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.gitignore.write_text(
            "**/disks/*\n!**/disks/README.md\n", encoding="utf-8"
        )
        self.assert_invalid(
            "hard_disks[0].path", "must match its indexed Git version"
        )

    def test_assume_unchanged_does_not_hide_gitignore_drift(self) -> None:
        self.run_git("update-index", "--assume-unchanged", ".gitignore")
        self.gitignore.write_text("**/*.hdd\n", encoding="utf-8")
        self.assert_invalid(
            "hard_disks[0].path", "must match its indexed Git version"
        )

    def test_skip_worktree_does_not_hide_gitignore_drift(self) -> None:
        self.run_git("update-index", "--skip-worktree", ".gitignore")
        self.gitignore.write_text("**/*.hdd\n", encoding="utf-8")
        self.assert_invalid(
            "hard_disks[0].path", "must match its indexed Git version"
        )

    def test_global_excludes_do_not_satisfy_runtime_ignore_contract(self) -> None:
        self.gitignore.write_text("", encoding="utf-8")
        self.stage(".gitignore")
        global_excludes = Path(self.temporary_directory.name) / "global-excludes"
        global_excludes.write_text("**/disks/*\n", encoding="utf-8")
        self.run_git("config", "core.excludesFile", str(global_excludes))
        self.assert_invalid(
            "hard_disks[0].path", "tracked repository .gitignore"
        )

    def test_non_ancestor_repository_gitignore_cannot_act_as_global_excludes(
        self,
    ) -> None:
        self.gitignore.write_text("", encoding="utf-8")
        self.stage(".gitignore")
        policy_dir = self.root / "policy"
        policy_dir.mkdir()
        policy_ignore = policy_dir / ".gitignore"
        policy_ignore.write_text("**/disks/*\n", encoding="utf-8")
        self.stage("policy/.gitignore")
        self.run_git("config", "core.excludesFile", str(policy_ignore))
        self.assert_invalid(
            "hard_disks[0].path", "ancestor repository .gitignore"
        )


if __name__ == "__main__":
    unittest.main()

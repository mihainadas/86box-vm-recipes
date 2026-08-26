#!/usr/bin/env python3
"""Hermetic runtime-contract tests for the macOS 86Box launcher."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_MACHINE = REPOSITORY_ROOT / "machines" / "1995-dream-486"
SOURCE_MANIFEST = SOURCE_MACHINE / "recipe.toml"
SOURCE_MOCK = REPOSITORY_ROOT / "tests" / "fixtures" / "mock-86box.py"
LAUNCH_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class LauncherResult:
    pid: int
    returncode: int
    stdout: str
    stderr: str


class MacOSLauncherContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="86box launcher contract "
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.test_root = Path(self.temporary_directory.name).resolve()
        self.machine_dir = self.test_root / "recipe copy with spaces"
        self.machine_dir.mkdir()

        with SOURCE_MANIFEST.open("rb") as manifest_file:
            self.manifest = tomllib.load(manifest_file)

        tracked_recipe_files = {
            "recipe.toml",
            self.manifest["machine_config"],
            self.manifest["global_config"],
            self.manifest["launchers"]["macos"],
        }
        for relative_path in tracked_recipe_files:
            source = SOURCE_MACHINE / relative_path
            destination = self.machine_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        self.launcher = self.machine_dir / self.manifest["launchers"]["macos"]
        self.rom_root = self.test_root / "synthetic ROM checkout with spaces"
        self.asset_root = self.test_root / "synthetic asset checkout with spaces"
        self.emulator = self.test_root / "fake tools with spaces" / "86Box fake"
        self.emulator.parent.mkdir()
        shutil.copy2(SOURCE_MOCK, self.emulator)
        self.emulator.chmod(0o700)
        self.capture_path = self.test_root / "recorded arguments.jsonl"
        self.working_directory = self.test_root / "unrelated working directory"
        self.working_directory.mkdir()

        for requirement in self.manifest["requirements"]["rom_subdirectories"]:
            self.path_below(self.rom_root, requirement).mkdir(parents=True, exist_ok=True)
        for requirement in self.manifest["requirements"]["asset_files"]:
            marker = self.path_below(self.asset_root, requirement)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("synthetic asset marker\n", encoding="utf-8")
        for disk in self.manifest["hard_disks"]:
            disk_path = self.path_below(self.machine_dir, disk["path"])
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            with disk_path.open("xb") as disk_file:
                disk_file.truncate(disk["bytes"])

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "EIGHTYSIXBOX_EXECUTABLE": str(self.emulator),
                "EIGHTYSIXBOX_ROM_PATH": str(self.rom_root),
                "EIGHTYSIXBOX_ASSET_PATH": str(self.asset_root),
                "MOCK_86BOX_CAPTURE": str(self.capture_path),
                "MOCK_86BOX_EXIT": "0",
            }
        )

    def path_below(self, root: Path, manifest_path: str) -> Path:
        relative_path = PurePosixPath(manifest_path)
        candidate = root.joinpath(*relative_path.parts)
        candidate.resolve(strict=False).relative_to(self.test_root)
        return candidate

    def run_launcher(
        self, environment_updates: dict[str, str] | None = None
    ) -> LauncherResult:
        environment = self.environment.copy()
        if environment_updates:
            environment.update(environment_updates)
        process = subprocess.Popen(
            [str(self.launcher)],
            cwd=self.working_directory,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=LAUNCH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            self.fail(
                f"launcher exceeded {LAUNCH_TIMEOUT_SECONDS} seconds and was terminated"
            )
        return LauncherResult(process.pid, process.returncode, stdout, stderr)

    def recorded_invocations(self) -> list[dict[str, object]]:
        if not self.capture_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.capture_path.read_text(encoding="utf-8").splitlines()
        ]

    def assert_preflight_failure(
        self,
        expected_message: str,
        environment_updates: dict[str, str] | None = None,
    ) -> None:
        result = self.run_launcher(environment_updates)
        self.assertEqual(result.returncode, 1, result)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, expected_message + "\n")
        self.assertEqual(self.recorded_invocations(), [])

    def restore_disk(self, disk: dict[str, object]) -> Path:
        disk_path = self.path_below(self.machine_dir, str(disk["path"]))
        with disk_path.open("wb") as disk_file:
            disk_file.truncate(int(disk["bytes"]))
        return disk_path

    def test_exact_ordered_arguments_and_single_invocation(self) -> None:
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(
            self.recorded_invocations(),
            [
                {
                    "pid": result.pid,
                    "argv": [
                        "--vmpath",
                        str(self.machine_dir),
                        "--global",
                        str(self.machine_dir / self.manifest["global_config"]),
                        "--rompath",
                        str(self.rom_root),
                        "--assetpath",
                        str(self.asset_root),
                        "--vmname",
                        self.manifest["name"],
                    ],
                }
            ],
        )

    def test_emulator_exit_status_is_propagated(self) -> None:
        result = self.run_launcher({"MOCK_86BOX_EXIT": "23"})
        self.assertEqual(result.returncode, 23, result)
        invocations = self.recorded_invocations()
        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0]["pid"], result.pid)

    def test_sparse_disks_have_manifest_size_and_small_allocation(self) -> None:
        for disk in self.manifest["hard_disks"]:
            with self.subTest(disk=disk["config_id"]):
                disk_path = self.path_below(self.machine_dir, disk["path"])
                disk_stat = disk_path.stat()
                self.assertEqual(disk_stat.st_size, disk["bytes"])
                self.assertLess(disk_stat.st_blocks * 512, 16 * 1024 * 1024)

    def test_disk_symlink_uses_target_size(self) -> None:
        disk = self.manifest["hard_disks"][0]
        disk_path = self.path_below(self.machine_dir, disk["path"])
        linked_disk = self.test_root / "synthetic linked disk with spaces.hdd"
        with linked_disk.open("xb") as disk_file:
            disk_file.truncate(disk["bytes"])
        disk_path.unlink()
        disk_path.symlink_to(linked_disk)

        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(len(self.recorded_invocations()), 1)

    def test_machine_and_global_configs_are_required(self) -> None:
        config_cases = [
            (
                self.machine_dir / "86box.cfg",
                "Create 86box.cfg as described in README.md before launching.",
            ),
            (
                self.machine_dir / self.manifest["global_config"],
                f"Create {self.manifest['global_config']} as described in README.md before launching.",
            ),
        ]
        for config_path, message in config_cases:
            with self.subTest(config=config_path.name):
                original = config_path.read_bytes()
                config_path.unlink()
                self.assert_preflight_failure(message)
                config_path.write_bytes(original)

    def test_machine_and_global_configs_must_be_regular_files(self) -> None:
        config_cases = [
            (
                self.machine_dir / "86box.cfg",
                "Create 86box.cfg as described in README.md before launching.",
            ),
            (
                self.machine_dir / self.manifest["global_config"],
                f"Create {self.manifest['global_config']} as described in README.md before launching.",
            ),
        ]
        for config_path, message in config_cases:
            with self.subTest(config=config_path.name):
                original = config_path.read_bytes()
                config_path.unlink()
                config_path.mkdir()
                self.assert_preflight_failure(message)
                config_path.rmdir()
                config_path.write_bytes(original)

    def test_broken_config_symlinks_are_rejected(self) -> None:
        config_cases = [
            (
                self.machine_dir / "86box.cfg",
                "Create 86box.cfg as described in README.md before launching.",
            ),
            (
                self.machine_dir / self.manifest["global_config"],
                f"Create {self.manifest['global_config']} as described in README.md before launching.",
            ),
        ]
        for config_path, message in config_cases:
            with self.subTest(config=config_path.name):
                original = config_path.read_bytes()
                config_path.unlink()
                config_path.symlink_to(self.test_root / "missing config target")
                self.assert_preflight_failure(message)
                config_path.unlink()
                config_path.write_bytes(original)

    def test_machine_and_global_configs_must_be_readable(self) -> None:
        config_cases = [
            (
                self.machine_dir / "86box.cfg",
                "Create 86box.cfg as described in README.md before launching.",
            ),
            (
                self.machine_dir / self.manifest["global_config"],
                f"Create {self.manifest['global_config']} as described in README.md before launching.",
            ),
        ]
        for config_path, message in config_cases:
            with self.subTest(config=config_path.name):
                config_path.chmod(0o000)
                try:
                    self.assert_preflight_failure(message)
                finally:
                    config_path.chmod(0o600)

    def test_runtime_disk_paths_are_ignored(self) -> None:
        for disk in self.manifest["hard_disks"]:
            repository_path = (
                Path("machines") / self.manifest["id"] / disk["path"]
            ).as_posix()
            with self.subTest(path=repository_path):
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(REPOSITORY_ROOT),
                        "check-ignore",
                        "--quiet",
                        "--",
                        repository_path,
                    ],
                    check=False,
                )
                self.assertEqual(result.returncode, 0)

    def test_missing_emulator_fails_before_invocation(self) -> None:
        missing = self.test_root / "missing emulator"
        self.assert_preflight_failure(
            "Set EIGHTYSIXBOX_EXECUTABLE to the 86Box executable.",
            {"EIGHTYSIXBOX_EXECUTABLE": str(missing)},
        )

    def test_non_executable_emulator_fails_before_invocation(self) -> None:
        self.emulator.chmod(0o600)
        self.assert_preflight_failure(
            "Set EIGHTYSIXBOX_EXECUTABLE to the 86Box executable."
        )

    def test_emulator_directory_is_rejected_as_malformed(self) -> None:
        malformed = self.test_root / "emulator directory"
        malformed.mkdir()
        self.assert_preflight_failure(
            "Set EIGHTYSIXBOX_EXECUTABLE to the 86Box executable.",
            {"EIGHTYSIXBOX_EXECUTABLE": str(malformed)},
        )

    def test_every_rom_requirement_is_required(self) -> None:
        message = "Set EIGHTYSIXBOX_ROM_PATH to the official 86Box ROM checkout."
        for requirement in self.manifest["requirements"]["rom_subdirectories"]:
            requirement_path = self.path_below(self.rom_root, requirement)
            with self.subTest(requirement=requirement):
                requirement_path.rmdir()
                self.assert_preflight_failure(message)
                requirement_path.mkdir(parents=True)

    def test_rom_requirement_must_be_a_directory(self) -> None:
        message = "Set EIGHTYSIXBOX_ROM_PATH to the official 86Box ROM checkout."
        for requirement in self.manifest["requirements"]["rom_subdirectories"]:
            requirement_path = self.path_below(self.rom_root, requirement)
            with self.subTest(requirement=requirement):
                requirement_path.rmdir()
                requirement_path.write_text("synthetic file\n", encoding="utf-8")
                self.assert_preflight_failure(message)
                requirement_path.unlink()
                requirement_path.mkdir(parents=True)

    def test_every_asset_requirement_is_required(self) -> None:
        message = "Set EIGHTYSIXBOX_ASSET_PATH to the official 86Box asset checkout."
        for requirement in self.manifest["requirements"]["asset_files"]:
            requirement_path = self.path_below(self.asset_root, requirement)
            with self.subTest(requirement=requirement):
                requirement_path.unlink()
                self.assert_preflight_failure(message)
                requirement_path.write_text("synthetic asset marker\n", encoding="utf-8")

    def test_asset_requirement_must_be_a_file(self) -> None:
        message = "Set EIGHTYSIXBOX_ASSET_PATH to the official 86Box asset checkout."
        for requirement in self.manifest["requirements"]["asset_files"]:
            requirement_path = self.path_below(self.asset_root, requirement)
            with self.subTest(requirement=requirement):
                requirement_path.unlink()
                requirement_path.mkdir()
                self.assert_preflight_failure(message)
                requirement_path.rmdir()
                requirement_path.write_text("synthetic asset marker\n", encoding="utf-8")

    def test_malformed_rom_and_asset_roots_fail(self) -> None:
        malformed_rom = self.test_root / "ROM root is a file"
        malformed_rom.write_text("synthetic\n", encoding="utf-8")
        self.assert_preflight_failure(
            "Set EIGHTYSIXBOX_ROM_PATH to the official 86Box ROM checkout.",
            {"EIGHTYSIXBOX_ROM_PATH": str(malformed_rom)},
        )

        malformed_asset = self.test_root / "asset root is a file"
        malformed_asset.write_text("synthetic\n", encoding="utf-8")
        self.assert_preflight_failure(
            "Set EIGHTYSIXBOX_ASSET_PATH to the official 86Box asset checkout.",
            {"EIGHTYSIXBOX_ASSET_PATH": str(malformed_asset)},
        )

    def test_every_manifest_disk_is_required(self) -> None:
        for disk in self.manifest["hard_disks"]:
            disk_path = self.path_below(self.machine_dir, disk["path"])
            with self.subTest(disk=disk["config_id"]):
                disk_path.unlink()
                self.assert_preflight_failure(
                    f"Create {disk['path']} as described in README.md before launching."
                )
                self.restore_disk(disk)

    def test_every_manifest_disk_must_have_exact_size(self) -> None:
        for disk in self.manifest["hard_disks"]:
            disk_path = self.path_below(self.machine_dir, disk["path"])
            with self.subTest(disk=disk["config_id"]):
                with disk_path.open("wb") as disk_file:
                    disk_file.truncate(disk["bytes"] - 1)
                self.assert_preflight_failure(
                    f"{disk['path']} must contain exactly {disk['bytes']} bytes as declared in recipe.toml."
                )
                self.restore_disk(disk)


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        raise SystemExit("macos-launcher-contract.py requires Python 3.11 or newer")
    unittest.main()

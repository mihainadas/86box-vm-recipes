from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "private-acceptance.py"
SPEC = importlib.util.spec_from_file_location("private_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HARNESS
SPEC.loader.exec_module(HARNESS)


class PrivateAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="private acceptance tests ")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()

    def assert_emptied_workspace(self, path: Path) -> None:
        self.assertTrue(path.is_dir())
        self.assertEqual(list(path.iterdir()), [])
        path.rmdir()

    def test_production_trust_record_is_exact_and_not_cli_overridable(self) -> None:
        trust = HARNESS.PRODUCTION_TRUST
        self.assertEqual(trust.repository, "86Box/86Box")
        self.assertEqual((trust.tag, trust.build), ("v6.0", 9001))
        self.assertEqual(trust.source_commit, "4fef696a4eead1d55a28d6ac0e5bd2864e5454da")
        self.assertEqual(trust.archive_name, "86Box-macOS-x86_64+arm64-b9001.zip")
        self.assertEqual(trust.archive_size, 124_110_592)
        self.assertEqual(trust.archive_sha256, "fc66fc97225012af20145ae04193911bbf689fc75f89590774a904483140a5a9")
        self.assertEqual(trust.executable_path, "86Box.app/Contents/MacOS/86Box")
        self.assertEqual(trust.executable_sha256, "764750187e52f643dc4d6e61ecaf517c64c3cd2e9225934eb1172aa733b3269b")
        self.assertEqual(trust.macho_arches, frozenset({"x86_64", "arm64"}))
        self.assertEqual(len(trust.symlinks), 42)
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            HARNESS.parse_args(["--private-manifest", "x", "--archive-sha256", "0" * 64])

    def test_unexpected_cli_failure_suppresses_private_details(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(HARNESS, "run", side_effect=OSError("/private/secret/media.iso")),
            mock.patch("sys.stderr", new=stderr),
        ):
            result = HARNESS.main(["--private-manifest", "/private/secret/manifest.toml"])
        self.assertEqual(result, 1)
        self.assertNotIn("/private/secret", stderr.getvalue())

    def write_manifest(self, **updates: str) -> Path:
        values = {
            "archive": self.root / "official.zip",
            "roms": self.root / "roms",
            "assets": self.root / "assets",
            "installed_hdd": self.root / "installed.hdd",
            "startup_floppy": self.root / "startup.img",
            "install_iso": self.root / "install.iso",
        }
        values.update({key: Path(value) for key, value in updates.items()})
        manifest = self.root / "private.local.toml"
        lines = ["schema_version = 1"] + [f'{key} = "{value}"' for key, value in values.items()]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest.chmod(0o600)
        return manifest

    def make_archive(self, members: list[tuple[str, bytes, int]] | None = None) -> tuple[Path, object]:
        executable = b"#!/bin/sh\nexit 0\n"
        if members is None:
            members = [("86Box.app/Contents/MacOS/86Box", executable, stat.S_IFREG | 0o755)]
        archive_path = self.root / "official.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value, mode in members:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, value)
        archive_bytes = archive_path.read_bytes()
        total = sum(len(value) for _, value, _ in members)
        executable_value = next((value for name, value, _ in members if name == "86Box.app/Contents/MacOS/86Box"), executable)
        links = {
            name: value.decode("utf-8")
            for name, value, mode in members
            if stat.S_ISLNK(mode)
        }
        trust = HARNESS.TrustRecord(
            repository="test/repository",
            tag="v0-test",
            build=1,
            source_commit="0" * 40,
            archive_name=archive_path.name,
            archive_size=len(archive_bytes),
            archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            archive_entries=len(members),
            archive_uncompressed=total,
            executable_path="86Box.app/Contents/MacOS/86Box",
            executable_sha256=hashlib.sha256(executable_value).hexdigest(),
            macho_arches=frozenset(),
            symlinks=links,
        )
        return archive_path, trust

    def test_private_manifest_requires_exact_mode(self) -> None:
        manifest = self.write_manifest()
        manifest.chmod(0o644)
        with self.assertRaisesRegex(HARNESS.HarnessError, "mode 0600"):
            HARNESS.load_private_manifest(manifest)

    def test_private_manifest_rejects_symlink_and_fifo(self) -> None:
        target = self.write_manifest()
        linked = self.root / "linked.toml"
        linked.symlink_to(target)
        with self.assertRaisesRegex(HARNESS.HarnessError, "nonsymlink"):
            HARNESS.load_private_manifest(linked)
        fifo = self.root / "manifest.fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(HARNESS.HarnessError, "nonsymlink"):
            HARNESS.load_private_manifest(fifo)

    def test_private_manifest_rejects_unknown_field_and_relative_path(self) -> None:
        manifest = self.write_manifest()
        manifest.write_text(manifest.read_text(encoding="utf-8") + 'product_key = "forbidden"\n', encoding="utf-8")
        with self.assertRaisesRegex(HARNESS.HarnessError, "schema exactly"):
            HARNESS.load_private_manifest(manifest)
        manifest = self.write_manifest(archive="relative.zip")
        with self.assertRaisesRegex(HARNESS.HarnessError, "absolute"):
            HARNESS.load_private_manifest(manifest)

    def test_ci_launch_refuses_before_private_manifest_read(self) -> None:
        options = HARNESS.Options(self.root / "does-not-exist", launch=True, source_vm_stopped=True)
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            with self.assertRaisesRegex(HARNESS.HarnessError, "disabled in CI"):
                HARNESS.run(options, hooks=HARNESS.Hooks(validate_repository=False))

    def test_launch_requires_stopped_vm_confirmation(self) -> None:
        options = HARNESS.Options(self.root / "does-not-exist", launch=True)
        with mock.patch.dict(os.environ, {name: "" for name in HARNESS.CI_MARKERS}, clear=False):
            with self.assertRaisesRegex(HARNESS.HarnessError, "source-vm-stopped"):
                HARNESS.run(options, hooks=HARNESS.Hooks(validate_repository=False, platform_name="Darwin"))

    def test_config_removes_network_and_adds_disposable_media(self) -> None:
        source = b"[General]\nfoo = 1\n\n[Network]\nnet_01_card = pcnetpci\nnet_01_net_type = slirp\n\n[Floppy and CD-ROM drives]\nfdd_01_type = 35_2hd\n"
        rendered = HARNESS.sanitize_machine_config(source).decode("utf-8")
        self.assertNotIn("[Network]", rendered)
        self.assertNotIn("pcnetpci", rendered)
        self.assertIn("fdd_01_fn = media/startup-floppy.img", rendered)
        self.assertIn("cdrom_01_image_path = media/install.iso", rendered)

    def test_config_rejects_defaults_duplicates_and_case_confusable_network(self) -> None:
        cases = (
            b"[DEFAULT]\nnet_01_card=x\n[Floppy and CD-ROM drives]\n",
            b"[Network]\na=1\n[Network]\nb=2\n[Floppy and CD-ROM drives]\n",
            "[Ｎetwork]\na=1\n[Floppy and CD-ROM drives]\n".encode("utf-8"),
            b"[network]\na=1\n[Floppy and CD-ROM drives]\n",
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(HARNESS.HarnessError):
                    HARNESS.sanitize_machine_config(value)

    def test_clone_must_create_distinct_inode_and_matching_content(self) -> None:
        source = self.root / "source.img"
        source.write_bytes(b"media bytes")
        snapshot = HARNESS.snapshot_file("media", source)
        destination = self.root / "copy.img"

        def copy_clone(original: Path, copied: Path) -> bool:
            shutil.copyfile(original, copied)
            return True

        HARNESS.stage_medium(snapshot, destination, False, copy_clone)
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertNotEqual(destination.stat().st_ino, source.stat().st_ino)

    def test_hardlink_clone_is_rejected(self) -> None:
        source = self.root / "source.img"
        source.write_bytes(b"media bytes")
        snapshot = HARNESS.snapshot_file("media", source)
        destination = self.root / "copy.img"

        def hardlink_clone(original: Path, copied: Path) -> bool:
            os.link(original, copied)
            return True

        with self.assertRaisesRegex(HARNESS.HarnessError, "distinct regular"):
            HARNESS.stage_medium(snapshot, destination, False, hardlink_clone)

    def test_clone_failure_requires_explicit_full_copy(self) -> None:
        source = self.root / "source.img"
        source.write_bytes(b"media bytes")
        snapshot = HARNESS.snapshot_file("media", source)
        with self.assertRaisesRegex(HARNESS.HarnessError, "allow-full-copy"):
            HARNESS.stage_medium(snapshot, self.root / "copy.img", False, lambda _a, _b: False)

    def test_source_mutation_is_detected(self) -> None:
        source = self.root / "source.img"
        source.write_bytes(b"before")
        snapshot = HARNESS.snapshot_file("media", source)
        source.write_bytes(b"after")
        self.assertFalse(HARNESS.snapshot_matches(snapshot))

    def test_validation_failure_rehashes_every_acquired_source(self) -> None:
        archive, trust = self.make_archive()
        roms = self.root / "roms"
        assets = self.root / "assets"
        roms.mkdir()
        assets.mkdir()
        hdd = self.root / "hdd"
        floppy = self.root / "floppy"
        iso = self.root / "iso"
        hdd.write_bytes(b"123")
        floppy.write_bytes(b"\0" * 1_474_560)
        iso.write_bytes(b"iso")
        inputs = HARNESS.PrivateInputs(archive, roms, assets, hdd, floppy, iso)
        recipe = {"requirements": {"rom_subdirectories": [], "asset_files": []}, "hard_disks": [{"bytes": 4}], "machine_config": "86box.cfg"}
        original_snapshot = HARNESS.snapshot_file

        def mutate_after_last_snapshot(role: str, path: Path) -> object:
            result = original_snapshot(role, path)
            if role == "install_iso":
                archive.write_bytes(archive.read_bytes() + b"changed")
            return result

        with (
            mock.patch.object(HARNESS, "load_recipe", return_value=recipe),
            mock.patch.object(HARNESS, "snapshot_file", side_effect=mutate_after_last_snapshot),
        ):
            with self.assertRaisesRegex(HARNESS.HarnessError, "source changed"):
                HARNESS.validate_private_inputs(inputs, trust)

    def test_private_media_symlink_and_fifo_are_rejected(self) -> None:
        target = self.root / "target.img"
        target.write_bytes(b"media")
        linked = self.root / "linked.img"
        linked.symlink_to(target)
        with self.assertRaisesRegex(HARNESS.HarnessError, "unavailable or unsafe"):
            HARNESS.snapshot_file("media", linked)
        fifo = self.root / "media.fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(HARNESS.HarnessError, "regular file"):
            HARNESS.snapshot_file("media", fifo)

    def test_private_input_rejects_symlinked_ancestor(self) -> None:
        real = self.root / "real"
        real.mkdir()
        (real / "medium.img").write_bytes(b"private")
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(HARNESS.HarnessError, "unsafe directory component"):
            HARNESS.snapshot_file("media", linked / "medium.img")

    def test_full_copy_checks_capacity_before_opening_destination(self) -> None:
        source = self.root / "source.img"
        source.write_bytes(b"media")
        usage = shutil.disk_usage(self.root)._replace(free=1)
        with mock.patch.object(HARNESS.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(HARNESS.HarnessError, "insufficient free space"):
                HARNESS.full_copy(source, self.root / "copy.img", source.stat().st_size)
        self.assertFalse((self.root / "copy.img").exists())

    def test_archive_traversal_is_rejected(self) -> None:
        archive, trust = self.make_archive([("../escape", b"x", stat.S_IFREG | 0o644)])
        snapshot = HARNESS.snapshot_file("archive", archive)
        with self.assertRaisesRegex(HARNESS.HarnessError, "unsafe member path"):
            HARNESS.extract_trusted_archive(snapshot, self.root / "extract", trust)

    def test_archive_special_file_is_rejected(self) -> None:
        archive, trust = self.make_archive([("86Box.app/device", b"", stat.S_IFIFO | 0o600)])
        snapshot = HARNESS.snapshot_file("archive", archive)
        with self.assertRaisesRegex(HARNESS.HarnessError, "special file"):
            HARNESS.extract_trusted_archive(snapshot, self.root / "extract", trust)

    def test_archive_case_collision_is_rejected(self) -> None:
        archive, trust = self.make_archive(
            [
                ("86Box.app/Contents/MacOS/86Box", b"#!/bin/sh\n", stat.S_IFREG | 0o755),
                ("86Box.app/contents/macos/86box", b"other", stat.S_IFREG | 0o644),
            ]
        )
        snapshot = HARNESS.snapshot_file("archive", archive)
        with self.assertRaisesRegex(HARNESS.HarnessError, "colliding"):
            HARNESS.extract_trusted_archive(snapshot, self.root / "extract", trust)

    def test_reviewed_symlink_may_not_escape_bundle(self) -> None:
        archive, trust = self.make_archive(
            [
                ("86Box.app/Contents/MacOS/86Box", b"#!/bin/sh\n", stat.S_IFREG | 0o755),
                ("86Box.app/escape", b"../../outside", stat.S_IFLNK | 0o777),
            ]
        )
        snapshot = HARNESS.snapshot_file("archive", archive)
        with self.assertRaisesRegex(HARNESS.HarnessError, "escapes"):
            HARNESS.extract_trusted_archive(snapshot, self.root / "extract", trust)

    def test_archive_unexpected_symlink_is_rejected(self) -> None:
        archive, trust = self.make_archive(
            [
                ("86Box.app/Contents/MacOS/86Box", b"#!/bin/sh\n", stat.S_IFREG | 0o755),
                ("86Box.app/unexpected", b"Contents", stat.S_IFLNK | 0o777),
            ]
        )
        trust = HARNESS.dataclasses.replace(trust, symlinks={})
        snapshot = HARNESS.snapshot_file("archive", archive)
        with self.assertRaisesRegex(HARNESS.HarnessError, "symbolic-link layout"):
            HARNESS.extract_trusted_archive(snapshot, self.root / "extract", trust)

    def test_safe_reviewed_symlink_extracts_inside_bundle(self) -> None:
        members = [
            ("86Box.app/Contents/MacOS/86Box", b"#!/bin/sh\n", stat.S_IFREG | 0o755),
            ("86Box.app/Contents/current", b"MacOS", stat.S_IFLNK | 0o777),
        ]
        archive, trust = self.make_archive(members)
        snapshot = HARNESS.snapshot_file("archive", archive)
        executable = HARNESS.extract_trusted_archive(snapshot, self.root / "extract", trust)
        self.assertTrue(executable.is_file())
        self.assertEqual(os.readlink(self.root / "extract" / "86Box.app" / "Contents" / "current"), "MacOS")

    def test_trust_hash_mismatch_is_rejected_before_extraction(self) -> None:
        archive, trust = self.make_archive()
        trust = HARNESS.dataclasses.replace(trust, archive_sha256="0" * 64)
        with self.assertRaisesRegex(HARNESS.HarnessError, "changed before extraction"):
            HARNESS.extract_trusted_archive(HARNESS.snapshot_file("archive", archive), self.root / "extract", trust)
        self.assertFalse((self.root / "extract").exists())

    def test_macho_architecture_parser(self) -> None:
        binary = self.root / "universal"
        entries = struct.pack(">IIIII", 0x01000007, 0, 48, 4, 0) + struct.pack(">IIIII", 0x0100000C, 0, 52, 4, 0)
        binary.write_bytes(b"\xca\xfe\xba\xbe" + struct.pack(">I", 2) + entries + b"xxxx" + b"yyyy")
        self.assertEqual(HARNESS.parse_macho_arches(binary), frozenset({"x86_64", "arm64"}))

    def test_signature_check_accepts_only_known_invalid_result(self) -> None:
        invalid = [
            subprocess.CompletedProcess([], 1, "", f"item: invalid signature (code or signature have been modified)\nIn architecture: {arch}\n")
            for arch in ("arm64", "x86_64", "arm64")
        ]
        with mock.patch.object(HARNESS.subprocess, "run", side_effect=invalid) as runner:
            self.assertEqual(HARNESS.verify_known_invalid_signature(Path("app"), Path("exe")), "known_invalid")
            self.assertEqual(runner.call_count, 3)
        valid = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(HARNESS.subprocess, "run", return_value=valid):
            with self.assertRaisesRegex(HARNESS.HarnessError, "differs"):
                HARNESS.verify_known_invalid_signature(Path("app"), Path("exe"))

    def test_cleanup_requires_unchanged_sentinel(self) -> None:
        workspace = HARNESS.create_workspace()
        sentinel = workspace.path / HARNESS.SENTINEL_NAME
        sentinel.write_text("tampered\n", encoding="ascii")
        with self.assertRaisesRegex(HARNESS.HarnessError, "refusing"):
            HARNESS.cleanup_workspace(workspace)
        self.assertTrue(workspace.path.exists())
        shutil.rmtree(workspace.path)

    def test_cleanup_rejects_symlink_sentinel(self) -> None:
        workspace = HARNESS.create_workspace()
        sentinel = workspace.path / HARNESS.SENTINEL_NAME
        sentinel.unlink()
        target = workspace.path / "target"
        target.write_text("x", encoding="utf-8")
        sentinel.symlink_to(target)
        with self.assertRaisesRegex(HARNESS.HarnessError, "refusing"):
            HARNESS.cleanup_workspace(workspace)
        shutil.rmtree(workspace.path)

    def test_cleanup_empties_but_does_not_name_delete_valid_workspace(self) -> None:
        workspace = HARNESS.create_workspace()
        HARNESS.cleanup_workspace(workspace)
        self.assert_emptied_workspace(workspace.path)

    def test_cleanup_never_rmdirs_verified_root(self) -> None:
        workspace = HARNESS.create_workspace()
        with mock.patch.object(HARNESS.os, "rmdir", side_effect=AssertionError("root rmdir is raceable")):
            HARNESS.cleanup_workspace(workspace)
        self.assert_emptied_workspace(workspace.path)

    def test_partial_workspace_creation_is_cleaned(self) -> None:
        created: list[Path] = []
        original_mkdtemp = HARNESS.tempfile.mkdtemp

        def record_mkdtemp(*args: object, **kwargs: object) -> str:
            value = original_mkdtemp(*args, **kwargs)
            created.append(Path(value))
            return value

        with (
            mock.patch.object(HARNESS.tempfile, "mkdtemp", side_effect=record_mkdtemp),
            mock.patch.object(HARNESS.os, "open", side_effect=OSError("injected")),
        ):
            with self.assertRaises(OSError):
                HARNESS.create_workspace()
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_cleanup_rejects_replaced_workspace_inode(self) -> None:
        workspace = HARNESS.create_workspace()
        original = workspace.path.with_name(workspace.path.name + "-original")
        workspace.path.rename(original)
        workspace.path.mkdir(mode=0o700)
        sentinel = workspace.path / HARNESS.SENTINEL_NAME
        sentinel.write_text(f"{HARNESS.SENTINEL_MAGIC}\n{workspace.nonce}\n", encoding="ascii")
        sentinel.chmod(0o600)
        with self.assertRaisesRegex(HARNESS.HarnessError, "refusing"):
            HARNESS.cleanup_workspace(workspace)
        shutil.rmtree(workspace.path)
        shutil.rmtree(original)

    def test_cleanup_top_level_swap_never_deletes_replacement(self) -> None:
        workspace = HARNESS.create_workspace()
        (workspace.path / "private-data").write_text("staged", encoding="ascii")
        moved = workspace.path.with_name(workspace.path.name + "-moved")
        original_listdir = os.listdir
        swapped = False

        def swap_then_list(directory: object) -> list[str]:
            nonlocal swapped
            if not swapped:
                swapped = True
                workspace.path.rename(moved)
                workspace.path.mkdir(mode=0o700)
                (workspace.path / "valuable").write_text("preserve", encoding="ascii")
            return original_listdir(directory)

        with mock.patch.object(HARNESS.os, "listdir", side_effect=swap_then_list):
            HARNESS.cleanup_workspace(workspace)
        self.assertEqual((workspace.path / "valuable").read_text(encoding="ascii"), "preserve")
        shutil.rmtree(workspace.path)
        moved.rmdir()

    def test_launch_process_terminates_child_group_on_signal(self) -> None:
        stdout = os.open(self.root / "stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr = os.open(self.root / "stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            with self.assertRaises(HARNESS.Interrupted):
                HARNESS.launch_process(
                    ["/bin/sh", "-c", "trap '' TERM; sleep 30"],
                    self.root,
                    {"PATH": "/usr/bin:/bin"},
                    stdout,
                    stderr,
                )
        finally:
            timer.cancel()
            os.close(stdout)
            os.close(stderr)

    def test_launch_process_terminates_descendants_after_leader_exits(self) -> None:
        stdout = os.open(self.root / "stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr = os.open(self.root / "stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        marker = self.root / "descendant-terminated"
        ready = self.root / "descendant-ready"
        try:
            result = HARNESS.launch_process(
                [
                    "/bin/sh",
                    "-c",
                    "(trap 'printf x > \"$1\"; exit 0' TERM; printf ready > \"$2\"; while :; do sleep 1; done) & while [ ! -f \"$2\" ]; do :; done",
                    "private-acceptance-test",
                    str(marker),
                    str(ready),
                ],
                self.root,
                {"PATH": "/usr/bin:/bin"},
                stdout,
                stderr,
            )
        finally:
            os.close(stdout)
            os.close(stderr)
        self.assertEqual(result, 0)
        self.assertEqual(marker.read_text(encoding="ascii"), "x")

    def test_signal_during_descendant_cleanup_is_not_lost(self) -> None:
        stdout = os.open(self.root / "stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr = os.open(self.root / "stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        original_terminate = HARNESS.terminate_group
        timer = threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            with mock.patch.object(HARNESS, "terminate_group", side_effect=lambda process: original_terminate(process, grace_seconds=0.3)):
                with self.assertRaises(HARNESS.Interrupted):
                    HARNESS.launch_process(
                        ["/bin/sh", "-c", "(trap '' TERM; exec sleep 30) &"],
                        self.root,
                        {"PATH": "/usr/bin:/bin"},
                        stdout,
                        stderr,
                    )
        finally:
            timer.cancel()
            os.close(stdout)
            os.close(stderr)

    def test_post_kill_probe_confirms_process_group_is_gone(self) -> None:
        stdout = os.open(self.root / "stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr = os.open(self.root / "stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        process_group_file = self.root / "process-group"
        original_terminate = HARNESS.terminate_group
        try:
            with mock.patch.object(HARNESS, "terminate_group", side_effect=lambda process: original_terminate(process, grace_seconds=0.2)):
                result = HARNESS.launch_process(
                    [
                        "/bin/sh",
                        "-c",
                        "printf '%s' \"$$\" > \"$1\"; (trap '' TERM; exec sleep 30) &",
                        "private-acceptance-test",
                        str(process_group_file),
                    ],
                    self.root,
                    {"PATH": "/usr/bin:/bin"},
                    stdout,
                    stderr,
                )
        finally:
            os.close(stdout)
            os.close(stderr)
        self.assertEqual(result, 0)
        process_group = int(process_group_file.read_text(encoding="ascii"))
        with self.assertRaises(ProcessLookupError):
            os.killpg(process_group, 0)

    def test_post_kill_probe_fails_closed_when_group_remains(self) -> None:
        process = mock.Mock()
        process.pid = 12345
        process.poll.return_value = 0
        with (
            mock.patch.object(HARNESS.os, "killpg"),
            mock.patch.object(HARNESS.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 2.5, 4.0]),
            mock.patch.object(HARNESS.time, "sleep"),
        ):
            with self.assertRaisesRegex(HARNESS.ProcessGroupAlive, "workspace was retained"):
                HARNESS.terminate_group(process, grace_seconds=0)

    def test_post_kill_leader_wait_is_bounded(self) -> None:
        process = mock.Mock()
        process.pid = 12345
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("fake-emulator", 0.05)
        with (
            mock.patch.object(HARNESS.os, "killpg"),
            mock.patch.object(HARNESS.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 2.5, 4.0]),
            mock.patch.object(HARNESS.time, "sleep"),
        ):
            with self.assertRaisesRegex(HARNESS.ProcessGroupAlive, "workspace was retained"):
                HARNESS.terminate_group(process, grace_seconds=0)
        self.assertTrue(process.wait.called)
        self.assertTrue(all(call.kwargs.get("timeout") == 0.05 for call in process.wait.call_args_list))

    def test_staged_config_is_reopened_and_reparsed(self) -> None:
        source = b"[Floppy and CD-ROM drives]\nfdd_01_type = 35_2hd\n"
        rendered = HARNESS.sanitize_machine_config(source)
        config = self.root / "86box.cfg"
        config.write_bytes(rendered)
        HARNESS.verify_staged_config(config, rendered)
        config.write_bytes(rendered + b"\n[Network]\nnet_01_card = pcnetpci\n")
        with self.assertRaisesRegex(HARNESS.HarnessError, "changed after staging"):
            HARNESS.verify_staged_config(config, rendered)

    def test_report_schema_is_fixed_redacted_and_no_clobber(self) -> None:
        report = HARNESS.build_report(HARNESS.PRODUCTION_TRUST, "not_requested")
        path = self.root / "report.json"
        HARNESS.write_report_no_clobber(path, report)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(parsed),
            {
                "schema_version",
                "recipe_id",
                "recipe_sha256",
                "machine_config_sha256",
                "emulator",
                "platform",
                "timestamp_utc",
                "preflight_state",
                "launch_state",
                "no_guest_nic_configured",
                "guest_checks",
            },
        )
        self.assertEqual(set(parsed["guest_checks"].values()), {"not_observed"})
        forbidden = os.sep + "Users" + os.sep + "private-person"
        self.assertNotIn(forbidden, path.read_text(encoding="utf-8"))
        self.assertNotIn(HARNESS.PRODUCTION_TRUST.archive_sha256, path.read_text(encoding="utf-8"))
        self.assertNotIn(HARNESS.PRODUCTION_TRUST.executable_sha256, path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(HARNESS.HarnessError, "new file|already exists"):
            HARNESS.write_report_no_clobber(path, report)

    def test_default_preflight_never_creates_workspace_or_launches(self) -> None:
        archive, trust = self.make_archive()
        manifest = self.write_manifest(archive=str(archive))
        inputs = HARNESS.PrivateInputs(archive, self.root / "roms", self.root / "assets", self.root / "hdd", self.root / "floppy", self.root / "iso")
        snapshot = HARNESS.snapshot_file("archive", archive)
        snapshots = {name: HARNESS.dataclasses.replace(snapshot, role=name) for name in ("archive", "installed_hdd", "startup_floppy", "install_iso")}
        recipe = HARNESS.load_recipe()
        sanitized = HARNESS.sanitize_machine_config((HARNESS.MACHINE_ROOT / "86box.cfg").read_bytes())
        with (
            mock.patch.object(HARNESS, "load_private_manifest", return_value=inputs),
            mock.patch.object(HARNESS, "validate_private_inputs", return_value=(snapshots, sanitized, recipe)),
            mock.patch.object(HARNESS, "create_workspace", side_effect=AssertionError("workspace created")),
        ):
            result = HARNESS.run(HARNESS.Options(manifest), trust, HARNESS.Hooks(validate_repository=False))
        self.assertEqual(result, 0)

    def test_signal_during_staging_cleans_workspace_and_rehashes_sources(self) -> None:
        archive, trust = self.make_archive()
        paths: dict[str, Path] = {}
        snapshots = {"archive": HARNESS.snapshot_file("archive", archive)}
        for role in ("installed_hdd", "startup_floppy", "install_iso"):
            paths[role] = self.root / role
            paths[role].write_bytes(role.encode("ascii"))
            snapshots[role] = HARNESS.snapshot_file(role, paths[role])
        roms = self.root / "roms"
        assets = self.root / "assets"
        roms.mkdir()
        assets.mkdir()
        inputs = HARNESS.PrivateInputs(archive, roms, assets, paths["installed_hdd"], paths["startup_floppy"], paths["install_iso"])
        recipe = HARNESS.load_recipe()
        sanitized = HARNESS.sanitize_machine_config((HARNESS.MACHINE_ROOT / "86box.cfg").read_bytes())
        observed: list[Path] = []
        original_create = HARNESS.create_workspace

        def record_workspace() -> object:
            workspace = original_create()
            observed.append(workspace.path)
            return workspace

        def interrupt_clone(_source: Path, _destination: Path) -> bool:
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("signal handler did not interrupt staging")

        hooks = HARNESS.Hooks(validate_repository=False, platform_name="Darwin", try_clone=interrupt_clone)
        with (
            mock.patch.object(HARNESS, "load_private_manifest", return_value=inputs),
            mock.patch.object(HARNESS, "validate_private_inputs", return_value=(snapshots, sanitized, recipe)),
            mock.patch.object(HARNESS, "create_workspace", side_effect=record_workspace),
            mock.patch.dict(os.environ, {name: "" for name in HARNESS.CI_MARKERS}, clear=False),
        ):
            with self.assertRaises(HARNESS.Interrupted):
                HARNESS.run(HARNESS.Options(self.root / "manifest", launch=True, source_vm_stopped=True), trust, hooks)
        self.assertEqual(len(observed), 1)
        self.assert_emptied_workspace(observed[0])
        self.assertTrue(all(HARNESS.snapshot_matches(snapshot) for snapshot in snapshots.values()))

    def test_signal_after_process_return_is_deferred_until_cleanup_finishes(self) -> None:
        archive, trust = self.make_archive()
        snapshot = HARNESS.snapshot_file("archive", archive)
        snapshots = {name: HARNESS.dataclasses.replace(snapshot, role=name) for name in ("archive", "installed_hdd", "startup_floppy", "install_iso")}
        inputs = HARNESS.PrivateInputs(archive, self.root, self.root, archive, archive, archive)
        recipe = HARNESS.load_recipe()
        sanitized = HARNESS.sanitize_machine_config((HARNESS.MACHINE_ROOT / "86box.cfg").read_bytes())
        observed: list[Path] = []
        original_create = HARNESS.create_workspace
        signalled = False

        def record_workspace() -> object:
            workspace = original_create()
            observed.append(workspace.path)
            return workspace

        def fake_extract(_snapshot: object, destination: Path, _trust: object) -> Path:
            executable = destination / trust.executable_path
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"#!/bin/sh\n")
            return executable

        def signal_while_finalizing(value: object) -> bool:
            nonlocal signalled
            if not signalled:
                signalled = True
                os.kill(os.getpid(), signal.SIGTERM)
            return True

        hooks = HARNESS.Hooks(
            validate_repository=False,
            platform_name="Darwin",
            try_clone=lambda _a, _b: True,
            signature_verifier=lambda _a, _b: "known_invalid",
            process_launcher=lambda _a, _b, _c, _d, _e: 0,
        )
        with (
            mock.patch.object(HARNESS, "load_private_manifest", return_value=inputs),
            mock.patch.object(HARNESS, "validate_private_inputs", return_value=(snapshots, sanitized, recipe)),
            mock.patch.object(HARNESS, "create_workspace", side_effect=record_workspace),
            mock.patch.object(HARNESS, "copy_public_inputs"),
            mock.patch.object(HARNESS, "stage_medium"),
            mock.patch.object(HARNESS, "extract_trusted_archive", side_effect=fake_extract),
            mock.patch.object(HARNESS, "verify_staged_config"),
            mock.patch.object(HARNESS, "snapshot_file", return_value=HARNESS.dataclasses.replace(snapshot, digest=trust.executable_sha256)),
            mock.patch.object(HARNESS, "snapshot_matches", side_effect=signal_while_finalizing),
            mock.patch.dict(os.environ, {name: "" for name in HARNESS.CI_MARKERS}, clear=False),
        ):
            with self.assertRaises(HARNESS.Interrupted):
                HARNESS.run(HARNESS.Options(self.root / "manifest", launch=True, source_vm_stopped=True), trust, hooks)
        self.assertTrue(signalled)
        self.assert_emptied_workspace(observed[0])

    def test_cleanup_signal_does_not_mask_live_process_group(self) -> None:
        archive, trust = self.make_archive()
        snapshot = HARNESS.snapshot_file("archive", archive)
        snapshots = {name: HARNESS.dataclasses.replace(snapshot, role=name) for name in ("archive", "installed_hdd", "startup_floppy", "install_iso")}
        inputs = HARNESS.PrivateInputs(archive, self.root, self.root, archive, archive, archive)
        recipe = HARNESS.load_recipe()
        sanitized = HARNESS.sanitize_machine_config((HARNESS.MACHINE_ROOT / "86box.cfg").read_bytes())
        observed: list[Path] = []
        original_create = HARNESS.create_workspace
        signalled = False

        def record_workspace() -> object:
            workspace = original_create()
            observed.append(workspace.path)
            return workspace

        def fake_extract(_snapshot: object, destination: Path, _trust: object) -> Path:
            executable = destination / trust.executable_path
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"#!/bin/sh\n")
            return executable

        def signal_while_finalizing(_value: object) -> bool:
            nonlocal signalled
            if not signalled:
                signalled = True
                os.kill(os.getpid(), signal.SIGTERM)
            return True

        def report_live_group(*_args: object) -> int:
            raise HARNESS.ProcessGroupAlive("emulator process group did not stop; disposable workspace was retained")

        hooks = HARNESS.Hooks(
            validate_repository=False,
            platform_name="Darwin",
            try_clone=lambda _a, _b: True,
            signature_verifier=lambda _a, _b: "known_invalid",
            process_launcher=report_live_group,
        )
        with (
            mock.patch.object(HARNESS, "load_private_manifest", return_value=inputs),
            mock.patch.object(HARNESS, "validate_private_inputs", return_value=(snapshots, sanitized, recipe)),
            mock.patch.object(HARNESS, "create_workspace", side_effect=record_workspace),
            mock.patch.object(HARNESS, "copy_public_inputs"),
            mock.patch.object(HARNESS, "stage_medium"),
            mock.patch.object(HARNESS, "extract_trusted_archive", side_effect=fake_extract),
            mock.patch.object(HARNESS, "verify_staged_config"),
            mock.patch.object(HARNESS, "snapshot_file", return_value=HARNESS.dataclasses.replace(snapshot, digest=trust.executable_sha256)),
            mock.patch.object(HARNESS, "snapshot_matches", side_effect=signal_while_finalizing),
            mock.patch.dict(os.environ, {name: "" for name in HARNESS.CI_MARKERS}, clear=False),
            mock.patch("sys.stderr", new=io.StringIO()),
        ):
            with self.assertRaises(HARNESS.ProcessGroupAlive):
                HARNESS.run(HARNESS.Options(self.root / "manifest", launch=True, source_vm_stopped=True), trust, hooks)
        self.assertTrue(signalled)
        self.assertTrue(observed[0].exists())
        shutil.rmtree(observed[0])

    def test_synthetic_launch_uses_disposable_media_and_never_claims_guest_success(self) -> None:
        archive, trust = self.make_archive()
        media = {}
        for role in ("installed_hdd", "startup_floppy", "install_iso"):
            path = self.root / role
            path.write_bytes((role + " data").encode("ascii"))
            media[role] = HARNESS.snapshot_file(role, path)
        snapshots = {"archive": HARNESS.snapshot_file("archive", archive), **media}
        roms = self.root / "roms"
        assets = self.root / "assets"
        roms.mkdir()
        assets.mkdir()
        inputs = HARNESS.PrivateInputs(archive, roms, assets, media["installed_hdd"].path, media["startup_floppy"].path, media["install_iso"].path)
        recipe = HARNESS.load_recipe()
        sanitized = HARNESS.sanitize_machine_config((HARNESS.MACHINE_ROOT / "86box.cfg").read_bytes())
        manifest = self.write_manifest(archive=str(archive))
        report_path = self.root / "launch-report.json"
        observed: dict[str, object] = {}

        def clone(source: Path, destination: Path) -> bool:
            shutil.copyfile(source, destination)
            return True

        def launch(argv: object, workspace: Path, environment: object, stdout: int, stderr: int) -> int:
            observed["argv"] = list(argv)
            observed["workspace"] = workspace
            config = workspace / "machine" / str(recipe["machine_config"])
            HARNESS.assert_disposable_config(config.read_bytes())
            copied_hdd = workspace / "machine" / "disks" / "windows95.hdd"
            copied_hdd.write_bytes(b"guest mutation")
            return 0

        hooks = HARNESS.Hooks(
            validate_repository=False,
            platform_name="Darwin",
            try_clone=clone,
            signature_verifier=lambda _app, _exe: "known_invalid",
            process_launcher=launch,
        )
        with (
            mock.patch.object(HARNESS, "load_private_manifest", return_value=inputs),
            mock.patch.object(HARNESS, "validate_private_inputs", return_value=(snapshots, sanitized, recipe)),
            mock.patch.dict(os.environ, {name: "" for name in HARNESS.CI_MARKERS}, clear=False),
        ):
            result = HARNESS.run(
                HARNESS.Options(manifest, launch=True, source_vm_stopped=True, report=report_path),
                trust,
                hooks,
            )
        self.assertEqual(result, 0)
        self.assertEqual(media["installed_hdd"].path.read_bytes(), b"installed_hdd data")
        serialized_argv = "\0".join(observed["argv"])
        for snapshot in media.values():
            self.assertNotIn(str(snapshot.path), serialized_argv)
        self.assert_emptied_workspace(Path(observed["workspace"]))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["launch_state"], "clean_process_exit")
        self.assertEqual(set(report["guest_checks"].values()), {"not_observed"})


if __name__ == "__main__":
    unittest.main()

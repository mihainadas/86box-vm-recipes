from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import stat
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "software-media.py"
SPEC = importlib.util.spec_from_file_location("software_media", SCRIPT)
assert SPEC and SPEC.loader
software_media = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = software_media
SPEC.loader.exec_module(software_media)


class SoftwareMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="software-media-test-")
        self.root = Path(os.path.realpath(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_zip(self, members: dict[str, bytes], name: str = "media.zip") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, payload in members.items():
                archive.writestr(member, payload)
        return path

    def private_manifest(self, source: Path, digest: str, mode: int = 0o600) -> Path:
        path = self.root / "private.toml"
        path.write_text(
            "schema_version = 1\n\n"
            "[[item]]\n"
            'id = "demo"\n'
            'title = "Demo"\n'
            'version = "1"\n'
            'source_type = "file"\n'
            f'path = "{source}"\n'
            f'sha256 = "{digest}"\n'
            'destination = "APPS/DEMO/SETUP.EXE"\n'
            "license_acknowledged = true\n",
            encoding="utf-8",
        )
        path.chmod(mode)
        return path

    def test_repository_catalog_is_valid_and_pinned(self) -> None:
        catalog, bundles = software_media.load_all()
        item = catalog["freedos-14"]
        self.assertEqual("modern-retro", item.compatibility)
        self.assertEqual("redistributable", item.distribution)
        self.assertEqual(
            "715b01754b8f5fd24cceb32d023f46aa9456c582c594d7d5fbe34787e1c773d4",
            item.artifacts[0].sha256,
        )
        self.assertTrue(bundles["freedos-14-kit"].redistributable)
        self.assertTrue(bundles["dream-486-personal"].allow_private)

    def test_extracts_exactly_one_iso_and_hashes_it(self) -> None:
        source = self.make_zip({"docs/readme.txt": b"docs", "images/install.iso": b"ISO bytes"})
        destination = self.root / "out" / "INSTALL.ISO"
        size, digest = software_media.extract_single_iso(
            source, destination, 100, source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest()
        )
        self.assertEqual(9, size)
        self.assertEqual(hashlib.sha256(b"ISO bytes").hexdigest(), digest)
        self.assertEqual(b"ISO bytes", destination.read_bytes())

    def test_rejects_unsafe_ambiguous_and_oversized_archives(self) -> None:
        cases = (
            ({"../escape.iso": b"x"}, 100),
            ({"one.iso": b"x", "two.iso": b"y"}, 100),
            ({"one.iso": b"too large"}, 2),
        )
        for index, (members, maximum) in enumerate(cases):
            with self.subTest(index=index):
                source = self.make_zip(members, f"bad-{index}.zip")
                with self.assertRaises(software_media.MediaError):
                    software_media.extract_single_iso(
                        source, self.root / f"out-{index}.iso", maximum,
                        source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest()
                    )

    def test_rejects_zip_symlink_member(self) -> None:
        source = self.root / "symlink.zip"
        info = zipfile.ZipInfo("image.iso")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(info, "elsewhere")
        with self.assertRaises(software_media.MediaError):
            software_media.extract_single_iso(
                source, self.root / "result.iso", 100,
                source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest()
            )

    def test_private_manifest_requires_owner_only_mode_and_matching_hash(self) -> None:
        source = self.root / "installer.exe"
        source.write_bytes(b"synthetic fixture")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = self.private_manifest(source, digest)
        items = software_media.load_private(manifest)
        self.assertEqual(source, items[0].path)
        destination = self.root / "copy.exe"
        software_media.copy_verified(source, destination, 100, digest)
        self.assertEqual(source.read_bytes(), destination.read_bytes())
        manifest.chmod(0o644)
        with self.assertRaises(software_media.MediaError):
            software_media.load_private(manifest)

    def test_private_manifest_and_inputs_reject_symlinks(self) -> None:
        source = self.root / "installer.exe"
        source.write_bytes(b"synthetic fixture")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = self.private_manifest(source, digest)
        manifest_link = self.root / "manifest-link.toml"
        manifest_link.symlink_to(manifest)
        with self.assertRaises(software_media.MediaError):
            software_media.load_private(manifest_link)
        source_link = self.root / "installer-link.exe"
        source_link.symlink_to(source)
        with self.assertRaises(software_media.MediaError):
            software_media.copy_verified(source_link, self.root / "copy.exe", 100, digest)

    def test_digest_mismatch_uses_sanitized_error(self) -> None:
        source = self.root / "sensitive-installer.exe"
        source.write_bytes(b"fixture")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = software_media.main([
                "build", "--bundle", "dream-486-personal", "--cache", str(self.root / "cache"),
                "--output", str(self.root / "out.iso"), "--private-manifest",
                str(self.private_manifest(source, "0" * 64)),
            ])
        self.assertEqual(1, status)
        self.assertNotIn(str(source), stderr.getvalue())
        self.assertNotIn("sensitive-installer", stderr.getvalue())

    def test_public_kit_is_reproducible_and_never_overwrites(self) -> None:
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "B.TXT").write_bytes(b"second")
        (stage / "A.TXT").write_bytes(b"first")
        first = self.root / "first.zip"
        second = self.root / "second.zip"
        bundle = software_media.Bundle("test", "Test", "kit", True, "TEST", 100, 10000, (), False)
        first_fd = software_media.open_directory(first.parent)
        try:
            software_media.publish_kit(stage, self.root / "first.tmp", first.name, first_fd, bundle)
        finally:
            os.close(first_fd)
        second_fd = software_media.open_directory(second.parent)
        try:
            software_media.publish_kit(stage, self.root / "second.tmp", second.name, second_fd, bundle)
        finally:
            os.close(second_fd)
        self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
        before = first.read_bytes()
        with self.assertRaises(software_media.MediaError):
            parent_fd = software_media.open_directory(first.parent)
            try:
                software_media.publish_kit(stage, self.root / "third.tmp", first.name, parent_fd, bundle)
            finally:
                os.close(parent_fd)
        self.assertEqual(before, first.read_bytes())

    def test_private_build_is_disabled_in_ci_before_manifest_access(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"CI": "true"}, clear=False), contextlib.redirect_stderr(stderr):
            status = software_media.main([
                "build", "--bundle", "dream-486-personal", "--cache", str(self.root / "cache"),
                "--output", str(self.root / "out.iso"), "--private-manifest", str(self.root / "missing.toml"),
            ])
        self.assertEqual(1, status)
        self.assertIn("disabled in CI", stderr.getvalue())
        self.assertNotIn("missing.toml", stderr.getvalue())

    def test_spdx_parser_rejects_made_up_and_incomplete_expressions(self) -> None:
        self.assertEqual("MIT OR GPL-2.0-only", software_media.license_expression("MIT OR GPL-2.0-only"))
        for value in ("TOTALLY MADE UP", "MIT OR", "Unknown-1.0", "MIT WITH Made-up-exception"):
            with self.subTest(value=value), self.assertRaises(software_media.MediaError):
                software_media.license_expression(value)

    def test_nonredistributable_catalog_entry_cannot_declare_artifacts(self) -> None:
        path = self.root / "bad.toml"
        path.write_text(
            """schema_version = 1
id = "bad"
title = "Bad"
version = "1"
kind = "application"
compatibility = "period-authentic"
distribution = "user-supplied"
homepage = "https://example.invalid/"
license_expression = "NONE"
license_basis = "Private"
license_evidence = "https://example.invalid/license"
notice_location = "Private copy"
source_compliance = "Not distributed"
security_notes = "Offline"
personal_destination = "APPS/BAD/SETUP.EXE"
personal_source_type = "file"
[[artifact]]
id = "bad"
role = "installer"
url = "https://example.invalid/bad.exe"
filename = "bad.exe"
bytes = 1
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
destination = "APPS/BAD/SETUP.EXE"
extract_single_iso = false
""",
            encoding="utf-8",
        )
        with self.assertRaises(software_media.MediaError):
            software_media.load_catalog(path)

    def test_verified_archive_fd_survives_path_replacement(self) -> None:
        source = self.make_zip({"original.iso": b"trusted"})
        expected_size = source.stat().st_size
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        replacement = self.make_zip({"replacement.iso": b"attacker"}, "replacement.zip")
        original_hash = software_media.sha256_fd
        calls = 0

        def replace_after_hash(fd: int) -> str:
            nonlocal calls
            calls += 1
            result = original_hash(fd)
            if calls == 1:
                os.replace(replacement, source)
            return result

        destination = self.root / "trusted.iso"
        with mock.patch.object(software_media, "sha256_fd", side_effect=replace_after_hash):
            software_media.extract_single_iso(source, destination, 100, expected_size, expected_hash)
        self.assertEqual(b"trusted", destination.read_bytes())

    def test_archive_is_rehashed_after_extraction(self) -> None:
        source = self.make_zip({"original.iso": b"trusted"})
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        with mock.patch.object(software_media, "sha256_fd", side_effect=(expected_hash, "0" * 64)):
            with self.assertRaises(software_media.MediaError):
                software_media.extract_single_iso(
                    source, self.root / "untrusted.iso", 100, source.stat().st_size, expected_hash
                )

    def test_directory_entry_and_depth_limits_are_enforced(self) -> None:
        crowded = self.root / "crowded"
        crowded.mkdir()
        for name in ("A.TXT", "B.TXT", "C.TXT"):
            (crowded / name).touch()
        with mock.patch.object(software_media, "MAX_PRIVATE_ENTRIES", 2):
            with self.assertRaises(software_media.MediaError):
                software_media.process_directory(crowded, None, 100)
        deep = self.root / "deep"
        (deep / "A" / "B").mkdir(parents=True)
        with mock.patch.object(software_media, "MAX_PRIVATE_DEPTH", 1):
            with self.assertRaises(software_media.MediaError):
                software_media.process_directory(deep, None, 100)

    def test_iso_paths_are_strict_ascii_83(self) -> None:
        self.assertEqual("APPS/DEMO_1/SETUP.EXE", software_media.iso_path("APPS/DEMO_1/SETUP.EXE", "path"))
        for value in ("APPS/É.TXT", "APPS/١.TXT", "APPS/.TXT", "APPS/TOO-LONG.TXT"):
            with self.subTest(value=value), self.assertRaises(software_media.MediaError):
                software_media.iso_path(value, "path")

    def test_capacity_and_reserved_metadata_paths_fail_before_copy(self) -> None:
        source = self.root / "large.exe"
        source.write_bytes(b"large")
        with self.assertRaises(software_media.MediaError):
            software_media.copy_verified(source, self.root / "copy.exe", 2)
        self.assertFalse((self.root / "copy.exe").exists())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        item = software_media.PrivateItem("demo", "Demo", "1", "file", source, digest, "README.TXT")
        bundle = software_media.Bundle("personal", "Personal", "iso", False, "PERSONAL", 100, 1000, (), True)
        stage = self.root / "stage-reserved"
        stage.mkdir()
        with self.assertRaises(software_media.MediaError):
            software_media.populate_stage(stage, bundle, {}, self.root, (item,))
        self.assertEqual([], list(stage.iterdir()))

    def test_fetch_and_public_kit_build_are_end_to_end_and_pinned(self) -> None:
        payload = b"synthetic public ISO"
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("images/install.iso", payload)
        downloaded = archive_bytes.getvalue()
        artifact = software_media.Artifact(
            "media", "boot-media", "https://example.invalid/media.zip", "media.zip", len(downloaded),
            hashlib.sha256(downloaded).hexdigest(), "OS/DEMO/INSTALL.ISO", True,
        )
        item = software_media.CatalogItem(
            "demo-os", "Demo OS", "1", "operating_system", "modern-retro", "redistributable",
            "https://example.invalid/", "MIT", "Synthetic fixture", "https://example.invalid/license",
            "Inside fixture", "No source obligation", "Offline", "OS/DEMO/INSTALL.ISO", "file", (artifact,),
        )
        bundle = software_media.Bundle("demo-kit", "Demo kit", "kit", True, "DEMO", 1000, 10000, (item.id,), False)

        class Opener:
            def open(self, request: object, timeout: int) -> io.BytesIO:
                return io.BytesIO(downloaded)

        cache = self.root / "cache"
        with mock.patch.object(software_media.urllib.request, "build_opener", return_value=Opener()):
            fetched = software_media.fetch_artifact(artifact, cache)
        self.assertEqual(downloaded, fetched.read_bytes())
        output = self.root / "kit.zip"
        software_media.build(bundle, {item.id: item}, cache, output, ())
        with zipfile.ZipFile(output) as kit:
            self.assertEqual(
                ["CATALOG/MEDIA.JSON", "OS/DEMO/INSTALL.ISO", "README.TXT"],
                sorted(kit.namelist()),
            )
            self.assertEqual(payload, kit.read("OS/DEMO/INSTALL.ISO"))
            metadata = kit.read("CATALOG/MEDIA.JSON").decode("ascii")
            self.assertIn(artifact.sha256, metadata)
            self.assertIn("https://example.invalid/license", metadata)

    def test_failed_fetch_cleans_private_partial_and_http_redirect_is_rejected(self) -> None:
        payload = b"wrong"
        artifact = software_media.Artifact(
            "media", "installer", "https://example.invalid/media.bin", "media.bin", len(payload),
            "0" * 64, "APPS/DEMO/SETUP.EXE", False,
        )

        class Opener:
            def open(self, request: object, timeout: int) -> io.BytesIO:
                return io.BytesIO(payload)

        cache = self.root / "failed-cache"
        with mock.patch.object(software_media.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(software_media.MediaError):
                software_media.fetch_artifact(artifact, cache)
        self.assertEqual([], list(cache.iterdir()))
        handler = software_media.HttpsOnly()
        request = software_media.urllib.request.Request("https://example.invalid/start")
        with self.assertRaises(software_media.MediaError):
            handler.redirect_request(request, None, 302, "redirect", {}, "http://example.invalid/file")

    def test_iso_intermediate_is_confined_and_final_size_is_checked(self) -> None:
        stage = self.root / "iso-stage"
        stage.mkdir()
        (stage / "README.TXT").write_text("fixture", encoding="ascii")
        temporary = self.root / "secure-workspace" / "IMAGE.tmp"
        temporary.parent.mkdir(mode=0o700)
        output = self.root / "output.iso"
        parent_fd = software_media.open_directory(output.parent)
        observed: dict[str, int] = {}

        def fake_run(command: list[str], **kwargs: object) -> types.SimpleNamespace:
            target = Path(command[command.index("-o") + 1])
            observed["workspace_mode"] = stat.S_IMODE(target.parent.stat().st_mode)
            target.write_bytes(b"synthetic iso bytes")
            target.chmod(0o644)
            return types.SimpleNamespace(returncode=0)

        bundle = software_media.Bundle("iso", "ISO", "iso", False, "ISO", 100, 100, (), True)
        try:
            with mock.patch.object(software_media.subprocess, "run", side_effect=fake_run):
                software_media.publish_iso(stage, temporary, output.name, parent_fd, bundle)
        finally:
            os.close(parent_fd)
        self.assertEqual(0o700, observed["workspace_mode"])
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))

        too_small = software_media.Bundle("small", "Small", "iso", False, "SMALL", 1, 1, (), True)
        second_fd = software_media.open_directory(self.root)
        try:
            with mock.patch.object(software_media.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(software_media.MediaError):
                    software_media.publish_iso(stage, self.root / "too-small.tmp", "too-small.iso", second_fd, too_small)
        finally:
            os.close(second_fd)
        self.assertFalse((self.root / "too-small.iso").exists())

    def test_personal_template_is_catalog_driven_private_and_no_clobber(self) -> None:
        catalog, bundles = software_media.load_all()
        output = self.root / "personal.toml"
        software_media.write_personal_template(bundles["dream-486-personal"], catalog, output)
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
        text = output.read_text(encoding="utf-8")
        self.assertIn('id = "winamp-2x"', text)
        self.assertIn('source_type = "directory"', text)
        self.assertIn('destination = "APPS/OFFICE95"', text)
        self.assertIn("license_acknowledged = false", text)
        before = output.read_bytes()
        with self.assertRaises(software_media.MediaError):
            software_media.write_personal_template(bundles["dream-486-personal"], catalog, output)
        self.assertEqual(before, output.read_bytes())

    def test_directory_source_copies_complete_tree_and_rejects_symlinks(self) -> None:
        source = self.root / "disc"
        (source / "DATA").mkdir(parents=True)
        (source / "SETUP.EXE").write_bytes(b"setup")
        (source / "DATA" / "GAME.DAT").write_bytes(b"data")
        size, digest = software_media.process_directory(source, None, 100)
        self.assertEqual(9, size)
        destination = self.root / "copied"
        self.assertEqual((size, digest), software_media.process_directory(source, destination, 100, digest))
        self.assertEqual(b"setup", (destination / "SETUP.EXE").read_bytes())
        self.assertEqual(b"data", (destination / "DATA" / "GAME.DAT").read_bytes())
        (source / "BAD.EXE").symlink_to(source / "SETUP.EXE")
        with self.assertRaises(software_media.MediaError):
            software_media.process_directory(source, self.root / "unsafe", 100)


if __name__ == "__main__":
    unittest.main()

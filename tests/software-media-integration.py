#!/usr/bin/env python3
"""Exercise deterministic companion-ISO generation with synthetic bytes."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "software-media.py"
SPEC = importlib.util.spec_from_file_location("software_media_integration", SCRIPT)
assert SPEC and SPEC.loader
software_media = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = software_media
SPEC.loader.exec_module(software_media)


class SoftwareMediaIntegrationTests(unittest.TestCase):
    def test_xorriso_output_is_reproducible_and_contains_only_expected_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="software-media-integration-") as temporary:
            root = Path(os.path.realpath(temporary))
            source = root / "installer"
            source.mkdir()
            (source / "SETUP.EXE").write_bytes(b"synthetic setup fixture\n")
            (source / "DATA.CAB").write_bytes(b"synthetic data fixture\n")
            _, digest = software_media.process_directory(source, None, 1024 * 1024)
            item = software_media.PrivateItem("demo", "Demo", "1", "directory", source, digest, "APPS/DEMO")
            bundle = software_media.Bundle(
                "test-personal", "Synthetic personal disc", "iso", False, "TEST_DISC",
                1024 * 1024, 2 * 1024 * 1024, (), True
            )
            cache = root / "cache"
            cache.mkdir()
            outputs = (root / "first.iso", root / "second.iso")
            for output in outputs:
                software_media.build(bundle, {}, cache, output, (item,))
            self.assertEqual(
                hashlib.sha256(outputs[0].read_bytes()).digest(),
                hashlib.sha256(outputs[1].read_bytes()).digest(),
            )
            listing = subprocess.run(
                ["xorriso", "-indev", str(outputs[0]), "-find", "/", "-type", "f"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            ).stdout
            files = {line.strip().strip("'") for line in listing.splitlines() if line.strip().startswith("'/")}
            self.assertEqual(
                {"/APPS/DEMO/SETUP.EXE", "/APPS/DEMO/DATA.CAB", "/CATALOG/MEDIA.JSON", "/README.TXT"},
                files,
            )
            extracted = root / "media.json"
            subprocess.run(
                ["xorriso", "-osirrox", "on", "-indev", str(outputs[0]),
                 "-extract", "/CATALOG/MEDIA.JSON", str(extracted)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            metadata = extracted.read_text(encoding="ascii")
            self.assertNotIn(str(root), metadata)
            self.assertNotIn(str(source), metadata)
            self.assertIn('"private": true', metadata)


if __name__ == "__main__":
    unittest.main()

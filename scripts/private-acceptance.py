#!/usr/bin/env python3
"""Safely preflight private media and optionally launch a disposable 86Box VM."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MACHINE_ID = "1995-dream-486"
MACHINE_ROOT = REPOSITORY_ROOT / "machines" / MACHINE_ID
PRIVATE_FIELDS = frozenset(
    {"schema_version", "archive", "roms", "assets", "installed_hdd", "startup_floppy", "install_iso"}
)
CI_MARKERS = (
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "CIRCLECI",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
    "TF_BUILD",
)
WORKSPACE_PREFIX = "86box-private-acceptance-"
SENTINEL_NAME = ".86box-private-acceptance"
SENTINEL_MAGIC = "86box-private-acceptance-v1"
CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_MEMBER = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL = 400 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


V6_SYMLINKS = {
    "86Box.app/Contents/Frameworks/libxcb.1.dylib": "libxcb.1.1.0.dylib",
    "86Box.app/Contents/Frameworks/libbrotlidec.1.dylib": "libbrotlidec.1.2.0.dylib",
    "86Box.app/Contents/Frameworks/libgraphite2.3.dylib": "libgraphite2.3.2.1.dylib",
    "86Box.app/Contents/Frameworks/libbrotlicommon.1.dylib": "libbrotlicommon.1.2.0.dylib",
    "86Box.app/Contents/Frameworks/libopenjp2.7.dylib": "libopenjp2.2.5.4.dylib",
    "86Box.app/Contents/Frameworks/QtPrintSupport.framework/QtPrintSupport": "Versions/Current/QtPrintSupport",
    "86Box.app/Contents/Frameworks/QtPrintSupport.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtPrintSupport.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/libsndfile.1.dylib": "libsndfile.1.0.37.dylib",
    "86Box.app/Contents/Frameworks/QtGui.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtGui.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/QtGui.framework/QtGui": "Versions/Current/QtGui",
    "86Box.app/Contents/Frameworks/QtDBus.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtDBus.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/QtDBus.framework/QtDBus": "Versions/Current/QtDBus",
    "86Box.app/Contents/Frameworks/libjack.0.1.0.dylib": "libjack.0.dylib",
    "86Box.app/Contents/Frameworks/QtCore.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtCore.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/QtCore.framework/QtCore": "Versions/Current/QtCore",
    "86Box.app/Contents/Frameworks/libserialport.0.dylib": "libserialport.0.1.1.dylib",
    "86Box.app/Contents/Frameworks/libfluidsynth.3.dylib": "libfluidsynth.3.2.2.dylib",
    "86Box.app/Contents/Frameworks/QtOpenGL.framework/QtOpenGL": "Versions/Current/QtOpenGL",
    "86Box.app/Contents/Frameworks/QtOpenGL.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtOpenGL.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/libicudata.78.dylib": "libicudata.78.3.dylib",
    "86Box.app/Contents/Frameworks/QtWidgets.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtWidgets.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/QtWidgets.framework/QtWidgets": "Versions/Current/QtWidgets",
    "86Box.app/Contents/Frameworks/QtNetwork.framework/QtNetwork": "Versions/Current/QtNetwork",
    "86Box.app/Contents/Frameworks/QtNetwork.framework/Resources": "Versions/Current/Resources",
    "86Box.app/Contents/Frameworks/QtNetwork.framework/Versions/Current": "5",
    "86Box.app/Contents/Frameworks/libzstd.1.dylib": "libzstd.1.5.7.dylib",
    "86Box.app/Contents/Frameworks/libjpeg.8.dylib": "libjpeg.8.3.2.dylib",
    "86Box.app/Contents/Frameworks/libicui18n.78.dylib": "libicui18n.78.3.dylib",
    "86Box.app/Contents/Frameworks/libreadline.8.dylib": "libreadline.8.3.dylib",
    "86Box.app/Contents/Frameworks/librtmidi.7.dylib": "librtmidi.7.0.0.dylib",
    "86Box.app/Contents/Frameworks/libexpat.1.dylib": "libexpat.1.11.3.dylib",
    "86Box.app/Contents/Frameworks/libbz2.1.0.dylib": "libbz2.1.0.8.dylib",
    "86Box.app/Contents/Frameworks/libz.1.dylib": "libz.1.3.2.dylib",
    "86Box.app/Contents/Frameworks/libopenal.1.dylib": "libopenal.1.24.3.dylib",
    "86Box.app/Contents/Frameworks/libicuuc.78.dylib": "libicuuc.78.3.dylib",
    "86Box.app/Contents/Frameworks/libdouble-conversion.3.dylib": "libdouble-conversion.3.4.0.dylib",
}


@dataclasses.dataclass(frozen=True)
class TrustRecord:
    repository: str
    tag: str
    build: int
    source_commit: str
    archive_name: str
    archive_size: int
    archive_sha256: str
    archive_entries: int
    archive_uncompressed: int
    executable_path: str
    executable_sha256: str
    macho_arches: frozenset[str]
    symlinks: Mapping[str, str]


PRODUCTION_TRUST = TrustRecord(
    repository="86Box/86Box",
    tag="v6.0",
    build=9001,
    source_commit="4fef696a4eead1d55a28d6ac0e5bd2864e5454da",
    archive_name="86Box-macOS-x86_64+arm64-b9001.zip",
    archive_size=124_110_592,
    archive_sha256="fc66fc97225012af20145ae04193911bbf689fc75f89590774a904483140a5a9",
    archive_entries=778,
    archive_uncompressed=305_891_269,
    executable_path="86Box.app/Contents/MacOS/86Box",
    executable_sha256="764750187e52f643dc4d6e61ecaf517c64c3cd2e9225934eb1172aa733b3269b",
    macho_arches=frozenset({"x86_64", "arm64"}),
    symlinks=V6_SYMLINKS,
)


@dataclasses.dataclass(frozen=True)
class PrivateInputs:
    archive: Path
    roms: Path
    assets: Path
    installed_hdd: Path
    startup_floppy: Path
    install_iso: Path


@dataclasses.dataclass(frozen=True)
class Snapshot:
    role: str
    path: Path
    digest: str
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclasses.dataclass(frozen=True)
class Workspace:
    path: Path
    device: int
    inode: int
    uid: int
    nonce: str


@dataclasses.dataclass(frozen=True)
class Options:
    private_manifest: Path
    launch: bool = False
    source_vm_stopped: bool = False
    allow_full_copy: bool = False
    keep_temp: bool = False
    report: Path | None = None


@dataclasses.dataclass
class Hooks:
    validate_repository: bool = True
    platform_name: str | None = None
    try_clone: Callable[[Path, Path], bool] | None = None
    signature_verifier: Callable[[Path, Path], str] | None = None
    process_launcher: Callable[[Sequence[str], Path, Mapping[str, str], int, int], int] | None = None


class HarnessError(Exception):
    """A sanitized, user-actionable harness failure."""


class Interrupted(HarnessError):
    def __init__(self, signum: int):
        super().__init__("launch interrupted; disposable state was cleaned up")
        self.signum = signum


class ProcessGroupAlive(HarnessError):
    """The emulator process group could not be proven stopped."""


def fail(message: str) -> None:
    raise HarnessError(message)


def sha256_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(fd, CHUNK_SIZE):
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def open_directory_nofollow(path: Path) -> int:
    if not path.is_absolute() or any(part in ("", ".", "..") for part in path.parts[1:]):
        fail("a required path is not a normalized absolute path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open("/", flags)
        for component in path.parts[1:]:
            following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except OSError:
        with contextlib.suppress(UnboundLocalError, OSError):
            os.close(current)
        fail("a required private path has an unavailable or unsafe directory component")


def open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    if not path.is_absolute() or not path.name or any(part in ("", ".", "..") for part in path.parts[1:]):
        fail("a required path is not a normalized absolute path")
    parent_fd = open_directory_nofollow(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
    except OSError:
        os.close(parent_fd)
        fail("a required private input is unavailable or unsafe")
    os.close(parent_fd)
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode):
        os.close(fd)
        fail("a required private input is not a regular file")
    return fd, details


def snapshot_file(role: str, path: Path) -> Snapshot:
    fd, before = open_regular_nofollow(path)
    try:
        digest = sha256_fd(fd)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        fail("a private input changed while it was being verified")
    return Snapshot(role, path, digest, before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)


def snapshot_matches(original: Snapshot) -> bool:
    try:
        current = snapshot_file(original.role, original.path)
    except HarnessError:
        return False
    return (
        current.digest == original.digest
        and current.device == original.device
        and current.inode == original.inode
        and current.size == original.size
        and current.mtime_ns == original.mtime_ns
    )


def reject_ci_launch(environment: Mapping[str, str]) -> None:
    if any(environment.get(name) for name in CI_MARKERS):
        fail("launch is disabled in CI environments")


def require_plain_absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail("private manifest paths must be nonempty strings without control characters")
    path = Path(value)
    if not path.is_absolute() or any(part in ("", ".", "..") for part in path.parts[1:]):
        fail("private manifest paths must be normalized absolute paths")
    return path


def load_private_manifest(path: Path) -> PrivateInputs:
    try:
        details = path.lstat()
    except OSError:
        fail("private manifest is unavailable")
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600 or details.st_uid != os.getuid():
        fail("private manifest must be a nonsymlink regular file owned by the current user with mode 0600")
    fd, opened = open_regular_nofollow(path)
    if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
        os.close(fd)
        fail("private manifest changed while being opened")
    try:
        if opened.st_size > 64 * 1024:
            fail("private manifest is unexpectedly large")
        raw = b""
        while chunk := os.read(fd, 8192):
            raw += chunk
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        fail("private manifest changed while being read")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        fail("private manifest is not valid TOML")
    if set(data) != PRIVATE_FIELDS or data.get("schema_version") != 1:
        fail("private manifest must use the documented schema exactly")
    paths = {field: require_plain_absolute_path(data[field]) for field in PRIVATE_FIELDS - {"schema_version"}}
    if len(set(paths.values())) != len(paths):
        fail("private manifest paths must be distinct")
    return PrivateInputs(**paths)


def require_directory(path: Path) -> None:
    try:
        fd = open_directory_nofollow(path)
        details = os.fstat(fd)
    finally:
        with contextlib.suppress(UnboundLocalError, OSError):
            os.close(fd)
    if not stat.S_ISDIR(details.st_mode):
        fail("a required private dependency must be a nonsymlink directory tree")


def load_recipe() -> dict[str, object]:
    try:
        with (MACHINE_ROOT / "recipe.toml").open("rb") as recipe_file:
            return tomllib.load(recipe_file)
    except (OSError, tomllib.TOMLDecodeError):
        fail("public recipe is unavailable or invalid")


def validate_repository() -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(REPOSITORY_ROOT / "scripts" / "validate-recipes.py")],
        cwd=REPOSITORY_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "LANG": "C"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        fail("public recipe validation failed")


def sanitize_machine_config(source: bytes) -> bytes:
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        parser.read_string(source.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error):
        fail("public machine configuration is invalid")
    if parser.defaults():
        fail("public machine configuration must not use DEFAULT inheritance")
    network_sections = [section for section in parser.sections() if unicodedata.normalize("NFKC", section).casefold() == "network"]
    if any(section != "Network" for section in network_sections) or len(network_sections) > 1:
        fail("public machine configuration contains a case-confusable Network section")
    if parser.has_section("Network"):
        parser.remove_section("Network")
    media_section = "Floppy and CD-ROM drives"
    if not parser.has_section(media_section):
        fail("public machine configuration lacks its removable-media section")
    parser.set(media_section, "fdd_01_fn", "media/startup-floppy.img")
    parser.set(media_section, "cdrom_01_image_path", "media/install.iso")
    output = io.StringIO()
    parser.write(output, space_around_delimiters=True)
    rendered = output.getvalue().encode("utf-8")

    assert_disposable_config(rendered)
    return rendered


def assert_disposable_config(rendered: bytes) -> None:
    check = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    check.optionxform = str
    try:
        check.read_string(rendered.decode("utf-8"))
    except configparser.Error:
        fail("disposable machine configuration did not reparse")
    if check.defaults() or any(unicodedata.normalize("NFKC", section).casefold() == "network" for section in check.sections()):
        fail("disposable machine configuration still contains guest NIC settings")
    for section in check.sections():
        if any(key.casefold().startswith("net_") for key in check[section]):
            fail("disposable machine configuration still contains guest NIC settings")
    media_section = "Floppy and CD-ROM drives"
    if not check.has_section(media_section):
        fail("disposable machine configuration lacks removable-media settings")
    if (
        check.get(media_section, "fdd_01_fn", fallback=None) != "media/startup-floppy.img"
        or check.get(media_section, "cdrom_01_image_path", fallback=None) != "media/install.iso"
    ):
        fail("disposable machine configuration does not use staged removable media")


def verify_staged_config(path: Path, expected: bytes) -> None:
    fd, before = open_regular_nofollow(path)
    try:
        actual = b""
        while chunk := os.read(fd, 8192):
            actual += chunk
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or actual != expected:
        fail("disposable machine configuration changed after staging")
    assert_disposable_config(actual)


def validate_private_inputs(inputs: PrivateInputs, trust: TrustRecord) -> tuple[dict[str, Snapshot], bytes, dict[str, object]]:
    if inputs.archive.name != trust.archive_name:
        fail("private archive is not the allowlisted official release asset")
    require_directory(inputs.roms)
    require_directory(inputs.assets)
    recipe = load_recipe()
    requirements = recipe.get("requirements")
    if not isinstance(requirements, dict):
        fail("public recipe requirements are invalid")
    for relative in requirements.get("rom_subdirectories", []):
        require_directory(inputs.roms / str(relative))
    for relative in requirements.get("asset_files", []):
        marker = inputs.assets / str(relative)
        fd, _ = open_regular_nofollow(marker)
        os.close(fd)

    snapshots: dict[str, Snapshot] = {}
    try:
        snapshots["archive"] = snapshot_file("archive", inputs.archive)
        snapshots["installed_hdd"] = snapshot_file("installed_hdd", inputs.installed_hdd)
        snapshots["startup_floppy"] = snapshot_file("startup_floppy", inputs.startup_floppy)
        snapshots["install_iso"] = snapshot_file("install_iso", inputs.install_iso)
        if snapshots["archive"].size != trust.archive_size or snapshots["archive"].digest != trust.archive_sha256:
            fail("private archive does not match the allowlisted official release asset")
        disks = recipe.get("hard_disks")
        if not isinstance(disks, list) or len(disks) != 1 or not isinstance(disks[0], dict):
            fail("public recipe hard-disk declaration is unsupported")
        if snapshots["installed_hdd"].size != disks[0].get("bytes"):
            fail("installed HDD size does not match the public recipe")
        if snapshots["startup_floppy"].size != 1_474_560:
            fail("startup floppy must be an exact 1.44 MB image")
        if snapshots["install_iso"].size <= 0:
            fail("installation ISO must not be empty")

        config_path = MACHINE_ROOT / str(recipe.get("machine_config"))
        config_bytes = config_path.read_bytes()
        sanitized = sanitize_machine_config(config_bytes)
        return snapshots, sanitized, recipe
    except BaseException:
        if any(not snapshot_matches(snapshot) for snapshot in snapshots.values()):
            fail("a private source changed during acceptance processing")
        raise


def create_workspace() -> Workspace:
    path = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)).resolve()
    try:
        os.chmod(path, 0o700)
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o700 or details.st_uid != os.getuid():
            fail("could not create a private disposable workspace")
        nonce = secrets.token_hex(32)
        sentinel = path / SENTINEL_NAME
        fd = os.open(sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            write_all(fd, f"{SENTINEL_MAGIC}\n{nonce}\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return Workspace(path, details.st_dev, details.st_ino, details.st_uid, nonce)
    except BaseException:
        # This directory was just created by mkdtemp and is not yet exposed to
        # any child. Remove only its immediate sentinel and then the empty root.
        with contextlib.suppress(OSError):
            os.unlink(path / SENTINEL_NAME)
        with contextlib.suppress(OSError):
            os.rmdir(path)
        raise


def remove_open_tree(directory_fd: int) -> None:
    """Empty an already-open directory without re-following its root path."""
    for name in os.listdir(directory_fd):
        if name in (".", ".."):
            fail("refusing unsafe disposable-workspace cleanup")
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                    fail("refusing cleanup after a disposable directory changed")
                remove_open_tree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def cleanup_workspace(workspace: Workspace) -> None:
    path = workspace.path
    forbidden = {Path("/").resolve(), Path.home().resolve(), REPOSITORY_ROOT.resolve(), Path(tempfile.gettempdir()).resolve()}
    if not path.is_absolute() or path in forbidden or not path.name.startswith(WORKSPACE_PREFIX):
        fail("refusing unsafe disposable-workspace cleanup")
    parent_fd = open_directory_nofollow(path.parent)
    root_fd: int | None = None
    try:
        try:
            details = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            root_fd = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened_root = os.fstat(root_fd)
            sentinel_lstat = os.stat(SENTINEL_NAME, dir_fd=root_fd, follow_symlinks=False)
            sentinel_fd = os.open(SENTINEL_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
            try:
                sentinel_details = os.fstat(sentinel_fd)
                sentinel_bytes = os.read(sentinel_fd, 256)
                if os.read(sentinel_fd, 1):
                    raise OSError("oversized sentinel")
            finally:
                os.close(sentinel_fd)
            sentinel_value = sentinel_bytes.decode("ascii")
        except (OSError, UnicodeDecodeError):
            fail("refusing disposable-workspace cleanup because ownership metadata changed")
        expected = f"{SENTINEL_MAGIC}\n{workspace.nonce}\n"
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o700
            or (details.st_dev, details.st_ino, details.st_uid) != (workspace.device, workspace.inode, workspace.uid)
            or (opened_root.st_dev, opened_root.st_ino, opened_root.st_uid) != (workspace.device, workspace.inode, workspace.uid)
            or not stat.S_ISREG(sentinel_details.st_mode)
            or (sentinel_details.st_dev, sentinel_details.st_ino) != (sentinel_lstat.st_dev, sentinel_lstat.st_ino)
            or stat.S_IMODE(sentinel_details.st_mode) != 0o600
            or sentinel_details.st_uid != workspace.uid
            or sentinel_value != expected
        ):
            fail("refusing disposable-workspace cleanup because ownership metadata changed")
        remove_open_tree(root_fd)
    except OSError:
        raise HarnessError("refusing cleanup after the disposable workspace changed") from None
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def copy_public_inputs(machine_dir: Path, sanitized_config: bytes, recipe: Mapping[str, object]) -> None:
    machine_dir.mkdir(mode=0o700)
    allowlist = {
        "recipe.toml": 0o600,
        str(recipe["global_config"]): 0o600,
        str(recipe["launchers"]["macos"]): 0o700,
    }
    for relative, mode in allowlist.items():
        source = MACHINE_ROOT / relative
        destination = machine_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with source.open("rb") as source_file:
            data = source_file.read()
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
        try:
            write_all(fd, data)
        finally:
            os.close(fd)
    config_destination = machine_dir / str(recipe["machine_config"])
    fd = os.open(config_destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        write_all(fd, sanitized_config)
    finally:
        os.close(fd)
    for directory in ("disks", "media", "nvr", "printer", "screenshots"):
        (machine_dir / directory).mkdir(mode=0o700)


def default_try_clone(source: Path, destination: Path) -> bool:
    if sys.platform == "darwin":
        command = ["/bin/cp", "-c", str(source), str(destination)]
    elif sys.platform.startswith("linux"):
        command = ["/bin/cp", "--reflink=always", "--sparse=always", str(source), str(destination)]
    else:
        return False
    result = subprocess.run(command, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def full_copy(source: Path, destination: Path, expected_size: int) -> None:
    free = shutil.disk_usage(destination.parent).free
    if free < expected_size + 64 * 1024 * 1024:
        fail("insufficient free space for an explicitly allowed full media copy")
    source_fd, before = open_regular_nofollow(source)
    destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        while chunk := os.read(source_fd, CHUNK_SIZE):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        after = os.fstat(source_fd)
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        fail("a private input changed during full copy")


def stage_medium(snapshot: Snapshot, destination: Path, allow_full_copy: bool, clone: Callable[[Path, Path], bool]) -> None:
    if destination.exists() or destination.is_symlink():
        fail("disposable media destination unexpectedly exists")
    cloned = clone(snapshot.path, destination)
    if not cloned:
        with contextlib.suppress(OSError):
            destination.unlink()
        if not allow_full_copy:
            fail("copy-on-write cloning is unavailable; rerun with --allow-full-copy only if sufficient private storage is available")
        full_copy(snapshot.path, destination, snapshot.size)
    try:
        destination_details = destination.lstat()
    except OSError:
        fail("disposable media copy was not created")
    if not stat.S_ISREG(destination_details.st_mode) or (destination_details.st_dev, destination_details.st_ino) == (snapshot.device, snapshot.inode):
        fail("disposable media must be a distinct regular file")
    os.chmod(destination, 0o600)
    copied = snapshot_file(snapshot.role, destination)
    if copied.size != snapshot.size or copied.digest != snapshot.digest or not snapshot_matches(snapshot):
        fail("disposable media copy failed integrity verification")


def safe_archive_member(name: str) -> PurePosixPath:
    if (
        not name
        or "\\" in name
        or len(name) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        fail("trusted archive contains an unsafe member name")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts) or relative.parts[0] != "86Box.app":
        fail("trusted archive contains an unsafe member path")
    return relative


def extract_trusted_archive(snapshot: Snapshot, destination: Path, trust: TrustRecord) -> Path:
    fd, before = open_regular_nofollow(snapshot.path)
    if before.st_size != trust.archive_size or sha256_fd(fd) != trust.archive_sha256:
        os.close(fd)
        fail("trusted archive changed before extraction")
    archive_file = os.fdopen(os.dup(fd), "rb")
    try:
        with zipfile.ZipFile(archive_file) as archive:
            infos = archive.infolist()
            if len(infos) != trust.archive_entries or len(infos) > MAX_ARCHIVE_ENTRIES:
                fail("trusted archive layout differs from the reviewed release")
            total = sum(info.file_size for info in infos)
            if total != trust.archive_uncompressed or total > MAX_ARCHIVE_TOTAL:
                fail("trusted archive size layout differs from the reviewed release")
            normalized: set[str] = set()
            directories: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            links: dict[str, tuple[zipfile.ZipInfo, PurePosixPath, str]] = {}
            for info in infos:
                relative = safe_archive_member(info.filename.rstrip("/") if info.is_dir() else info.filename)
                key = unicodedata.normalize("NFC", relative.as_posix()).casefold()
                if key in normalized:
                    fail("trusted archive contains colliding member paths")
                normalized.add(key)
                if (
                    info.flag_bits & 0x1
                    or info.file_size > MAX_ARCHIVE_MEMBER
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    fail("trusted archive contains an unsupported member")
                if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                    fail("trusted archive exceeds the reviewed compression bounds")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind == stat.S_IFLNK:
                    if info.is_dir():
                        fail("trusted archive contains an inconsistent symbolic link")
                    try:
                        target = archive.read(info).decode("utf-8")
                    except (UnicodeDecodeError, RuntimeError, zipfile.BadZipFile):
                        fail("trusted archive contains an invalid symbolic link")
                    links[relative.as_posix()] = (info, relative, target)
                elif info.is_dir() or kind == stat.S_IFDIR:
                    if kind not in (0, stat.S_IFDIR):
                        fail("trusted archive contains an inconsistent directory")
                    directories.append((info, relative))
                elif kind in (0, stat.S_IFREG):
                    files.append((info, relative))
                else:
                    fail("trusted archive contains a special file")
            actual_links = {name: value[2] for name, value in links.items()}
            if actual_links != dict(trust.symlinks):
                fail("trusted archive symbolic-link layout differs from the reviewed release")
            link_paths = {PurePosixPath(name) for name in links}
            for _, relative in directories + files:
                if any(parent in link_paths for parent in relative.parents):
                    fail("trusted archive member traverses an archived symbolic link")

            destination.mkdir(mode=0o700)
            for _, relative in sorted(directories, key=lambda item: len(item[1].parts)):
                target = destination.joinpath(*relative.parts)
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            for info, relative in files:
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                mode = 0o700 if (info.external_attr >> 16) & 0o111 else 0o600
                output_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
                try:
                    with archive.open(info, "r") as source:
                        while chunk := source.read(CHUNK_SIZE):
                            view = memoryview(chunk)
                            while view:
                                written = os.write(output_fd, view)
                                view = view[written:]
                finally:
                    os.close(output_fd)
            for name, (_, relative, target_value) in links.items():
                if (
                    not target_value
                    or len(target_value) > 512
                    or "\\" in target_value
                    or any(ord(char) < 32 or ord(char) == 127 for char in target_value)
                ):
                    fail("trusted archive contains an unsafe symbolic-link target")
                lexical_target = PurePosixPath(*relative.parts[:-1], target_value)
                if lexical_target.is_absolute() or any(part == ".." for part in lexical_target.parts):
                    fail("trusted archive symbolic link escapes its app bundle")
                link = destination.joinpath(*relative.parts)
                link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.symlink(target_value, link)
            root = destination.resolve()
            for name in links:
                try:
                    destination.joinpath(*PurePosixPath(name).parts).resolve(strict=True).relative_to(root)
                except (OSError, ValueError):
                    fail("trusted archive symbolic link does not resolve inside its app bundle")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        fail("trusted archive could not be safely extracted")
    finally:
        archive_file.close()
        after = os.fstat(fd)
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        fail("trusted archive changed during extraction")
    executable = destination.joinpath(*PurePosixPath(trust.executable_path).parts)
    executable_snapshot = snapshot_file("emulator", executable)
    if executable_snapshot.digest != trust.executable_sha256 or not os.access(executable, os.X_OK):
        fail("extracted emulator differs from the reviewed release")
    if trust.macho_arches and parse_macho_arches(executable) != trust.macho_arches:
        fail("extracted emulator architecture set differs from the reviewed release")
    return executable


def parse_macho_arches(path: Path) -> frozenset[str]:
    fd, details = open_regular_nofollow(path)
    try:
        header = os.read(fd, 8)
        if len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe":
            fail("extracted emulator is not the reviewed universal Mach-O format")
        count = struct.unpack(">I", header[4:])[0]
        if count != 2:
            fail("extracted emulator has an unexpected Mach-O architecture count")
        entries = os.read(fd, 20 * count)
    finally:
        os.close(fd)
    if len(entries) != 20 * count:
        fail("extracted emulator has a truncated Mach-O header")
    names = {0x01000007: "x86_64", 0x0100000C: "arm64"}
    arches: set[str] = set()
    ranges: list[tuple[int, int]] = []
    for index in range(count):
        cpu_type, _, offset, size, _ = struct.unpack(">IIIII", entries[index * 20 : (index + 1) * 20])
        if cpu_type not in names or offset + size > details.st_size or size == 0:
            fail("extracted emulator has an invalid Mach-O architecture table")
        arches.add(names[cpu_type])
        ranges.append((offset, offset + size))
    if len(arches) != count or max(ranges[0][0], ranges[1][0]) < min(ranges[0][1], ranges[1][1]):
        fail("extracted emulator has overlapping or duplicate Mach-O architectures")
    return frozenset(arches)


def verify_known_invalid_signature(app: Path, executable: Path) -> str:
    commands = (
        (["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", "--verbose=4", str(app)], "arm64"),
        (["/usr/bin/codesign", "--verify", "--strict", "--verbose=4", "--arch", "x86_64", str(executable)], "x86_64"),
        (["/usr/bin/codesign", "--verify", "--strict", "--verbose=4", "--arch", "arm64", str(executable)], "arm64"),
    )
    for command, expected_architecture in commands:
        result = subprocess.run(
            command,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "LANG": "C"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        lines = result.stderr.splitlines()
        if (
            result.returncode != 1
            or result.stdout
            or len(lines) != 2
            or not lines[0].endswith(": invalid signature (code or signature have been modified)")
            or lines[1] != f"In architecture: {expected_architecture}"
        ):
            fail("Apple signature result differs from the reviewed known-invalid release outcome")
    return "known_invalid"


def terminate_group(process: subprocess.Popen[object], grace_seconds: int = 5) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.05)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS can transiently report EPERM for an orphaned group while
            # its last killed member is being reaped; only ESRCH proves exit.
            pass
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + max(1.0, min(float(grace_seconds), 5.0))
    while time.monotonic() < kill_deadline:
        if process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.05)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.05)
    raise ProcessGroupAlive("emulator process group did not stop; disposable workspace was retained")


def launch_process(argv: Sequence[str], workspace: Path, environment: Mapping[str, str], stdout_fd: int, stderr_fd: int) -> int:
    process: subprocess.Popen[object] | None = None
    interrupted: int | None = None
    previous: dict[int, object] = {}

    def handle(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = signum
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, handle)
    status: int | None = None
    try:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=workspace,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout_fd,
                stderr=stderr_fd,
                start_new_session=True,
                close_fds=True,
            )
            while process.poll() is None:
                if interrupted is not None:
                    break
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            status = int(process.returncode) if process.returncode is not None else None
        finally:
            if process is not None:
                terminate_group(process)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if interrupted is not None:
        raise Interrupted(interrupted)
    if status is None:
        fail("emulator process status was unavailable")
    return status


def public_hashes() -> tuple[str, str]:
    recipe = hashlib.sha256((MACHINE_ROOT / "recipe.toml").read_bytes()).hexdigest()
    config = hashlib.sha256((MACHINE_ROOT / "86box.cfg").read_bytes()).hexdigest()
    return recipe, config


def build_report(trust: TrustRecord, launch_state: str) -> dict[str, object]:
    recipe_hash, config_hash = public_hashes()
    machine = platform.machine().lower()
    coarse_machine = machine if machine in {"arm64", "aarch64", "x86_64", "amd64"} else "other"
    return {
        "schema_version": 1,
        "recipe_id": MACHINE_ID,
        "recipe_sha256": recipe_hash,
        "machine_config_sha256": config_hash,
        "emulator": {
            "repository": trust.repository,
            "tag": trust.tag,
            "build": trust.build,
            "source_commit": trust.source_commit,
            "apple_signature_status": "known_invalid",
        },
        "platform": {"system": platform.system(), "machine": coarse_machine},
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "preflight_state": "passed",
        "launch_state": launch_state,
        "no_guest_nic_configured": True,
        "guest_checks": {
            "bios_post": "not_observed",
            "install_cd_drive_d": "not_observed",
            "windows_desktop": "not_observed",
            "wheel_mouse": "not_observed",
            "printer_test_page": "not_observed",
            "soft_reboot_1": "not_observed",
            "soft_reboot_2": "not_observed",
            "soft_reboot_3": "not_observed",
        },
    }


def write_report_no_clobber(path: Path, report: Mapping[str, object]) -> None:
    destination = path if path.is_absolute() else Path.cwd() / path
    parent = destination.parent
    try:
        parent_details = parent.lstat()
    except OSError:
        fail("report destination directory is unavailable")
    if not stat.S_ISDIR(parent_details.st_mode) or destination.name in ("", ".", ".."):
        fail("report destination must be a new file in an existing directory")
    payload = (json.dumps(report, sort_keys=True, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")
    try:
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        fail("report destination directory is unavailable or unsafe")
    temporary_name = f".{destination.name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
        dir_fd=directory_fd,
    )
    try:
        write_all(fd, payload)
        os.fsync(fd)
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            fail("report destination already exists")
        except OSError:
            fail("report could not be published atomically")
        os.fsync(directory_fd)
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)


def run(options: Options, trust: TrustRecord = PRODUCTION_TRUST, hooks: Hooks | None = None) -> int:
    hooks = hooks or Hooks()
    if options.launch:
        reject_ci_launch(os.environ)
        if not options.source_vm_stopped:
            fail("--launch requires --source-vm-stopped before copying the installed HDD")
        if (hooks.platform_name or platform.system()) != "Darwin" and trust is PRODUCTION_TRUST:
            fail("the production launch path supports macOS only")
    if hooks.validate_repository:
        validate_repository()
    lifecycle_signal: int | None = None
    finalizing = False
    previous_handlers: dict[int, object] = {}

    def handle_lifecycle_signal(signum: int, _frame: object) -> None:
        nonlocal lifecycle_signal
        lifecycle_signal = signum
        if not finalizing:
            raise Interrupted(signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, handle_lifecycle_signal)

    snapshots: dict[str, Snapshot] = {}
    launch_state = "not_requested"
    exit_code = 0
    workspace: Workspace | None = None
    try:
        inputs = load_private_manifest(options.private_manifest)
        snapshots, sanitized_config, recipe = validate_private_inputs(inputs, trust)
        if options.launch:
            workspace = create_workspace()
            machine_dir = workspace.path / "machine"
            copy_public_inputs(machine_dir, sanitized_config, recipe)
            clone = hooks.try_clone or default_try_clone
            stage_medium(snapshots["installed_hdd"], machine_dir / "disks" / "windows95.hdd", options.allow_full_copy, clone)
            stage_medium(snapshots["startup_floppy"], machine_dir / "media" / "startup-floppy.img", options.allow_full_copy, clone)
            stage_medium(snapshots["install_iso"], machine_dir / "media" / "install.iso", options.allow_full_copy, clone)
            archive_root = workspace.path / "emulator"
            executable = extract_trusted_archive(snapshots["archive"], archive_root, trust)
            signature_verifier = hooks.signature_verifier or verify_known_invalid_signature
            if signature_verifier(archive_root / "86Box.app", executable) != "known_invalid":
                fail("Apple signature result differs from the reviewed known-invalid release outcome")
            if snapshot_file("emulator", executable).digest != trust.executable_sha256:
                fail("extracted emulator changed before launch")
            verify_staged_config(machine_dir / str(recipe["machine_config"]), sanitized_config)

            home = workspace.path / "home"
            tmp = workspace.path / "tmp"
            home.mkdir(mode=0o700)
            tmp.mkdir(mode=0o700)
            environment = {
                "HOME": str(home),
                "TMPDIR": str(tmp),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C",
                "LC_ALL": "C",
            }
            argv = [
                str(executable),
                "--vmpath",
                str(machine_dir),
                "--global",
                str(machine_dir / str(recipe["global_config"])),
                "--rompath",
                str(inputs.roms),
                "--assetpath",
                str(inputs.assets),
                "--vmname",
                str(recipe["name"]),
            ]
            stdout_path = workspace.path / "emulator.stdout.log"
            stderr_path = workspace.path / "emulator.stderr.log"
            stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                launcher = hooks.process_launcher or launch_process
                process_status = launcher(argv, workspace.path, environment, stdout_fd, stderr_fd)
            finally:
                os.close(stdout_fd)
                os.close(stderr_fd)
            launch_state = "clean_process_exit" if process_status == 0 else "process_error"
            exit_code = 0 if process_status == 0 else 1
    finally:
        finalizing = True
        active_error = sys.exc_info()[1]
        active_interruption = isinstance(active_error, Interrupted)
        unsafe_process_group = isinstance(active_error, ProcessGroupAlive)
        try:
            source_changed = any(not snapshot_matches(snapshot) for snapshot in snapshots.values())
            if workspace is not None:
                if unsafe_process_group:
                    print(f"Disposable workspace retained at {workspace.path} because emulator children may still be running.", file=sys.stderr)
                elif options.keep_temp and not active_interruption and lifecycle_signal is None:
                    print(f"Private disposable workspace retained at {workspace.path}; do not publish its contents.", file=sys.stderr)
                else:
                    cleanup_workspace(workspace)
            if source_changed:
                fail("a private source changed during acceptance processing")
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        if lifecycle_signal is not None and not active_interruption and not unsafe_process_group:
            raise Interrupted(lifecycle_signal)
    if options.report is not None:
        write_report_no_clobber(options.report, build_report(trust, launch_state))
    return exit_code


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-manifest", required=True, type=Path)
    parser.add_argument("--launch", action="store_true", help="explicitly launch the disposable VM")
    parser.add_argument("--source-vm-stopped", action="store_true", help="confirm the installed HDD is not in use")
    parser.add_argument("--allow-full-copy", action="store_true", help="allow capacity-checked full copies when COW cloning is unavailable")
    parser.add_argument("--keep-temp", action="store_true", help="retain the private disposable workspace for local diagnosis")
    parser.add_argument("--report", type=Path, help="atomically create a sanitized JSON report")
    values = parser.parse_args(argv)
    if values.keep_temp and not values.launch:
        parser.error("--keep-temp requires --launch")
    if values.source_vm_stopped and not values.launch:
        parser.error("--source-vm-stopped requires --launch")
    if values.allow_full_copy and not values.launch:
        parser.error("--allow-full-copy requires --launch")
    return Options(**vars(values))


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("private-acceptance.py requires Python 3.11 or newer", file=sys.stderr)
        return 2
    try:
        return run(parse_args(argv))
    except Interrupted as error:
        print(str(error), file=sys.stderr)
        return 128 + error.signum
    except HarnessError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("acceptance failed unexpectedly; private path details were suppressed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

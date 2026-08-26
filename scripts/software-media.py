#!/usr/bin/env python3
"""Validate, fetch, and assemble license-aware vintage software media."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "software" / "catalog"
BUNDLE_ROOT = ROOT / "software" / "bundles"
CHUNK = 1024 * 1024
SOURCE_DATE_EPOCH = 946684800
MAX_PRIVATE_ENTRIES = 50_000
MAX_PRIVATE_DEPTH = 32
CI_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "JENKINS_URL", "TF_BUILD")
COMPATIBILITY = {"period-authentic", "period-compatible", "modern-retro"}
DISTRIBUTION = {"redistributable", "reference-only", "user-supplied"}
KINDS = {"application", "operating_system", "collection"}
OUTPUTS = {"kit", "iso"}
SPDX_LICENSES = {
    "0BSD", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0-only", "GPL-3.0-or-later", "ISC", "LGPL-2.0-only", "LGPL-2.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "MIT", "MPL-2.0",
}
SPDX_EXCEPTIONS = {"Classpath-exception-2.0", "GCC-exception-2.0", "LLVM-exception"}
CATALOG_FIELDS = {
    "schema_version", "id", "title", "version", "kind", "compatibility", "distribution",
    "homepage", "license_expression", "license_basis", "license_evidence", "notice_location",
    "source_compliance", "security_notes", "personal_destination", "personal_source_type", "artifact",
}
ARTIFACT_FIELDS = {"id", "role", "url", "filename", "bytes", "sha256", "destination", "extract_single_iso"}
BUNDLE_FIELDS = {
    "schema_version", "id", "title", "output", "redistributable", "volume_id",
    "max_payload_bytes", "max_output_bytes", "items", "allow_private",
}
PRIVATE_FIELDS = {"schema_version", "item"}
PRIVATE_ITEM_FIELDS = {
    "id", "title", "version", "source_type", "path", "sha256", "destination", "license_acknowledged",
}


class MediaError(Exception):
    """A sanitized software-media failure."""


@dataclasses.dataclass(frozen=True)
class Artifact:
    id: str
    role: str
    url: str
    filename: str
    size: int
    sha256: str
    destination: str
    extract_single_iso: bool


@dataclasses.dataclass(frozen=True)
class CatalogItem:
    id: str
    title: str
    version: str
    kind: str
    compatibility: str
    distribution: str
    homepage: str
    license_expression: str
    license_basis: str
    license_evidence: str
    notice_location: str
    source_compliance: str
    security_notes: str
    personal_destination: str
    personal_source_type: str
    artifacts: tuple[Artifact, ...]


@dataclasses.dataclass(frozen=True)
class Bundle:
    id: str
    title: str
    output: str
    redistributable: bool
    volume_id: str
    max_payload_bytes: int
    max_output_bytes: int
    items: tuple[str, ...]
    allow_private: bool


@dataclasses.dataclass(frozen=True)
class PrivateItem:
    id: str
    title: str
    version: str
    source_type: str
    path: Path
    sha256: str
    destination: str


def fail(message: str) -> None:
    raise MediaError(message)


def plain_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail(f"{label} must be a nonempty plain string")
    return value


def identifier(value: object, label: str) -> str:
    text = plain_string(value, label)
    if not text[0].isalnum() or any(not (char.islower() or char.isdigit() or char == "-") for char in text):
        fail(f"{label} must use lowercase letters, digits, and hyphens")
    return text


def normalized_absolute(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute() or any(part in ("", ".", "..") for part in absolute.parts[1:]):
        fail(f"{label} must be a normalized path")
    return absolute


def license_expression(value: object) -> str:
    text = plain_string(value, "license expression")
    if text in {"NONE", "NOASSERTION"}:
        return text
    tokens = text.replace("(", " ( ").replace(")", " ) ").split()
    position = 0

    def primary() -> None:
        nonlocal position
        if position < len(tokens) and tokens[position] == "(":
            position += 1
            expression()
            if position >= len(tokens) or tokens[position] != ")":
                fail("license expression must use the supported SPDX grammar")
            position += 1
            return
        if position >= len(tokens) or tokens[position] not in SPDX_LICENSES:
            fail("license expression contains an unsupported SPDX identifier")
        position += 1
        if position < len(tokens) and tokens[position] == "WITH":
            position += 1
            if position >= len(tokens) or tokens[position] not in SPDX_EXCEPTIONS:
                fail("license expression contains an unsupported SPDX exception")
            position += 1

    def conjunction() -> None:
        nonlocal position
        primary()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            primary()

    def expression() -> None:
        nonlocal position
        conjunction()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            conjunction()

    expression()
    if position != len(tokens):
        fail("license expression must use the supported SPDX grammar")
    return text


def integer(value: object, label: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(f"{label} must be an integer of at least {minimum}")
    return value


def digest(value: object, label: str) -> str:
    text = plain_string(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        fail(f"{label} must be a lowercase SHA-256 digest")
    return text


def https_url(value: object, label: str) -> str:
    text = plain_string(value, label)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        fail(f"{label} must be a credential-free HTTPS URL")
    return text


def iso_component(value: str) -> bool:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    if value in ("", ".", "..") or len(value) > 12:
        return False
    stem, dot, extension = value.partition(".")
    if not stem or len(stem) > 8 or (dot and (not extension or len(extension) > 3 or "." in extension)):
        return False
    return all(char in allowed for char in stem + extension)


def iso_path(value: object, label: str) -> str:
    text = plain_string(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(not iso_component(part) for part in path.parts):
        fail(f"{label} must be a relative uppercase ISO 9660 8.3 path")
    return str(path)


def load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as source:
            value = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError):
        fail("a software metadata file is unavailable or invalid")
    if not isinstance(value, dict):
        fail("software metadata must be a TOML table")
    return value


def load_catalog(path: Path) -> CatalogItem:
    data = load_toml(path)
    if set(data) != CATALOG_FIELDS or data.get("schema_version") != 1:
        fail("catalog entry must use the documented schema exactly")
    item_id = identifier(data["id"], "catalog id")
    if path.stem != item_id:
        fail("catalog id must match its filename")
    kind = plain_string(data["kind"], "catalog kind")
    compatibility = plain_string(data["compatibility"], "compatibility")
    distribution = plain_string(data["distribution"], "distribution")
    if kind not in KINDS or compatibility not in COMPATIBILITY or distribution not in DISTRIBUTION:
        fail("catalog enum value is unsupported")
    license_value = license_expression(data["license_expression"])
    if distribution == "redistributable" and license_value == "NONE":
        fail("redistributable entries require license information")
    artifact_data = data["artifact"]
    if not isinstance(artifact_data, list) or (distribution == "redistributable" and not artifact_data):
        fail("redistributable entries require artifacts")
    if distribution != "redistributable" and artifact_data:
        fail("reference-only and user-supplied entries cannot declare downloadable artifacts")
    personal_source_type = plain_string(data["personal_source_type"], "personal source type")
    if personal_source_type not in {"file", "directory"}:
        fail("personal source type must be file or directory")
    artifacts: list[Artifact] = []
    seen_ids: set[str] = set()
    seen_destinations: set[str] = set()
    for raw in artifact_data:
        if not isinstance(raw, dict) or set(raw) != ARTIFACT_FIELDS:
            fail("catalog artifact must use the documented schema exactly")
        artifact_id = identifier(raw["id"], "artifact id")
        destination = iso_path(raw["destination"], "artifact destination")
        if artifact_id in seen_ids or destination.casefold() in seen_destinations:
            fail("catalog artifact ids and destinations must be unique")
        seen_ids.add(artifact_id)
        seen_destinations.add(destination.casefold())
        filename = plain_string(raw["filename"], "artifact filename")
        if Path(filename).name != filename or filename in (".", ".."):
            fail("artifact filename must be a single safe component")
        extract = raw["extract_single_iso"]
        if not isinstance(extract, bool):
            fail("extract_single_iso must be a boolean")
        artifacts.append(Artifact(
            artifact_id,
            plain_string(raw["role"], "artifact role"),
            https_url(raw["url"], "artifact URL"),
            filename,
            integer(raw["bytes"], "artifact size"),
            digest(raw["sha256"], "artifact digest"),
            destination,
            extract,
        ))
    return CatalogItem(
        item_id,
        plain_string(data["title"], "catalog title"),
        plain_string(data["version"], "catalog version"),
        kind,
        compatibility,
        distribution,
        https_url(data["homepage"], "catalog homepage"),
        license_value,
        plain_string(data["license_basis"], "license basis"),
        https_url(data["license_evidence"], "license evidence"),
        plain_string(data["notice_location"], "notice location"),
        plain_string(data["source_compliance"], "source compliance"),
        plain_string(data["security_notes"], "security notes"),
        iso_path(data["personal_destination"], "personal destination"),
        personal_source_type,
        tuple(artifacts),
    )


def load_bundle(path: Path) -> Bundle:
    data = load_toml(path)
    if set(data) != BUNDLE_FIELDS or data.get("schema_version") != 1:
        fail("bundle must use the documented schema exactly")
    bundle_id = identifier(data["id"], "bundle id")
    if path.stem != bundle_id:
        fail("bundle id must match its filename")
    output = plain_string(data["output"], "bundle output")
    if output not in OUTPUTS:
        fail("bundle output is unsupported")
    redistributable = data["redistributable"]
    allow_private = data["allow_private"]
    if not isinstance(redistributable, bool) or not isinstance(allow_private, bool):
        fail("bundle policy flags must be booleans")
    if redistributable and allow_private:
        fail("a redistributable bundle cannot accept private items")
    volume_id = plain_string(data["volume_id"], "volume id")
    if len(volume_id) > 32 or volume_id != volume_id.upper() or any(not (c.isupper() or c.isdigit() or c == "_") for c in volume_id):
        fail("volume id must be at most 32 uppercase ISO characters")
    raw_items = data["items"]
    if not isinstance(raw_items, list) or any(not isinstance(value, str) for value in raw_items):
        fail("bundle items must be an array of catalog ids")
    items = tuple(identifier(value, "bundle item") for value in raw_items)
    if len(set(items)) != len(items):
        fail("bundle items must be unique")
    max_payload = integer(data["max_payload_bytes"], "bundle payload size")
    max_output = integer(data["max_output_bytes"], "bundle output size")
    if max_payload > max_output:
        fail("bundle payload size cannot exceed its output size")
    return Bundle(bundle_id, plain_string(data["title"], "bundle title"), output, redistributable, volume_id,
                  max_payload, max_output, items, allow_private)


def load_all() -> tuple[dict[str, CatalogItem], dict[str, Bundle]]:
    catalog = {item.id: item for item in (load_catalog(path) for path in sorted(CATALOG_ROOT.glob("*.toml")))}
    bundles = {bundle.id: bundle for bundle in (load_bundle(path) for path in sorted(BUNDLE_ROOT.glob("*.toml")))}
    if not catalog or not bundles:
        fail("software catalog and bundles must not be empty")
    for bundle in bundles.values():
        for item_id in bundle.items:
            if item_id not in catalog:
                fail("bundle references an unknown catalog item")
            item = catalog[item_id]
            if bundle.redistributable and item.distribution != "redistributable":
                fail("redistributable bundle references a non-redistributable item")
    return catalog, bundles


def open_directory(path: Path) -> int:
    if not path.is_absolute() or any(part in ("", ".", "..") for part in path.parts[1:]):
        fail("private path must be normalized and absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except OSError:
        os.close(current)
        fail("private path has an unavailable or unsafe ancestor")


def open_regular(path: Path) -> tuple[int, os.stat_result]:
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    except OSError:
        os.close(parent)
        fail("private input is unavailable or unsafe")
    os.close(parent)
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode):
        os.close(fd)
        fail("private input must be a regular file")
    return fd, details


def sha256_fd(fd: int) -> str:
    value = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, CHUNK):
        value.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return value.hexdigest()


def create_private_file(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError:
        fail("a private temporary file could not be created")


def load_private(path: Path) -> tuple[PrivateItem, ...]:
    try:
        details = path.lstat()
    except OSError:
        fail("private manifest is unavailable")
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600 or details.st_uid != os.getuid():
        fail("private manifest must be a nonsymlink file owned by the current user with mode 0600")
    fd, opened = open_regular(path)
    try:
        if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino) or opened.st_size > 128 * 1024:
            fail("private manifest changed or is unexpectedly large")
        raw = b""
        while chunk := os.read(fd, 8192):
            raw += chunk
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        fail("private manifest changed while being read")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        fail("private manifest is invalid TOML")
    if set(data) != PRIVATE_FIELDS or data.get("schema_version") != 1 or not isinstance(data["item"], list):
        fail("private manifest must use the documented schema exactly")
    result: list[PrivateItem] = []
    destinations: set[str] = set()
    item_ids: set[str] = set()
    for raw_item in data["item"]:
        if not isinstance(raw_item, dict) or set(raw_item) != PRIVATE_ITEM_FIELDS or raw_item["license_acknowledged"] is not True:
            fail("private item must use the documented schema and acknowledge its license")
        raw_path = plain_string(raw_item["path"], "private item path")
        item_path = Path(raw_path)
        if not item_path.is_absolute() or any(part in ("", ".", "..") for part in item_path.parts[1:]):
            fail("private item path must be normalized and absolute")
        destination = iso_path(raw_item["destination"], "private destination")
        source_type = plain_string(raw_item["source_type"], "private source type")
        if source_type not in {"file", "directory"}:
            fail("private source type must be file or directory")
        if destination.casefold() in destinations:
            fail("private destinations must be unique")
        destinations.add(destination.casefold())
        item_id = identifier(raw_item["id"], "private item id")
        if item_id in item_ids:
            fail("private item ids must be unique")
        item_ids.add(item_id)
        result.append(PrivateItem(item_id, plain_string(raw_item["title"], "private title"),
                                  plain_string(raw_item["version"], "private version"), source_type, item_path,
                                  digest(raw_item["sha256"], "private digest"), destination))
    if not result:
        fail("private manifest must contain at least one item")
    return tuple(result)


def hash_path(path: Path, expected_size: int, expected_hash: str) -> None:
    fd, details = open_regular(normalized_absolute(path, "artifact path"))
    try:
        actual = sha256_fd(fd)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if details.st_size != expected_size or actual != expected_hash or (details.st_dev, details.st_ino, details.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
        fail("artifact size, digest, or identity does not match its catalog")


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: urllib.request.Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme != "https":
            fail("artifact download attempted a non-HTTPS redirect")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_artifact(artifact: Artifact, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_fd = open_directory(cache)
    os.close(cache_fd)
    destination = cache / artifact.filename
    if destination.exists():
        hash_path(destination, artifact.size, artifact.sha256)
        return destination
    temporary = cache / f".{artifact.filename}.{secrets.token_hex(12)}.tmp"
    request = urllib.request.Request(artifact.url, headers={"User-Agent": "86box-vm-recipes/1 software-media"})
    opener = urllib.request.build_opener(HttpsOnly())
    total = 0
    digest_value = hashlib.sha256()
    try:
        with opener.open(request, timeout=60) as response:
            output_fd = create_private_file(temporary)
            with os.fdopen(output_fd, "wb") as output:
                while chunk := response.read(CHUNK):
                    total += len(chunk)
                    if total > artifact.size:
                        fail("download exceeded its catalog size")
                    digest_value.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if total != artifact.size or digest_value.hexdigest() != artifact.sha256:
            fail("download does not match its catalog size and digest")
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            hash_path(destination, artifact.size, artifact.sha256)
        return destination
    except (OSError, urllib.error.URLError):
        fail("artifact download failed")
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def copy_verified(source: Path, destination: Path, maximum_size: int, expected_hash: str | None = None) -> tuple[int, str]:
    fd, before = open_regular(normalized_absolute(source, "source path"))
    if before.st_size > maximum_size:
        os.close(fd)
        fail("source exceeds the remaining bundle capacity")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        os.close(fd)
        fail("staged destination could not be created safely")
    digest_value = hashlib.sha256()
    size = 0
    try:
        while chunk := os.read(fd, CHUNK):
            digest_value.update(chunk)
            size += len(chunk)
            if size > maximum_size:
                fail("source exceeds the remaining bundle capacity")
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                view = view[written:]
        os.fsync(output_fd)
        after = os.fstat(fd)
    finally:
        os.close(output_fd)
        os.close(fd)
    actual = digest_value.hexdigest()
    if expected_hash is not None and actual != expected_hash:
        fail("private input digest does not match its manifest")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        fail("source changed while media was assembled")
    os.utime(destination, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
    return size, actual


def process_directory(source: Path, destination: Path | None, maximum_size: int,
                      expected_hash: str | None = None) -> tuple[int, str]:
    source = normalized_absolute(source, "directory source path")
    root_fd = open_directory(source)
    root_before = os.fstat(root_fd)
    tree_hash = hashlib.sha256()
    total = 0
    entry_count = 0

    def visit(directory_fd: int, relative: tuple[str, ...]) -> None:
        nonlocal entry_count, total
        if len(relative) > MAX_PRIVATE_DEPTH:
            fail("private directory exceeds the supported nesting depth")
        try:
            entries = []
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    entry_count += 1
                    if entry_count > MAX_PRIVATE_ENTRIES:
                        fail("private directory contains too many entries")
                    entries.append(entry)
            entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError:
            fail("private directory could not be read safely")
        folded: set[str] = set()
        for entry in entries:
            name = plain_string(entry.name, "private directory entry")
            iso_name = name.upper()
            if not iso_component(iso_name) or iso_name.casefold() in folded:
                fail("private directory entries must map uniquely to ISO 9660 8.3 names")
            folded.add(iso_name.casefold())
            child_relative = (*relative, iso_name)
            encoded_path = "/".join(child_relative).encode("ascii")
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError:
                fail("private directory entry changed or became unavailable")
            if stat.S_ISDIR(details.st_mode):
                tree_hash.update(b"D\0" + encoded_path + b"\0")
                if destination is not None:
                    destination.joinpath(*child_relative).mkdir(mode=0o700)
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError:
                    fail("private directory entry changed or is unsafe")
                child_before = os.fstat(child_fd)
                try:
                    visit(child_fd, child_relative)
                    child_after = os.fstat(child_fd)
                finally:
                    os.close(child_fd)
                if (child_before.st_dev, child_before.st_ino, child_before.st_mtime_ns) != (
                        child_after.st_dev, child_after.st_ino, child_after.st_mtime_ns):
                    fail("private directory changed while media was assembled")
            elif stat.S_ISREG(details.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                try:
                    input_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError:
                    fail("private directory file changed or is unsafe")
                before = os.fstat(input_fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_size - total:
                    os.close(input_fd)
                    fail("private directory exceeds the remaining bundle capacity")
                output_fd: int | None = None
                if destination is not None:
                    target = destination.joinpath(*child_relative)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    output_fd = create_private_file(target)
                file_hash = hashlib.sha256()
                file_size = 0
                try:
                    while chunk := os.read(input_fd, CHUNK):
                        file_size += len(chunk)
                        if total + file_size > maximum_size:
                            fail("private directory exceeds the remaining bundle capacity")
                        file_hash.update(chunk)
                        if output_fd is not None:
                            view = memoryview(chunk)
                            while view:
                                written = os.write(output_fd, view)
                                view = view[written:]
                    if output_fd is not None:
                        os.fsync(output_fd)
                    after = os.fstat(input_fd)
                finally:
                    if output_fd is not None:
                        os.close(output_fd)
                    os.close(input_fd)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    fail("private directory file changed while media was assembled")
                total += file_size
                tree_hash.update(
                    b"F\0" + encoded_path + b"\0" + str(file_size).encode("ascii") + b"\0" +
                    file_hash.hexdigest().encode("ascii") + b"\0"
                )
            else:
                fail("private directory may contain only regular files and directories")

    try:
        if destination is not None:
            destination.mkdir(parents=True, mode=0o700)
        visit(root_fd, ())
        root_after = os.fstat(root_fd)
    finally:
        os.close(root_fd)
    if (root_before.st_dev, root_before.st_ino, root_before.st_mtime_ns) != (
            root_after.st_dev, root_after.st_ino, root_after.st_mtime_ns):
        fail("private directory changed while media was assembled")
    actual = tree_hash.hexdigest()
    if expected_hash is not None and actual != expected_hash:
        fail("private directory digest does not match its manifest")
    return total, actual


def extract_single_iso(source: Path, destination: Path, maximum_size: int,
                       expected_size: int, expected_hash: str) -> tuple[int, str]:
    fd, before = open_regular(normalized_absolute(source, "archive path"))
    if before.st_size != expected_size or sha256_fd(fd) != expected_hash:
        os.close(fd)
        fail("artifact size or digest does not match its catalog")
    try:
        with os.fdopen(os.dup(fd), "rb") as archive_file, zipfile.ZipFile(archive_file) as archive:
            members = archive.infolist()
            if len(members) > 10_000:
                fail("upstream media archive contains too many entries")
            candidates: list[zipfile.ZipInfo] = []
            for member in members:
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                unsafe_mode = file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR)
                if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts) or unsafe_mode:
                    fail("upstream media archive contains an unsafe member")
                if not member.is_dir() and path.suffix.casefold() == ".iso":
                    candidates.append(member)
            if len(candidates) != 1:
                fail("upstream media archive must contain exactly one ISO")
            member = candidates[0]
            if member.file_size > maximum_size:
                fail("upstream ISO exceeds the bundle capacity")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            value = hashlib.sha256()
            size = 0
            try:
                with archive.open(member) as input_file:
                    while chunk := input_file.read(CHUNK):
                        size += len(chunk)
                        if size > maximum_size:
                            fail("upstream ISO exceeds the bundle capacity")
                        value.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(output_fd, view)
                            view = view[written:]
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
        after = os.fstat(fd)
        after_hash = sha256_fd(fd)
    except (OSError, zipfile.BadZipFile):
        fail("upstream media archive could not be safely extracted")
    finally:
        os.close(fd)
    if after_hash != expected_hash:
        fail("upstream media archive digest changed while it was extracted")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        fail("upstream media archive changed while it was extracted")
    os.utime(destination, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
    return size, value.hexdigest()


def selected_artifacts(bundle: Bundle, catalog: Mapping[str, CatalogItem]) -> Iterable[tuple[CatalogItem, Artifact]]:
    for item_id in bundle.items:
        item = catalog[item_id]
        if item.distribution != "redistributable" and item.artifacts:
            fail("non-redistributable catalog entries cannot be fetched or bundled")
        for artifact in item.artifacts:
            yield item, artifact


def write_metadata(stage: Path, bundle: Bundle, records: list[dict[str, object]], private: bool,
                   builder: Mapping[str, str]) -> None:
    payload = {
        "schema_version": 1,
        "bundle": bundle.id,
        "redistributable": bundle.redistributable and not private,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "builder": dict(builder),
        "artifacts": records,
    }
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    (stage / "CATALOG").mkdir(exist_ok=True)
    (stage / "CATALOG" / "MEDIA.JSON").write_text(text, encoding="ascii")
    warning = (
        "This kit contains only catalog-approved redistributable artifacts. See CATALOG\\MEDIA.JSON.\r\n"
        if not private else
        "PERSONAL MEDIA: contains user-supplied software. Do not publish or redistribute this image.\r\n"
    )
    (stage / "README.TXT").write_text(warning, encoding="ascii")
    for path in stage.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
            os.utime(path, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
        elif path.is_dir():
            os.chmod(path, 0o700)
            os.utime(path, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
    os.utime(stage, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)


def destination_conflicts(candidate: str, existing: Iterable[str]) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(candidate).parts)
    for value in existing:
        other = tuple(part.casefold() for part in PurePosixPath(value).parts)
        if parts == other or parts[:len(other)] == other or other[:len(parts)] == parts:
            return True
    return False


def populate_stage(stage: Path, bundle: Bundle, catalog: Mapping[str, CatalogItem], cache: Path,
                   private_items: Sequence[PrivateItem], builder: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    destinations: set[str] = {"README.TXT", "CATALOG/MEDIA.JSON"}
    total = 0
    for item, artifact in selected_artifacts(bundle, catalog):
        source = cache / artifact.filename
        destination = artifact.destination
        if destination_conflicts(destination, destinations):
            fail("bundle destinations collide")
        destinations.add(destination)
        target = stage.joinpath(*PurePosixPath(destination).parts)
        if artifact.extract_single_iso:
            material_size, material_hash = extract_single_iso(
                source, target, bundle.max_payload_bytes - total, artifact.size, artifact.sha256
            )
        else:
            material_size, material_hash = copy_verified(
                source, target, bundle.max_payload_bytes - total, artifact.sha256
            )
        total += material_size
        records.append({"id": item.id, "version": item.version, "destination": destination, "sha256": material_hash,
                        "bytes": material_size, "license_expression": item.license_expression,
                        "license_basis": item.license_basis, "license_evidence": item.license_evidence,
                        "notice_location": item.notice_location, "source_compliance": item.source_compliance,
                        "compatibility": item.compatibility, "homepage": item.homepage, "role": artifact.role,
                        "source_archive": {"url": artifact.url, "filename": artifact.filename,
                                           "bytes": artifact.size, "sha256": artifact.sha256}, "private": False})
    for item in private_items:
        if destination_conflicts(item.destination, destinations):
            fail("private and public destinations collide")
        destinations.add(item.destination)
        target = stage.joinpath(*PurePosixPath(item.destination).parts)
        if item.source_type == "directory":
            size, actual = process_directory(
                item.path, target, bundle.max_payload_bytes - total, item.sha256
            )
        else:
            size, actual = copy_verified(
                item.path, target, bundle.max_payload_bytes - total, item.sha256
            )
        total += size
        records.append({"id": item.id, "version": item.version, "destination": item.destination, "sha256": actual,
                        "bytes": size, "license_expression": "NOASSERTION", "source_compliance": "user-supplied",
                        "source_type": item.source_type, "private": True})
    if total > bundle.max_payload_bytes:
        fail("bundle contents exceed its configured media capacity")
    write_metadata(stage, bundle, records, bool(private_items), builder or {"tool": "software-media"})
    return records


def get_xorriso_version(xorriso: str) -> str:
    try:
        result = subprocess.run(
            [xorriso, "-version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, check=False,
        )
    except OSError:
        fail("xorriso is required to build a companion ISO")
    first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if result.returncode != 0 or not first_line or len(first_line) > 160 or any(ord(char) < 32 for char in first_line):
        fail("xorriso version could not be identified")
    return first_line


def publish_temporary(temporary: Path, output_name: str, parent_fd: int, failure: str) -> None:
    try:
        os.link(temporary, output_name, dst_dir_fd=parent_fd, follow_symlinks=False)
    except FileExistsError:
        fail("output already exists")
    except OSError:
        fail(failure)


def publish_kit(stage: Path, temporary: Path, output_name: str, parent_fd: int, bundle: Bundle) -> None:
    try:
        raw_fd = create_private_file(temporary)
        with os.fdopen(raw_fd, "w+b") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
                for source in sorted(path for path in stage.rglob("*") if path.is_file()):
                    relative = source.relative_to(stage).as_posix()
                    info = zipfile.ZipInfo(relative, (2000, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.compress_type = zipfile.ZIP_STORED
                    fd, before = open_regular(normalized_absolute(source, "kit source"))
                    try:
                        with os.fdopen(fd, "rb", closefd=False) as input_file, archive.open(info, "w", force_zip64=True) as target:
                            while chunk := input_file.read(CHUNK):
                                target.write(chunk)
                        after = os.fstat(fd)
                    finally:
                        os.close(fd)
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        fail("kit source changed while it was assembled")
            raw.flush()
            os.fsync(raw.fileno())
        if temporary.stat().st_size > bundle.max_output_bytes:
            fail("kit output exceeds its configured size")
        publish_temporary(temporary, output_name, parent_fd, "kit output could not be published atomically")
    except (OSError, zipfile.BadZipFile):
        fail("kit archive could not be created")
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def publish_iso(stage: Path, temporary: Path, output_name: str, parent_fd: int,
                bundle: Bundle, xorriso: str = "xorriso") -> None:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "LANG": "C",
                   "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH)}
    command = [xorriso, "-no_rc", "-as", "mkisofs", "-quiet", "-iso-level", "1", "-J", "-r", "-V", bundle.volume_id,
               "-publisher", "86BOX VM RECIPES", "-p", "86BOX VM RECIPES", "-o", str(temporary), str(stage)]
    try:
        wrapped = ["/bin/sh", "-c", 'umask 077\nexec "$@"', "software-media-xorriso", *command]
        result = subprocess.run(wrapped, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0 or not temporary.is_file():
            fail("xorriso could not create the companion ISO")
        os.chmod(temporary, 0o600)
        if temporary.stat().st_size > bundle.max_output_bytes:
            fail("companion ISO exceeds its configured media capacity")
        publish_temporary(temporary, output_name, parent_fd, "ISO output could not be published atomically")
    except OSError:
        fail("xorriso is required to build a companion ISO")
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def build(bundle: Bundle, catalog: Mapping[str, CatalogItem], cache: Path, output: Path,
          private_items: Sequence[PrivateItem], xorriso: str = "xorriso") -> None:
    if private_items and not bundle.allow_private:
        fail("this bundle does not accept private items")
    if bundle.allow_private and not private_items:
        fail("personal bundle requires a private manifest")
    output = normalized_absolute(output, "output path")
    cache = normalized_absolute(cache, "cache path")
    parent = output.parent
    parent_fd = open_directory(parent)
    parent_details = os.fstat(parent_fd)
    workspace_name = f".86box-software-{secrets.token_hex(12)}"
    try:
        os.mkdir(workspace_name, mode=0o700, dir_fd=parent_fd)
    except OSError:
        os.close(parent_fd)
        fail("secure build workspace could not be created")
    workspace = parent / workspace_name
    stage = workspace / "CONTENT"
    workspace_details = os.stat(workspace_name, dir_fd=parent_fd, follow_symlinks=False)
    try:
        stage.mkdir(mode=0o700)
        if bundle.output == "iso":
            builder = {"tool": "xorriso", "version": get_xorriso_version(xorriso),
                       "reproducibility_scope": "same inputs and xorriso version"}
        else:
            builder = {"tool": "python-zipfile", "format": "ZIP_STORED",
                       "reproducibility_scope": "same inputs and repository revision"}
        populate_stage(stage, bundle, catalog, cache, private_items, builder)
        current_parent = parent.lstat()
        if (current_parent.st_dev, current_parent.st_ino) != (parent_details.st_dev, parent_details.st_ino):
            fail("output directory changed while media was assembled")
        if bundle.output == "kit":
            publish_kit(stage, workspace / "KIT.tmp", output.name, parent_fd, bundle)
        else:
            publish_iso(stage, workspace / "IMAGE.tmp", output.name, parent_fd, bundle, xorriso)
    finally:
        try:
            current = os.stat(workspace_name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (workspace_details.st_dev, workspace_details.st_ino):
                fail("temporary build directory changed unexpectedly; it was left in place")
            import shutil
            shutil.rmtree(workspace_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def print_catalog(catalog: Mapping[str, CatalogItem]) -> None:
    print("ID\tCOMPATIBILITY\tDISTRIBUTION\tTITLE")
    for item in sorted(catalog.values(), key=lambda value: (value.kind, value.title.casefold())):
        print(f"{item.id}\t{item.compatibility}\t{item.distribution}\t{item.title}")


def write_personal_template(bundle: Bundle, catalog: Mapping[str, CatalogItem], output: Path) -> None:
    if not bundle.allow_private:
        fail("personal templates require a private-media bundle")
    selections = [catalog[item_id] for item_id in bundle.items if catalog[item_id].distribution == "user-supplied"]
    if not selections:
        fail("personal bundle has no catalog selections")
    output = normalized_absolute(output, "template output path")
    parent_fd = open_directory(output.parent)
    temporary = output.parent / f".{output.name}.{secrets.token_hex(12)}.tmp"
    lines = ["schema_version = 1", ""]
    for item in selections:
        suffix = "directory" if item.personal_source_type == "directory" else "installer-or-data"
        lines.extend([
            "[[item]]",
            f"id = {json.dumps(item.id)}",
            f"title = {json.dumps(item.title)}",
            f"version = {json.dumps(item.version)}",
            f"source_type = {json.dumps(item.personal_source_type)}",
            f"path = {json.dumps('/absolute/path/to/your/' + item.id + '-' + suffix)}",
            f"sha256 = {json.dumps('replace-with-the-files-lowercase-sha256-digest')}",
            f"destination = {json.dumps(item.personal_destination)}",
            "license_acknowledged = false",
            "",
        ])
    try:
        fd = create_private_file(temporary)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            target.write("\n".join(lines))
            target.flush()
            os.fsync(target.fileno())
        publish_temporary(temporary, output.name, parent_fd, "personal template could not be published atomically")
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
        os.close(parent_fd)


def hash_private_source(path: Path, source_type: str) -> tuple[int, str]:
    path = normalized_absolute(path, "private source path")
    if source_type == "directory":
        return process_directory(path, None, (1 << 63) - 1)
    fd, details = open_regular(path)
    try:
        actual = sha256_fd(fd)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        fail("private source changed while it was hashed")
    return details.st_size, actual


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("list")
    hash_parser = sub.add_parser("hash")
    hash_parser.add_argument("--source-type", required=True, choices=("file", "directory"))
    hash_parser.add_argument("--path", required=True, type=Path)
    template = sub.add_parser("personal-template")
    template.add_argument("--bundle", required=True)
    template.add_argument("--output", required=True, type=Path)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--bundle", required=True)
    fetch.add_argument("--cache", required=True, type=Path)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--bundle", required=True)
    build_parser.add_argument("--cache", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--private-manifest", type=Path)
    build_parser.add_argument("--xorriso", default="xorriso", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        catalog, bundles = load_all()
        if args.command == "validate":
            print(f"Validated {len(catalog)} software catalog item(s) and {len(bundles)} bundle(s).")
            return 0
        if args.command == "list":
            print_catalog(catalog)
            return 0
        if args.command == "hash":
            if any(os.environ.get(marker) for marker in CI_MARKERS):
                fail("private source hashing is disabled in CI")
            size, actual = hash_private_source(args.path, args.source_type)
            print(f"sha256 = {json.dumps(actual)}")
            print(f"bytes = {size}")
            return 0
        if args.bundle not in bundles:
            fail("unknown software bundle")
        bundle = bundles[args.bundle]
        if args.command == "personal-template":
            write_personal_template(bundle, catalog, args.output)
            return 0
        if args.command == "fetch":
            if os.environ.get("CI") or any(os.environ.get(marker) for marker in CI_MARKERS):
                fail("network fetching is disabled in CI")
            for _, artifact in selected_artifacts(bundle, catalog):
                fetch_artifact(artifact, normalized_absolute(args.cache, "cache path"))
            return 0
        private_items: tuple[PrivateItem, ...] = ()
        if args.private_manifest is not None:
            if any(os.environ.get(marker) for marker in CI_MARKERS):
                fail("private media builds are disabled in CI")
            private_items = load_private(normalized_absolute(args.private_manifest, "private manifest path"))
        build(bundle, catalog, args.cache, normalized_absolute(args.output, "output path"), private_items, args.xorriso)
        return 0
    except MediaError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("software-media operation failed; private details were suppressed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate public 86Box machine recipe manifests and their referenced files."""

from __future__ import annotations

import argparse
import configparser
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import NoReturn

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only reachable on Python < 3.11
    print("validate-recipes.py requires Python 3.11 or newer.", file=sys.stderr)
    raise SystemExit(2)


SCHEMA_VERSION = 1
TOP_LEVEL_FIELDS = {
    "schema_version",
    "id",
    "name",
    "machine_config",
    "global_config",
    "launchers",
    "hard_disks",
    "requirements",
}
LAUNCHER_FIELDS = {"macos"}
HARD_DISK_FIELDS = {
    "config_id",
    "path",
    "bytes",
    "sectors",
    "heads",
    "cylinders",
    "bus",
    "channel",
}
REQUIREMENT_FIELDS = {"rom_subdirectories", "asset_files"}
RECIPE_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
DISK_ID_PATTERN = re.compile(r"hdd_[0-9]{2}\Z")
WINDOWS_DRIVE_PATTERN = re.compile(r"[A-Za-z]:")
ASCII_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
CONFIG_DISK_PATH_PATTERN = re.compile(r"(hdd_[0-9]{2})_fn\Z")


class RecipeValidationError(Exception):
    """A validation failure tied to one manifest field."""

    def __init__(self, manifest: Path, field: str, message: str) -> None:
        super().__init__(message)
        self.manifest = manifest
        self.field = field
        self.message = message

    def render(self, root: Path) -> str:
        try:
            display_path = self.manifest.relative_to(root)
        except ValueError:
            display_path = self.manifest
        return f"{display_path.as_posix()}: {self.field}: {self.message}"


def fail(manifest: Path, field: str, message: str) -> NoReturn:
    raise RecipeValidationError(manifest, field, message)


def require_exact_fields(
    table: dict[str, object], expected: set[str], manifest: Path, field: str
) -> None:
    missing = sorted(expected - table.keys())
    if missing:
        fail(manifest, field, f"missing required field {missing[0]!r}")
    unknown = sorted(table.keys() - expected)
    if unknown:
        fail(manifest, field, f"unknown field {unknown[0]!r}")


def require_string(
    table: dict[str, object], key: str, manifest: Path, field_prefix: str = ""
) -> str:
    field = f"{field_prefix}.{key}" if field_prefix else key
    value = table[key]
    if type(value) is not str:
        fail(manifest, field, "must be a string")
    if not value:
        fail(manifest, field, "must not be empty")
    if ASCII_CONTROL_PATTERN.search(value):
        fail(manifest, field, "must not contain ASCII control characters")
    return value


def require_positive_integer(
    table: dict[str, object], key: str, manifest: Path, field_prefix: str
) -> int:
    field = f"{field_prefix}.{key}"
    value = table[key]
    if type(value) is not int:
        fail(manifest, field, "must be an integer")
    if value <= 0:
        fail(manifest, field, "must be greater than zero")
    return value


def require_table(
    table: dict[str, object], key: str, manifest: Path, field_prefix: str = ""
) -> dict[str, object]:
    field = f"{field_prefix}.{key}" if field_prefix else key
    value = table[key]
    if type(value) is not dict:
        fail(manifest, field, "must be a table")
    return value


def require_string_list(
    table: dict[str, object], key: str, manifest: Path, field_prefix: str
) -> list[str]:
    field = f"{field_prefix}.{key}"
    value = table[key]
    if type(value) is not list:
        fail(manifest, field, "must be an array of strings")
    if not value:
        fail(manifest, field, "must not be empty")
    for index, item in enumerate(value):
        if type(item) is not str or not item:
            fail(manifest, f"{field}[{index}]", "must be a non-empty string")
        if ASCII_CONTROL_PATTERN.search(item):
            fail(
                manifest,
                f"{field}[{index}]",
                "must not contain ASCII control characters",
            )
    return value


def validate_relative_path(value: str, manifest: Path, field: str) -> PurePosixPath:
    if ASCII_CONTROL_PATTERN.search(value):
        fail(manifest, field, "must not contain ASCII control characters")
    path = PurePosixPath(value)
    if WINDOWS_DRIVE_PATTERN.match(value) or path.is_absolute():
        fail(manifest, field, "must be relative")
    if "\\" in value:
        fail(manifest, field, "must use forward slashes")
    if not value or path.as_posix() != value or any(part in {".", ".."} for part in path.parts):
        fail(manifest, field, "must be a normalized relative path without '.' or '..'")
    return path


def ensure_no_symlink_components(
    machine_dir: Path, relative_path: PurePosixPath, manifest: Path, field: str
) -> Path:
    current = machine_dir
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            fail(manifest, field, f"must not traverse symlink {part!r}")
    return current


def ensure_inside_machine(
    candidate: Path, machine_dir: Path, manifest: Path, field: str, *, strict: bool
) -> None:
    try:
        resolved = candidate.resolve(strict=strict)
        resolved.relative_to(machine_dir.resolve(strict=True))
    except (FileNotFoundError, RuntimeError, ValueError):
        fail(manifest, field, "must resolve inside the machine directory")


def run_git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def indexed_blob_oid(
    root: Path, repository_path: str, manifest: Path, field: str, display_value: str
) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--error-unmatch",
            "--stage",
            "-z",
            "--",
            repository_path,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        fail(manifest, field, f"referenced file is not tracked by Git: {display_value}")

    records = result.stdout.split(b"\0")
    if len(records) != 2 or records[-1] != b"" or b"\t" not in records[0]:
        fail(manifest, field, f"could not read the indexed file entry: {display_value}")
    metadata, indexed_path = records[0].split(b"\t", 1)
    parts = metadata.split()
    if len(parts) != 3 or indexed_path != os.fsencode(repository_path):
        fail(manifest, field, f"could not read the indexed file entry: {display_value}")
    mode, oid, stage = parts
    if stage != b"0":
        fail(manifest, field, f"indexed file has unresolved conflicts: {display_value}")
    if mode not in {b"100644", b"100755"}:
        fail(manifest, field, f"must be tracked as a regular file: {display_value}")
    return oid.decode("ascii")


def ensure_index_matches_worktree(
    root: Path,
    candidate: Path,
    repository_path: str,
    indexed_oid: str,
    manifest: Path,
    field: str,
    display_value: str,
) -> None:
    result = run_git(
        root,
        [
            "hash-object",
            "--filters",
            f"--path={repository_path}",
            "--",
            str(candidate),
        ],
    )
    worktree_oid = result.stdout.strip()
    if result.returncode != 0 or not worktree_oid:
        detail = result.stderr.strip() or f"could not hash worktree file: {display_value}"
        fail(manifest, field, detail)
    if worktree_oid != indexed_oid:
        fail(manifest, field, f"must match its indexed Git version: {display_value}")


def ensure_tracked_file(
    root: Path,
    machine_dir: Path,
    value: str,
    manifest: Path,
    field: str,
    *,
    require_index_match: bool = False,
) -> Path:
    relative_path = validate_relative_path(value, manifest, field)
    candidate = ensure_no_symlink_components(machine_dir, relative_path, manifest, field)
    ensure_inside_machine(candidate, machine_dir, manifest, field, strict=False)
    if not candidate.is_file():
        fail(manifest, field, f"referenced file does not exist: {value}")
    ensure_inside_machine(candidate, machine_dir, manifest, field, strict=True)
    repository_path = candidate.relative_to(root).as_posix()
    oid = indexed_blob_oid(root, repository_path, manifest, field, value)
    if require_index_match:
        ensure_index_matches_worktree(
            root, candidate, repository_path, oid, manifest, field, value
        )
    return candidate


def ensure_ignored_runtime_path(
    root: Path, machine_dir: Path, value: str, manifest: Path, field: str
) -> None:
    relative_path = validate_relative_path(value, manifest, field)
    candidate = ensure_no_symlink_components(machine_dir, relative_path, manifest, field)
    ensure_inside_machine(candidate, machine_dir, manifest, field, strict=False)
    if not candidate.parent.is_dir():
        fail(manifest, field, f"parent directory does not exist: {relative_path.parent}")
    repository_path = candidate.relative_to(root).as_posix()
    ignored = run_git(root, ["check-ignore", "--quiet", "--", repository_path])
    if ignored.returncode == 1:
        fail(manifest, field, f"runtime path is not ignored by Git: {value}")
    if ignored.returncode != 0:
        detail = ignored.stderr.strip() or "git check-ignore failed"
        fail(manifest, field, detail)

    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--verbose", "-z", "--stdin"],
        check=False,
        input=os.fsencode(repository_path) + b"\0",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 1:
        fail(manifest, field, f"runtime path is not ignored by Git: {value}")
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or "git check-ignore failed"
        fail(manifest, field, detail)

    fields = result.stdout.split(b"\0")
    if len(fields) != 5 or fields[-1] != b"":
        fail(manifest, field, "could not determine the ignore rule provenance")
    source_value = os.fsdecode(fields[0])
    source = Path(source_value)
    if not source.is_absolute():
        source = root / source
    try:
        resolved_source = source.resolve(strict=True)
        source_relative = resolved_source.relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError):
        fail(
            manifest,
            field,
            "ignore rule must come from a tracked repository .gitignore",
        )
    if source_relative.name != ".gitignore":
        fail(
            manifest,
            field,
            "ignore rule must come from a tracked repository .gitignore",
        )
    try:
        candidate.resolve(strict=False).relative_to(resolved_source.parent)
    except ValueError:
        fail(
            manifest,
            field,
            "ignore rule must come from an ancestor repository .gitignore",
        )
    source_posix = PurePosixPath(source_relative.as_posix())
    ensure_no_symlink_components(root, source_posix, manifest, field)
    source_repository_path = source_relative.as_posix()
    source_oid = indexed_blob_oid(
        root, source_repository_path, manifest, field, source_repository_path
    )
    ensure_index_matches_worktree(
        root,
        resolved_source,
        source_repository_path,
        source_oid,
        manifest,
        field,
        "contributing .gitignore",
    )


def parse_machine_config(config_path: Path, manifest: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        with config_path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except (configparser.Error, UnicodeError) as error:
        fail(manifest, "machine_config", f"invalid INI: {error}")
    if parser.defaults():
        fail(
            manifest,
            "machine_config",
            "must not define values in [DEFAULT]; settings must be section-local",
        )
    return parser


def require_config_value(
    config: configparser.ConfigParser, key: str, manifest: Path, field: str
) -> str:
    section = "Hard disks"
    if not config.has_section(section):
        fail(manifest, field, f"machine config is missing section [{section}]")
    if not config.has_option(section, key):
        fail(manifest, field, f"machine config is missing {key}")
    return config.get(section, key).strip()


def validate_hard_disks(
    raw_disks: object,
    root: Path,
    machine_dir: Path,
    manifest: Path,
    config: configparser.ConfigParser,
) -> None:
    if type(raw_disks) is not list or not raw_disks:
        fail(manifest, "hard_disks", "must be a non-empty array of tables")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_channels: set[tuple[str, str]] = set()
    for index, raw_disk in enumerate(raw_disks):
        prefix = f"hard_disks[{index}]"
        if type(raw_disk) is not dict:
            fail(manifest, prefix, "must be a table")
        require_exact_fields(raw_disk, HARD_DISK_FIELDS, manifest, prefix)

        config_id = require_string(raw_disk, "config_id", manifest, prefix)
        if not DISK_ID_PATTERN.fullmatch(config_id):
            fail(manifest, f"{prefix}.config_id", "must use the form hdd_NN")
        if config_id in seen_ids:
            fail(manifest, f"{prefix}.config_id", "must be unique")
        seen_ids.add(config_id)

        disk_path = require_string(raw_disk, "path", manifest, prefix)
        if disk_path in seen_paths:
            fail(manifest, f"{prefix}.path", "must be unique")
        seen_paths.add(disk_path)
        ensure_ignored_runtime_path(
            root, machine_dir, disk_path, manifest, f"{prefix}.path"
        )
        runtime_readme = (
            validate_relative_path(disk_path, manifest, f"{prefix}.path").parent
            / "README.md"
        ).as_posix()
        ensure_tracked_file(
            root, machine_dir, runtime_readme, manifest, f"{prefix}.path"
        )

        byte_size = require_positive_integer(raw_disk, "bytes", manifest, prefix)
        sectors = require_positive_integer(raw_disk, "sectors", manifest, prefix)
        heads = require_positive_integer(raw_disk, "heads", manifest, prefix)
        cylinders = require_positive_integer(raw_disk, "cylinders", manifest, prefix)
        expected_size = sectors * heads * cylinders * 512
        if byte_size != expected_size:
            fail(
                manifest,
                f"{prefix}.bytes",
                f"must equal sectors × heads × cylinders × 512 ({expected_size})",
            )

        bus = require_string(raw_disk, "bus", manifest, prefix)
        if bus != "ide":
            fail(manifest, f"{prefix}.bus", "v1 supports only the 'ide' bus")
        channel = require_string(raw_disk, "channel", manifest, prefix)
        channel_key = (bus, channel)
        if channel_key in seen_channels:
            fail(manifest, f"{prefix}.channel", "must be unique for its bus")
        seen_channels.add(channel_key)

        configured_path = require_config_value(
            config, f"{config_id}_fn", manifest, f"{prefix}.path"
        )
        if configured_path != disk_path:
            fail(
                manifest,
                f"{prefix}.path",
                f"does not match {config_id}_fn in the machine config",
            )

        raw_parameters = require_config_value(
            config, f"{config_id}_parameters", manifest, prefix
        )
        parameters = [part.strip() for part in raw_parameters.split(",")]
        if len(parameters) != 5:
            fail(
                manifest,
                prefix,
                f"{config_id}_parameters must contain sectors, heads, cylinders, flags, and bus",
            )
        expected_geometry = [str(sectors), str(heads), str(cylinders)]
        if parameters[:3] != expected_geometry:
            fail(manifest, prefix, "CHS geometry does not match the machine config")
        if parameters[4] != bus:
            fail(manifest, f"{prefix}.bus", "does not match the machine config")

        configured_channel = require_config_value(
            config, f"{config_id}_{bus}_channel", manifest, f"{prefix}.channel"
        )
        if configured_channel != channel:
            fail(manifest, f"{prefix}.channel", "does not match the machine config")

    configured_ids = {
        match.group(1)
        for option in config.options("Hard disks")
        if (match := CONFIG_DISK_PATH_PATTERN.fullmatch(option))
    }
    if seen_ids != configured_ids:
        missing = sorted(configured_ids - seen_ids)
        extra = sorted(seen_ids - configured_ids)
        details: list[str] = []
        if missing:
            details.append(f"undeclared machine-config IDs: {', '.join(missing)}")
        if extra:
            details.append(f"manifest-only IDs: {', '.join(extra)}")
        fail(
            manifest,
            "hard_disks",
            "disk IDs must exactly match hdd_NN_fn entries (" + "; ".join(details) + ")",
        )


def validate_requirements(raw: object, manifest: Path) -> None:
    if type(raw) is not dict:
        fail(manifest, "requirements", "must be a table")
    require_exact_fields(raw, REQUIREMENT_FIELDS, manifest, "requirements")
    for key in sorted(REQUIREMENT_FIELDS):
        values = require_string_list(raw, key, manifest, "requirements")
        for index, value in enumerate(values):
            validate_relative_path(value, manifest, f"requirements.{key}[{index}]")

    # Issue #5's launcher contract tests verify that these declared external
    # resources become launcher arguments; parsing shell source here is brittle.


def validate_manifest(root: Path, machine_dir: Path) -> None:
    manifest = machine_dir / "recipe.toml"
    if machine_dir.is_symlink():
        fail(manifest, "id", "machine directory must not be a symlink")
    if not manifest.is_file():
        fail(manifest, "manifest", "file not found")
    if manifest.is_symlink():
        fail(manifest, "manifest", "must not be a symlink")
    ensure_tracked_file(
        root,
        machine_dir,
        "recipe.toml",
        manifest,
        "manifest",
        require_index_match=True,
    )

    try:
        with manifest.open("rb") as manifest_file:
            data = tomllib.load(manifest_file)
    except tomllib.TOMLDecodeError as error:
        fail(manifest, "manifest", f"invalid TOML: {error}")

    require_exact_fields(data, TOP_LEVEL_FIELDS, manifest, "manifest")
    version = data["schema_version"]
    if type(version) is not int:
        fail(manifest, "schema_version", "must be an integer")
    if version != SCHEMA_VERSION:
        fail(manifest, "schema_version", f"unsupported version {version!r}")

    recipe_id = require_string(data, "id", manifest)
    if not RECIPE_ID_PATTERN.fullmatch(recipe_id):
        fail(manifest, "id", "must be a lowercase hyphenated identifier")
    if recipe_id != machine_dir.name:
        fail(manifest, "id", "must match the machine directory name")
    require_string(data, "name", manifest)

    ensure_tracked_file(root, machine_dir, "README.md", manifest, "readme")

    machine_config_value = require_string(data, "machine_config", manifest)
    machine_config = ensure_tracked_file(
        root,
        machine_dir,
        machine_config_value,
        manifest,
        "machine_config",
        require_index_match=True,
    )
    ensure_tracked_file(
        root,
        machine_dir,
        require_string(data, "global_config", manifest),
        manifest,
        "global_config",
    )

    launchers = require_table(data, "launchers", manifest)
    require_exact_fields(launchers, LAUNCHER_FIELDS, manifest, "launchers")
    for platform in sorted(LAUNCHER_FIELDS):
        ensure_tracked_file(
            root,
            machine_dir,
            require_string(launchers, platform, manifest, "launchers"),
            manifest,
            f"launchers.{platform}",
        )

    config = parse_machine_config(machine_config, manifest)
    validate_hard_disks(
        data["hard_disks"], root, machine_dir, manifest, config
    )
    validate_requirements(data["requirements"], manifest)


def validate_repository(root: Path) -> list[RecipeValidationError]:
    root = root.resolve()
    machines_dir = root / "machines"
    git_check = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if git_check.returncode != 0 or git_check.stdout.strip() != "true":
        return [
            RecipeValidationError(
                machines_dir / "recipe.toml", "repository", "root is not a Git worktree"
            )
        ]

    index_result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "machines"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if index_result.returncode != 0:
        detail = os.fsdecode(index_result.stderr).strip() or "could not read Git index"
        return [RecipeValidationError(machines_dir / "recipe.toml", "machines", detail)]
    records = index_result.stdout.split(b"\0")
    if not records or records[-1] != b"":
        return [
            RecipeValidationError(
                machines_dir / "recipe.toml", "machines", "could not parse Git index"
            )
        ]
    indexed_ids = {
        path.parts[1]
        for record in records[:-1]
        if len((path := PurePosixPath(os.fsdecode(record))).parts) >= 2
        and path.parts[0] == "machines"
    }

    worktree_ids: set[str] = set()
    if machines_dir.is_dir():
        worktree_ids = {
            path.name
            for path in machines_dir.iterdir()
            if path.is_dir() or path.is_symlink()
        }
    if worktree_ids != indexed_ids:
        index_only = sorted(indexed_ids - worktree_ids)
        worktree_only = sorted(worktree_ids - indexed_ids)
        details: list[str] = []
        if index_only:
            details.append(f"index-only: {', '.join(index_only)}")
        if worktree_only:
            details.append(f"worktree-only: {', '.join(worktree_only)}")
        affected_id = (index_only or worktree_only)[0]
        return [
            RecipeValidationError(
                machines_dir / affected_id / "recipe.toml",
                "machines",
                "worktree and Git index machine IDs differ ("
                + "; ".join(details)
                + ")",
            )
        ]

    if not machines_dir.is_dir():
        return [
            RecipeValidationError(
                machines_dir / "recipe.toml", "machines", "directory not found"
            )
        ]
    if not worktree_ids:
        return [
            RecipeValidationError(
                machines_dir / "recipe.toml", "machines", "no machine directories found"
            )
        ]

    machine_dirs = [machines_dir / machine_id for machine_id in sorted(worktree_ids)]

    errors: list[RecipeValidationError] = []
    for machine_dir in machine_dirs:
        try:
            validate_manifest(root, machine_dir)
        except RecipeValidationError as error:
            errors.append(error)
    return errors


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (defaults to the validator's repository)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    root = arguments.root.resolve()
    errors = validate_repository(root)
    if errors:
        for error in errors:
            print(error.render(root), file=sys.stderr)
        return 1
    manifests = sorted((root / "machines").glob("*/recipe.toml"))
    print(f"Validated {len(manifests)} recipe manifest(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

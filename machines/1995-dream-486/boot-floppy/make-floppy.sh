#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 3 ]]; then
    printf 'Usage: %s BASE_IMAGE DRIVER_DIRECTORY OUTPUT_IMAGE\n' "$0" >&2
    exit 2
fi

base_image="$1"
driver_dir="$2"
output_image="$3"
script_dir="$(cd "$(dirname "$0")" && pwd)"
expected_size=1474560
temporary_image=""
validation_file=""
checksum_style=""

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2317,SC2329
cleanup() {
    if [[ -n "$temporary_image" ]]; then
        rm -f -- "$temporary_image" || true
    fi
    if [[ -n "$validation_file" ]]; then
        rm -f -- "$validation_file" || true
    fi
}
trap 'cleanup' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for tool in mcopy mdir; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf '%s is required; install the mtools package.\n' "$tool" >&2
        exit 1
    fi
done
if ! command -v link >/dev/null 2>&1; then
    printf 'The system link utility is required for atomic no-clobber output.\n' >&2
    exit 1
fi
if command -v shasum >/dev/null 2>&1; then
    checksum_style="shasum"
elif command -v sha256sum >/dev/null 2>&1; then
    checksum_style="sha256sum"
else
    printf 'shasum or sha256sum is required to verify the completed image.\n' >&2
    exit 1
fi

for path in "$base_image" "$driver_dir/HIMEMX.EXE" "$driver_dir/UDVD2.SYS" "$driver_dir/SHSUCDX.COM"; do
    if [[ ! -f "$path" ]]; then
        printf 'Required file not found: %s\n' "$path" >&2
        exit 1
    fi
done

if [[ -e "$output_image" || -L "$output_image" ]]; then
    printf 'Refusing to overwrite existing output: %s\n' "$output_image" >&2
    exit 1
fi

if [[ "$output_image" == */ ]]; then
    printf 'Output must name a file: %s\n' "$output_image" >&2
    exit 1
fi

output_parent="$(dirname "$output_image")"
output_name="$(basename "$output_image")"
if [[ ! -d "$output_parent" ]]; then
    printf 'Output directory not found: %s\n' "$output_parent" >&2
    exit 1
fi
output_dir="$(cd "$output_parent" && pwd -P)"
output_path="$output_dir/$output_name"

# Repeat the check using the normalized path before creating any temporary data.
if [[ -e "$output_path" || -L "$output_path" ]]; then
    printf 'Refusing to overwrite existing output: %s\n' "$output_image" >&2
    exit 1
fi

umask 077
temporary_image="$(mktemp "$output_dir/.${output_name}.tmp.XXXXXX")"
validation_file="$(mktemp "$output_dir/.${output_name}.verify.XXXXXX")"

cp "$base_image" "$temporary_image"

if ! mdir -i "$temporary_image" '::/FREEDOS/BIN/COMMAND.COM' >/dev/null 2>&1; then
    printf 'The base image does not contain A:\\FREEDOS\\BIN\\COMMAND.COM.\n' >&2
    exit 1
fi

mcopy -o -i "$temporary_image" "$driver_dir/HIMEMX.EXE" '::/HIMEMX.EXE'
mcopy -o -i "$temporary_image" "$driver_dir/UDVD2.SYS" '::/UDVD2.SYS'
mcopy -o -i "$temporary_image" "$driver_dir/SHSUCDX.COM" '::/SHSUCDX.COM'
mcopy -o -i "$temporary_image" "$script_dir/FDCONFIG.SYS" '::/FDCONFIG.SYS'
mcopy -o -i "$temporary_image" "$script_dir/FDAUTO.BAT" '::/FDAUTO.BAT'

image_size="$(wc -c < "$temporary_image")"
if ((image_size != expected_size)); then
    printf 'Output image has %d bytes; expected %d.\n' "$image_size" "$expected_size" >&2
    exit 1
fi

validate_copy() {
    local source_path="$1"
    local guest_path="$2"

    if ! mdir -i "$temporary_image" "::/$guest_path" >/dev/null 2>&1; then
        printf 'Output image is missing A:\\%s.\n' "$guest_path" >&2
        exit 1
    fi
    : > "$validation_file"
    mcopy -o -i "$temporary_image" "::/$guest_path" "$validation_file"
    if ! cmp -s "$source_path" "$validation_file"; then
        printf 'Output validation failed for %s.\n' "${source_path##*/}" >&2
        exit 1
    fi
}

validate_copy "$driver_dir/HIMEMX.EXE" HIMEMX.EXE
validate_copy "$driver_dir/UDVD2.SYS" UDVD2.SYS
validate_copy "$driver_dir/SHSUCDX.COM" SHSUCDX.COM
validate_copy "$script_dir/FDCONFIG.SYS" FDCONFIG.SYS
validate_copy "$script_dir/FDAUTO.BAT" FDAUTO.BAT

calculate_sha256() {
    case "$checksum_style" in
        shasum) shasum -a 256 "$temporary_image" ;;
        sha256sum) sha256sum "$temporary_image" ;;
    esac
}

if ! checksum_line="$(calculate_sha256)"; then
    printf 'Could not compute SHA-256 for the completed image.\n' >&2
    exit 1
fi
read -r image_hash _ <<< "$checksum_line"
if [[ ! "$image_hash" =~ ^[[:xdigit:]]{64}$ ]]; then
    printf 'SHA-256 utility returned an invalid digest.\n' >&2
    exit 1
fi

# A hard link creates the destination name atomically and fails if any other
# process created that name after our preflight checks. The temporary image is in
# the same directory, so this never crosses filesystems.
if ! link "$temporary_image" "$output_path"; then
    printf 'Refusing to replace output created concurrently: %s\n' "$output_image" >&2
    exit 1
fi
if rm -f -- "$temporary_image"; then
    temporary_image=""
else
    printf 'Warning: could not remove completed temporary link: %s\n' \
        "$temporary_image" >&2 || true
fi

printf 'Created %s\n' "$output_image" || true
printf 'SHA-256: %s\n' "$image_hash" || true
exit 0

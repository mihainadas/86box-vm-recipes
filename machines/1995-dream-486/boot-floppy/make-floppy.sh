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

for tool in mcopy mdir; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf '%s is required; install the mtools package.\n' "$tool" >&2
        exit 1
    fi
done

for path in "$base_image" "$driver_dir/HIMEMX.EXE" "$driver_dir/UDVD2.SYS" "$driver_dir/SHSUCDX.COM"; do
    if [[ ! -f "$path" ]]; then
        printf 'Required file not found: %s\n' "$path" >&2
        exit 1
    fi
done

if [[ -e "$output_image" ]]; then
    printf 'Refusing to overwrite existing output: %s\n' "$output_image" >&2
    exit 1
fi

cp "$base_image" "$output_image"

if ! mdir -i "$output_image" '::/FREEDOS/BIN/COMMAND.COM' >/dev/null 2>&1; then
    printf 'The base image does not contain A:\\FREEDOS\\BIN\\COMMAND.COM.\n' >&2
    rm -f "$output_image"
    exit 1
fi

mcopy -o -i "$output_image" "$driver_dir/HIMEMX.EXE" '::/HIMEMX.EXE'
mcopy -o -i "$output_image" "$driver_dir/UDVD2.SYS" '::/UDVD2.SYS'
mcopy -o -i "$output_image" "$driver_dir/SHSUCDX.COM" '::/SHSUCDX.COM'
mcopy -o -i "$output_image" "$script_dir/FDCONFIG.SYS" '::/FDCONFIG.SYS'
mcopy -o -i "$output_image" "$script_dir/FDAUTO.BAT" '::/FDAUTO.BAT'

printf 'Created %s\n' "$output_image"
shasum -a 256 "$output_image"

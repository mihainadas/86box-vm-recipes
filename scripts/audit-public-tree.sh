#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
status=0

while IFS= read -r -d '' path; do
    printf 'Unexpected large file: %s\n' "${path#"$repo_dir"/}"
    status=1
done < <(find "$repo_dir" -path "$repo_dir/.git" -prune -o -type f -size +5M -print0)

while IFS= read -r -d '' path; do
    case "$path" in
        */boot-floppy/FDCONFIG.SYS) ;;
        *)
            printf 'Forbidden binary/media extension: %s\n' "${path#"$repo_dir"/}"
            status=1
            ;;
    esac
done < <(find "$repo_dir" -path "$repo_dir/.git" -prune -o -type f \( -iname '*.hdd' -o -iname '*.vhd' -o -iname '*.vhdx' -o -iname '*.qcow' -o -iname '*.qcow2' -o -iname '*.img' -o -iname '*.ima' -o -iname '*.iso' -o -iname '*.cue' -o -iname '*.rom' -o -iname '*.bin' -o -iname '*.nvr' -o -iname '*.zip' -o -iname '*.7z' -o -iname '*.rar' -o -iname '*.exe' -o -iname '*.com' -o -iname '*.dll' -o -iname '*.vxd' -o -iname '*.sys' \) -print0)

if grep -RIE --exclude-dir=.git --exclude='audit-public-tree.sh' '(/Users/|/home/|C:\\Users\\|[A-Z0-9]{5}(-[A-Z0-9]{5}){4})' "$repo_dir"; then
    printf 'Possible private path or product key found.\n'
    status=1
fi

if [[ "$status" -ne 0 ]]; then
    exit "$status"
fi

printf 'Public-tree audit passed.\n'

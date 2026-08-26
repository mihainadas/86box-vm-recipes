#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_dir="$(git -C "$script_dir" rev-parse --show-toplevel)"
max_blob_size=$((5 * 1024 * 1024))
status=0

umask 077
blob_file="$(mktemp "${TMPDIR:-/tmp}/86box-public-audit.XXXXXX")"
cleanup() {
    rm -f -- "$blob_file"
}
trap cleanup EXIT

report() {
    printf '%s: ' "$1" >&2
    printf '%q\n' "$2" >&2
    status=1
}

allowed_path() {
    case "$1" in
        .gitattributes | .gitignore | LICENSE | \
            .github/CODEOWNERS | \
            *.md | *.cfg | *.json | *.toml | *.py | *.sh | *.command | \
            *.yml | *.yaml)
            return 0
            ;;
        machines/*/boot-floppy/FDCONFIG.SYS | \
            machines/*/boot-floppy/FDAUTO.BAT)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

allowed_executable() {
    case "$1" in
        *.py | *.sh | *.command)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

has_binary_magic() {
    local magic

    magic="$(od -An -tx1 -N 16 "$blob_file" | tr -d ' \n')"
    case "$magic" in
        7f454c46* | \
            4d5a* | \
            89504e470d0a1a0a* | \
            ffd8ff* | \
            474946383761* | 474946383961* | \
            25504446* | \
            504b0304* | 504b0506* | 504b0708* | \
            1f8b* | \
            377abcaf271c* | \
            52617221* | \
            cafebabe* | feedface* | feedfacf* | cefaedfe* | cffaedfe*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# These are assembled so the audit does not mistake its own detection strings for
# leaked values. Blob contents are never printed. Binary detection is deliberately
# heuristic: the NUL check and common magic signatures are defense in depth for a
# repository whose allowed formats are expected to be ordinary text.
lfs_marker='version https://git-lfs.github.com/spec/'"v1"
private_path_pattern='/(Users|home)/[^/[:space:]]+(/[^[:space:]]*)?|/'"root"'(/[^[:space:]]*)?|[A-Za-z]:[\\/]+Users[\\/]+[^\\/[:space:]]+([\\/][^[:space:]]*)?|[\\][\\][^\\/[:space:]]+[\\/]+([^\\/[:space:]]+[\\/]+)*Users[\\/]+[^\\/[:space:]]+([\\/][^[:space:]]*)?'
product_key_pattern='[[:alnum:]]{5}(-[[:alnum:]]{5}){4}|[0-9]{5}-OEM-[0-9]{7}-[0-9]{5}|[0-9]{3}-[0-9]{7}'

while IFS= read -r -d '' entry; do
    metadata="${entry%%$'\t'*}"
    path="${entry#*$'\t'}"
    read -r mode oid stage <<< "$metadata"

    if [[ "$stage" != "0" ]]; then
        report "Unmerged index entry" "$path"
        continue
    fi

    case "$mode" in
        120000)
            report "Tracked symlink is forbidden" "$path"
            continue
            ;;
        160000)
            report "Tracked submodule is forbidden" "$path"
            continue
            ;;
        100644 | 100755)
            ;;
        *)
            report "Unexpected Git mode $mode" "$path"
            continue
            ;;
    esac

    if ! allowed_path "$path"; then
        report "Unexpected public file type" "$path"
    fi

    if [[ "$mode" == "100755" ]] && ! allowed_executable "$path"; then
        report "Unexpected executable file" "$path"
    fi

    if ! blob_size="$(git -C "$repo_dir" cat-file -s "$oid" 2>/dev/null)"; then
        report "Unreadable Git blob" "$path"
        continue
    fi
    if [[ ! "$blob_size" =~ ^[0-9]+$ ]]; then
        report "Unreadable Git blob" "$path"
        continue
    fi
    if ((blob_size > max_blob_size)); then
        report "Oversized blob (${blob_size} bytes)" "$path"
        continue
    fi

    if ! git -C "$repo_dir" cat-file blob "$oid" > "$blob_file"; then
        report "Unreadable Git blob" "$path"
        continue
    fi

    if grep -Fq "$lfs_marker" "$blob_file"; then
        report "Git LFS pointer is forbidden" "$path"
    fi

    if ((blob_size > 0)) && ! LC_ALL=C grep -Iq '' "$blob_file"; then
        report "Binary content is forbidden" "$path"
    elif has_binary_magic; then
        report "Binary file signature is forbidden" "$path"
    fi

    if LC_ALL=C grep -Eiq "$private_path_pattern" "$blob_file"; then
        report "Possible private host path" "$path"
    fi

    if LC_ALL=C grep -Eiq "$product_key_pattern" "$blob_file"; then
        report "Possible product key" "$path"
    fi
done < <(git -C "$repo_dir" ls-files --stage -z)

if [[ "$status" -ne 0 ]]; then
    exit "$status"
fi

printf 'Public-tree audit passed.\n'

#!/usr/bin/env bash

set -euo pipefail

# Fixture repositories must not inherit repository-selection overrides from a
# caller such as a pre-commit hook.
audit_index_override="${GIT_INDEX_FILE:-}"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
audit_source="$repo_dir/scripts/audit-public-tree.sh"
fixture_root="$(mktemp -d "${TMPDIR:-/tmp}/86box-audit-tests.XXXXXX")"
tests_run=0
fifo_writer_pids=("")
: > "$fixture_root/.audit-test-root"
audit_timeout_seconds=8

cleanup() {
    local pid

    for pid in "${fifo_writer_pids[@]}"; do
        if [[ -z "$pid" ]]; then
            continue
        fi
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    if [[ -d "$fixture_root" && -f "$fixture_root/.audit-test-root" ]]; then
        rm -rf -- "$fixture_root"
    fi
}
trap cleanup EXIT

new_fixture() {
    local name="$1"
    local fixture="$fixture_root/$name"

    mkdir -p "$fixture/scripts"
    git -C "$fixture" init --quiet
    cp "$audit_source" "$fixture/scripts/audit-public-tree.sh"
    chmod +x "$fixture/scripts/audit-public-tree.sh"
    printf '# Safe fixture\n' > "$fixture/README.md"
    git -C "$fixture" add README.md scripts/audit-public-tree.sh
    printf '%s\n' "$fixture"
}

make_sparse_file() {
    local path="$1"
    local size="$2"

    dd if=/dev/zero of="$path" bs=1 count=0 seek="$size" 2>/dev/null
}

run_audit() {
    local fixture="$1"
    local output="$2"
    local timeout_marker="$output.timeout"
    local audit_pid
    local watchdog_pid
    local audit_status

    "$fixture/scripts/audit-public-tree.sh" > "$output" 2>&1 &
    audit_pid=$!
    (
        sleep "$audit_timeout_seconds"
        if kill -0 "$audit_pid" 2>/dev/null; then
            : > "$timeout_marker"
            kill "$audit_pid" 2>/dev/null || true
        fi
    ) &
    watchdog_pid=$!

    if wait "$audit_pid"; then
        audit_status=0
    else
        audit_status=$?
    fi
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true

    if [[ -e "$timeout_marker" ]]; then
        printf 'Audit exceeded %s seconds; it may have read a FIFO.\n' \
            "$audit_timeout_seconds" >> "$output"
        return 124
    fi
    return "$audit_status"
}

start_fifo_sentinel() {
    local fifo="$1"
    local read_marker="$2"

    (
        printf 'THIS-FIFO-MUST-NOT-BE-READ\n' > "$fifo"
        : > "$read_marker"
    ) &
    fifo_writer_pids+=("$!")
}

assert_fifo_unread() {
    local read_marker="$1"
    local writer_pid="${fifo_writer_pids[${#fifo_writer_pids[@]} - 1]}"
    local label="$2"

    kill "$writer_pid" 2>/dev/null || true
    wait "$writer_pid" 2>/dev/null || true
    fifo_writer_pids[${#fifo_writer_pids[@]} - 1]=""
    if [[ -e "$read_marker" ]]; then
        printf 'FAIL: %s read a private FIFO\n' "$label" >&2
        exit 1
    fi
}

expect_pass() {
    local name="$1"
    local fixture="$2"
    local output="$fixture_root/$name.output"

    if ! run_audit "$fixture" "$output"; then
        printf 'FAIL: %s unexpectedly failed\n' "$name" >&2
        sed 's/^/  /' "$output" >&2
        exit 1
    fi
    tests_run=$((tests_run + 1))
}

expect_fail() {
    local name="$1"
    local fixture="$2"
    local expected="$3"
    local output="$fixture_root/$name.output"

    if run_audit "$fixture" "$output"; then
        printf 'FAIL: %s unexpectedly passed\n' "$name" >&2
        exit 1
    fi
    if ! grep -Fq "$expected" "$output"; then
        printf 'FAIL: %s did not report %s\n' "$name" "$expected" >&2
        sed 's/^/  /' "$output" >&2
        exit 1
    fi
    tests_run=$((tests_run + 1))
}

fixture="$(new_fixture safe-index-with-private-ignored-files)"
printf '**/*.hdd\n**/*.iso\n**/*.img\n**/*.nvr\n**/*.sys\n**/*.SYS\n**/screenshots/*\n**/printer/*\n' > "$fixture/.gitignore"
git -C "$fixture" add .gitignore
mkdir -p "$fixture/private/drivers" "$fixture/private/screenshots" "$fixture/private/printer"
make_sparse_file "$fixture/private/windows95.hdd" $((6 * 1024 * 1024))
printf 'private ISO\n' > "$fixture/private/windows95.iso"
printf 'private image\n' > "$fixture/private/startup.img"
printf 'private NVR\n' > "$fixture/private/machine.nvr"
printf 'private driver\n' > "$fixture/private/drivers/PRIVATE.SYS"
printf 'private screenshot\n' > "$fixture/private/screenshots/test.png"
printf 'private print\n' > "$fixture/private/printer/page.png"
mkfifo "$fixture/private/never-read.hdd"
fifo_marker="$fixture_root/ignored-fifo-read"
start_fifo_sentinel "$fixture/private/never-read.hdd" "$fifo_marker"
expect_pass safe-index-with-private-ignored-files "$fixture"
assert_fifo_unread "$fifo_marker" safe-index-with-private-ignored-files

fixture="$(new_fixture fdconfig-text-exception)"
mkdir -p "$fixture/machines/test/boot-floppy"
printf '!COUNTRY=001,858,A:\\FREEDOS\\BIN\\COUNTRY.SYS\n' > "$fixture/machines/test/boot-floppy/FDCONFIG.SYS"
git -C "$fixture" add machines/test/boot-floppy/FDCONFIG.SYS
expect_pass fdconfig-text-exception "$fixture"

fixture="$(new_fixture recipe-manifest-and-validator)"
printf 'schema_version = 1\nslug = "test"\n' > "$fixture/recipe.toml"
printf '#!/usr/bin/env python3\nprint("safe fixture")\n' > "$fixture/validate.py"
chmod +x "$fixture/validate.py"
git -C "$fixture" add recipe.toml validate.py
expect_pass recipe-manifest-and-validator "$fixture"

fixture="$(new_fixture codeowners)"
mkdir -p "$fixture/.github"
printf '* @fixture-maintainer\n' > "$fixture/.github/CODEOWNERS"
git -C "$fixture" add .github/CODEOWNERS
expect_pass codeowners "$fixture"

fixture="$(new_fixture applications-path)"
printf '86Box is installed below /Applications/86Box.app.\n' > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_pass applications-path "$fixture"

fixture="$(new_fixture nul-safe-filenames)"
unusual_name=$'notes with spaces\tand a newline\nremain-safe.md'
printf 'Safe unusual filename.\n' > "$fixture/$unusual_name"
git -C "$fixture" add "$unusual_name"
expect_pass nul-safe-filenames "$fixture"

fixture="$(new_fixture forbidden-extension)"
printf 'not real media\n' > "$fixture/disk.iso"
git -C "$fixture" add disk.iso
expect_fail forbidden-extension "$fixture" 'Unexpected public file type: disk.iso'

fixture="$(new_fixture renamed-binary)"
printf '\211PNG\r\n\032\nsynthetic' > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail renamed-binary "$fixture" 'Binary file signature is forbidden: notes.md'

fixture="$(new_fixture nul-binary)"
printf '\0synthetic binary' > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail nul-binary "$fixture" 'Binary content is forbidden: notes.md'

fixture="$(new_fixture oversized-blob)"
make_sparse_file "$fixture/oversized.md" $((5 * 1024 * 1024 + 1))
git -C "$fixture" add oversized.md
expect_fail oversized-blob "$fixture" 'Oversized blob'

fixture="$(new_fixture tracked-symlink)"
mkfifo "$fixture_root/symlink-target.txt"
fifo_marker="$fixture_root/symlink-fifo-read"
start_fifo_sentinel "$fixture_root/symlink-target.txt" "$fifo_marker"
ln -s "$fixture_root/symlink-target.txt" "$fixture/outside.md"
git -C "$fixture" add outside.md
expect_fail tracked-symlink "$fixture" 'Tracked symlink is forbidden: outside.md'
assert_fifo_unread "$fifo_marker" tracked-symlink

fixture="$(new_fixture tracked-submodule)"
git -C "$fixture" -c user.name=Fixture -c user.email=fixture.invalid@example.invalid \
    -c commit.gpgsign=false \
    commit --quiet --message='Fixture base'
commit_oid="$(git -C "$fixture" rev-parse HEAD)"
git -C "$fixture" update-index --add --cacheinfo "160000,$commit_oid,vendor/example"
expect_fail tracked-submodule "$fixture" 'Tracked submodule is forbidden: vendor/example'

fixture="$(new_fixture lfs-pointer)"
lfs_marker='version https://git-lfs.github.com/spec/'"v1"
printf '%s\noid sha256:%064d\nsize 42\n' "$lfs_marker" 0 > "$fixture/pointer.md"
git -C "$fixture" add pointer.md
expect_fail lfs-pointer "$fixture" 'Git LFS pointer is forbidden: pointer.md'

fixture="$(new_fixture unreadable-index-blob)"
existing_oid="$(git -C "$fixture" hash-object README.md)"
case "${existing_oid: -1}" in
    0) missing_oid="${existing_oid%?}1" ;;
    *) missing_oid="${existing_oid%?}0" ;;
esac
git -C "$fixture" update-index --add --info-only \
    --cacheinfo "100644,$missing_oid,missing.md"
expect_fail unreadable-index-blob "$fixture" 'Unreadable Git blob: missing.md'

fixture="$(new_fixture unexpected-executable)"
chmod +x "$fixture/README.md"
git -C "$fixture" add README.md
expect_fail unexpected-executable "$fixture" 'Unexpected executable file: README.md'

fixture="$(new_fixture private-path)"
private_path='/''Users/fixture/private/windows95.iso'
printf 'Media lives at %s\n' "$private_path" > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail private-path "$fixture" 'Possible private host path: notes.md'

fixture="$(new_fixture non-ascii-private-path)"
private_path='/''Users/'$'Jos\303\251''/private/windows95.iso'
printf 'Media lives at %s\n' "$private_path" > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail non-ascii-private-path "$fixture" 'Possible private host path: notes.md'

fixture="$(new_fixture root-private-path)"
private_path='/''root/private/windows95.iso'
printf 'Media lives at %s\n' "$private_path" > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail root-private-path "$fixture" 'Possible private host path: notes.md'

fixture="$(new_fixture windows-private-path)"
private_path='C:'"\\Users\\fixture\\private\\windows95.iso"
printf 'Media lives at %s\n' "$private_path" > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail windows-private-path "$fixture" 'Possible private host path: notes.md'

fixture="$(new_fixture windows-unc-private-path)"
printf -v backslash '\134'
private_path="${backslash}${backslash}server${backslash}Users${backslash}fixture${backslash}private${backslash}windows95.iso"
printf 'Media lives at %s\n' "$private_path" > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail windows-unc-private-path "$fixture" 'Possible private host path: notes.md'

fixture="$(new_fixture product-key)"
product_key='ABCDE-FGHIJ-KLMNO-'"PQRST-UVWXY"
printf 'Key: %s\n' "$product_key" > "$fixture/notes.md"
git -C "$fixture" add notes.md
expect_fail product-key "$fixture" 'Possible product key: notes.md'

actual_output="$fixture_root/actual-repository.output"
if [[ -n "$audit_index_override" ]]; then
    actual_audit_status=0
    GIT_INDEX_FILE="$audit_index_override" "$audit_source" \
        > "$actual_output" 2>&1 || actual_audit_status=$?
else
    actual_audit_status=0
    "$audit_source" > "$actual_output" 2>&1 || actual_audit_status=$?
fi
if [[ "$actual_audit_status" -ne 0 ]]; then
    printf 'FAIL: the actual repository audit did not pass\n' >&2
    sed 's/^/  /' "$actual_output" >&2
    exit 1
fi
tests_run=$((tests_run + 1))

printf 'Audit regression tests passed (%d cases).\n' "$tests_run"

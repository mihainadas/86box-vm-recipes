#!/usr/bin/env bash
# Generated fixture scripts intentionally use single-quoted source fragments.
# shellcheck disable=SC2016

set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
check_script="$repo_dir/scripts/check.sh"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/86box-check-cli.XXXXXX")"
tests_run=0
: > "$test_root/.check-cli-test-root"
mkdir "$test_root/outside checkout"

cleanup() {
    if [[ -d "$test_root" && -f "$test_root/.check-cli-test-root" ]]; then
        rm -rf -- "$test_root"
    fi
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

assert_status() {
    local expected="$1"
    local label="$2"

    if [[ "$last_status" -ne "$expected" ]]; then
        printf 'FAIL: %s returned %d instead of %d\n' \
            "$label" "$last_status" "$expected" >&2
        sed 's/^/  stdout: /' "$last_stdout" >&2
        sed 's/^/  stderr: /' "$last_stderr" >&2
        exit 1
    fi
}

make_shellcheck() {
    local tool_dir="$1"

    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'printf "shellcheck:%s\\n" "$1" >> "$CHECK_LOG"' \
        > "$tool_dir/shellcheck"
    chmod +x "$tool_dir/shellcheck"
}

link_system_tool() {
    local tool_dir="$1"
    local tool_name="$2"
    local tool_path

    tool_path="$(command -v "$tool_name")" || fail "$tool_name is required by the orchestration tests"
    case "$tool_path" in
        /*)
            ;;
        *)
            fail "cannot resolve an absolute path for $tool_name"
            ;;
    esac
    ln -s "$tool_path" "$tool_dir/$tool_name"
}

new_fixture() {
    local name="$1"
    local fixture="$test_root/$name"
    local tool_dir="$test_root/$name tools"
    local tool

    mkdir -p \
        "$fixture/scripts" \
        "$fixture/tests" \
        "$fixture/machines/1995-dream-486/boot-floppy" \
        "$fixture/extra" \
        "$tool_dir"
    cp "$check_script" "$fixture/scripts/check.sh"
    chmod +x "$fixture/scripts/check.sh"

    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'printf "audit\\n" >> "$CHECK_LOG"' \
        'case "${CHECK_MUTATION:-}" in' \
        '    dirty) printf "dirty-after\\n" > tracked.txt ;;' \
        '    staged)' \
        '        printf "index-after\\n" > staged.txt' \
        '        git add staged.txt' \
        '        printf "worktree-before\\n" > staged.txt' \
        '        ;;' \
        '    untracked) printf "untracked-after\\n" > local.txt ;;' \
        '    mode) chmod 700 mode.txt ;;' \
        '    type) rm tracked.txt; mkdir tracked.txt ;;' \
        '    failure) exit 17 ;;' \
        '    signal) kill -TERM "$PPID" ;;' \
        'esac' \
        > "$fixture/scripts/audit-public-tree.sh"
    chmod +x "$fixture/scripts/audit-public-tree.sh"

    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'printf "cli-self\\n" >> "$CHECK_LOG"' \
        > "$fixture/tests/check-orchestration.sh"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'printf "audit-tests\\n" >> "$CHECK_LOG"' \
        > "$fixture/tests/audit-public-tree.sh"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'printf "boot\\n" >> "$CHECK_LOG"' \
        > "$fixture/tests/boot-floppy-builder.sh"
    printf '%s\n' '#!/usr/bin/env bash' 'exit 0' \
        > "$fixture/machines/1995-dream-486/boot-floppy/make-floppy.sh"
    printf '%s\n' '#!/usr/bin/env bash' 'exit 0' \
        > "$fixture/extra/covered.sh"
    printf '%s\n' '#!/bin/zsh' 'exit 0' \
        > "$fixture/machines/1995-dream-486/launch-macos.command"
    printf '%s\n' '# validator placeholder' > "$fixture/scripts/validate-recipes.py"
    printf '%s\n' 'tracked-base' > "$fixture/tracked.txt"
    printf '%s\n' 'staged-base' > "$fixture/staged.txt"
    printf '%s\n' 'mode-base' > "$fixture/mode.txt"
    printf '%s\n' 'private/' > "$fixture/.gitignore"
    chmod +x \
        "$fixture/tests/check-orchestration.sh" \
        "$fixture/tests/audit-public-tree.sh" \
        "$fixture/tests/boot-floppy-builder.sh" \
        "$fixture/machines/1995-dream-486/boot-floppy/make-floppy.sh" \
        "$fixture/extra/covered.sh"

    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'if [[ "${1:-}" == "-c" ]]; then' \
        '    exit "${CHECK_PYTHON_STATUS:-0}"' \
        'fi' \
        'printf "python:%s\\n" "$*" >> "$CHECK_LOG"' \
        > "$tool_dir/python3"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'printf "%s\\n" "${CHECK_HOST:-Darwin}"' \
        > "$tool_dir/uname"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'printf "zsh:%s\\n" "$*" >> "$CHECK_LOG"' \
        > "$tool_dir/zsh"
    for tool in mformat mmd mcopy mdir; do
        printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$tool_dir/$tool"
    done
    make_shellcheck "$tool_dir"
    chmod +x "$tool_dir"/*
    for tool in bash git dirname mktemp stat readlink cmp rm chmod mkdir cat; do
        link_system_tool "$tool_dir" "$tool"
    done
    if command -v shasum >/dev/null 2>&1; then
        link_system_tool "$tool_dir" shasum
    else
        link_system_tool "$tool_dir" sha256sum
    fi

    git -C "$fixture" init --quiet
    git -C "$fixture" config user.name 'Check Fixture'
    git -C "$fixture" config user.email 'check-fixture@example.invalid'
    git -C "$fixture" config commit.gpgsign false
    git -C "$fixture" add .
    git -C "$fixture" commit --quiet -m fixture

    printf '%s\n' "$fixture"
}

run_fixture() {
    local name="$1"
    local fixture="$2"
    shift 2
    local fixture_name
    local tool_dir
    local temp_dir="$test_root/$name state"
    local log="$test_root/$name.log"

    fixture_name="$(basename "$fixture")"
    tool_dir="$test_root/$fixture_name tools"
    mkdir -p "$temp_dir"
    : > "$log"
    last_stdout="$test_root/$name.stdout"
    last_stderr="$test_root/$name.stderr"
    last_state_dir="$temp_dir"
    last_log="$log"
    last_status=0
    (
        cd "$test_root/outside checkout"
        env \
            PATH="$tool_dir" \
            TMPDIR="$temp_dir" \
            CHECK_LOG="$log" \
            CHECK_HOST="${run_host:-Darwin}" \
            CHECK_MUTATION="${run_mutation:-}" \
            CI="${run_ci:-}" \
            "$fixture/scripts/check.sh" "$@"
    ) > "$last_stdout" 2> "$last_stderr" || last_status=$?
    run_host=''
    run_mutation=''
    run_ci=''
}

assert_guard_failure() {
    local name="$1"
    local fixture="$2"
    local mutation="$3"

    run_mutation="$mutation"
    run_fixture "$name" "$fixture" repository-safety
    assert_status 1 "$name"
    grep -Fq 'checks changed repository content, type, mode, or index state' \
        "$last_stderr" || fail "$name lacked the state-change diagnostic"
    tests_run=$((tests_run + 1))
}

# The real entry point resolves its repository before changing directory.
(
    cd "$test_root/outside checkout"
    "$check_script" --help > "$test_root/help.stdout" 2> "$test_root/help.stderr"
)
[[ ! -s "$test_root/help.stderr" ]] || fail '--help wrote to stderr'
grep -Fq 'Usage: scripts/check.sh' "$test_root/help.stdout" || fail '--help omitted usage'
for suite in all repository-safety boot-floppy macos-launcher; do
    grep -Fq "$suite" "$test_root/help.stdout" || fail "--help omitted $suite"
done
tests_run=$((tests_run + 1))

unknown_status=0
"$check_script" definitely-not-a-suite > "$test_root/unknown.stdout" 2> "$test_root/unknown.stderr" || unknown_status=$?
[[ "$unknown_status" -eq 2 ]] || fail "unknown argument returned $unknown_status instead of 2"
grep -Fq 'unknown argument: definitely-not-a-suite' "$test_root/unknown.stderr" || fail 'unknown argument lacked a diagnostic'
tests_run=$((tests_run + 1))

extra_status=0
"$check_script" repository-safety boot-floppy > "$test_root/extra.stdout" 2> "$test_root/extra.stderr" || extra_status=$?
[[ "$extra_status" -eq 2 ]] || fail "multiple suites returned $extra_status instead of 2"
grep -Fq 'only one suite may be selected' "$test_root/extra.stderr" || fail 'multiple suites lacked a diagnostic'
tests_run=$((tests_run + 1))

# Repository-safety dispatch is ordered, path-independent, non-recursive, and
# discovers every tracked Bash script instead of maintaining a second list.
fixture="$(new_fixture repository-order)"
run_fixture repository-order "$fixture" repository-safety
assert_status 0 repository-order
grep -E '^(audit|cli-self|audit-tests|python:)' "$last_log" > "$test_root/repository-order.filtered"
printf '%s\n' \
    audit \
    cli-self \
    audit-tests \
    'python:-B scripts/validate-recipes.py' \
    "python:-B -m unittest discover -s tests -p test_*.py -v" \
    > "$test_root/repository-order.expected"
cmp "$test_root/repository-order.expected" "$test_root/repository-order.filtered" || fail 'repository-safety dispatch order drifted'
[[ "$(grep -Fc 'cli-self' "$last_log")" -eq 1 ]] || fail 'orchestration test recursively invoked itself'
grep -Fq 'shellcheck:extra/covered.sh' "$last_log" || fail 'tracked Bash script was omitted from ShellCheck'
tests_run=$((tests_run + 1))

# all runs safety, floppy, then launcher on macOS.
fixture="$(new_fixture all-macos)"
run_fixture all-macos "$fixture" all
assert_status 0 all-macos
grep -E '^(audit|cli-self|audit-tests|python:|boot|zsh:)' "$last_log" > "$test_root/all-macos.filtered"
printf '%s\n' \
    audit \
    cli-self \
    audit-tests \
    'python:-B scripts/validate-recipes.py' \
    "python:-B -m unittest discover -s tests -p test_*.py -v" \
    boot \
    'zsh:-n machines/1995-dream-486/launch-macos.command' \
    'python:-B tests/macos-launcher-contract.py -v' \
    > "$test_root/all-macos.expected"
cmp "$test_root/all-macos.expected" "$test_root/all-macos.filtered" || fail 'macOS all-suite order drifted'
tests_run=$((tests_run + 1))

# Non-macOS all runs supported suites and explicitly reports launcher gating.
fixture="$(new_fixture all-linux)"
run_host=Linux
run_fixture all-linux "$fixture" all
assert_status 0 all-linux
grep -Fq 'SKIP: macos-launcher tests require macOS.' "$last_stderr" || fail 'Linux all did not report launcher gating'
if grep -Eq '^(zsh:|python:-B tests/macos-launcher-contract)' "$last_log"; then
    fail 'Linux all dispatched the macOS launcher'
fi
grep -Fxq boot "$last_log" || fail 'Linux all omitted boot-floppy tests'
tests_run=$((tests_run + 1))

# ShellCheck is optional locally, strict in CI-style runs, and checked before
# any suite component starts when required.
fixture="$(new_fixture optional-shellcheck)"
tool_dir="$test_root/optional-shellcheck tools"
rm -f -- "$tool_dir/shellcheck"
system_shellcheck_dir="$test_root/system shellcheck"
mkdir "$system_shellcheck_dir"
printf '%s\n' '#!/usr/bin/env bash' 'exit 99' > "$system_shellcheck_dir/shellcheck"
chmod +x "$system_shellcheck_dir/shellcheck"
PATH="$system_shellcheck_dir:$PATH" run_fixture optional-shellcheck "$fixture" repository-safety
assert_status 0 optional-shellcheck
grep -Fq 'SKIP: ShellCheck is not installed.' "$last_stderr" || fail 'optional ShellCheck skip lacked guidance'
make_shellcheck "$tool_dir"
tests_run=$((tests_run + 1))

rm -f -- "$tool_dir/shellcheck"
run_fixture strict-shellcheck "$fixture" --require-optional repository-safety
assert_status 1 strict-shellcheck
grep -Fq 'ShellCheck is required in strict mode.' "$last_stderr" || fail 'strict ShellCheck failure lacked guidance'
[[ ! -s "$last_log" ]] || fail 'strict dependency failure was not fail-fast'
tests_run=$((tests_run + 1))

# Explicit suites make their own core prerequisites mandatory.
fixture="$(new_fixture selected-prerequisite)"
rm -f -- "$test_root/selected-prerequisite tools/mformat"
run_fixture selected-boot-missing "$fixture" boot-floppy
assert_status 1 selected-boot-missing
grep -Fq 'mformat is required for boot-floppy tests.' "$last_stderr" || fail 'boot prerequisite failure lacked guidance'
[[ ! -s "$last_log" ]] || fail 'boot suite ran after a missing prerequisite'
tests_run=$((tests_run + 1))

run_host=Linux
run_fixture selected-macos-linux "$fixture" macos-launcher
assert_status 1 selected-macos-linux
grep -Fq 'macos-launcher suite requires macOS' "$last_stderr" || fail 'macOS host failure lacked guidance'
[[ ! -s "$last_log" ]] || fail 'macOS suite ran on Linux'
tests_run=$((tests_run + 1))

# CI refuses a dirty checkout before dispatch.
fixture="$(new_fixture ci-dirty)"
printf '%s\n' dirty > "$fixture/tracked.txt"
run_ci=true
run_fixture ci-dirty "$fixture" repository-safety
assert_status 1 ci-dirty
grep -Fq 'CI must start from a clean Git checkout' "$last_stderr" || fail 'dirty CI failure lacked guidance'
[[ ! -s "$last_log" ]] || fail 'dirty CI checkout dispatched a suite'
tests_run=$((tests_run + 1))

# Exact state snapshots catch changes hidden by an already-dirty porcelain code.
fixture="$(new_fixture dirty-tracked-mutation)"
printf '%s\n' dirty-before > "$fixture/tracked.txt"
assert_guard_failure dirty-tracked-mutation "$fixture" dirty

fixture="$(new_fixture staged-mutation)"
printf '%s\n' index-before > "$fixture/staged.txt"
git -C "$fixture" add staged.txt
printf '%s\n' worktree-before > "$fixture/staged.txt"
assert_guard_failure staged-mutation "$fixture" staged

fixture="$(new_fixture untracked-mutation)"
printf '%s\n' untracked-before > "$fixture/local.txt"
assert_guard_failure untracked-mutation "$fixture" untracked

fixture="$(new_fixture mode-mutation)"
chmod 600 "$fixture/mode.txt"
assert_guard_failure mode-mutation "$fixture" mode

fixture="$(new_fixture type-mutation)"
printf '%s\n' dirty-before > "$fixture/tracked.txt"
assert_guard_failure type-mutation "$fixture" type

# Ignored private FIFOs are not enumerated or opened by the state guard.
fixture="$(new_fixture ignored-fifo)"
mkdir "$fixture/private"
mkfifo "$fixture/private/runtime.hdd"
fifo_read_marker="$test_root/ignored-fifo.read"
(
    printf 'private runtime bytes\n' > "$fixture/private/runtime.hdd"
    : > "$fifo_read_marker"
) 2>/dev/null &
fifo_writer=$!
run_fixture ignored-fifo "$fixture" repository-safety
kill "$fifo_writer" 2>/dev/null || true
wait "$fifo_writer" 2>/dev/null || true
assert_status 0 ignored-fifo
[[ ! -e "$fifo_read_marker" ]] || fail 'state guard opened an ignored private FIFO'
tests_run=$((tests_run + 1))

# In-repository temporary state would observe itself and is rejected.
fixture="$(new_fixture in-repo-tmpdir)"
mkdir "$fixture/local-tmp"
in_repo_status=0
env PATH="$test_root/in-repo-tmpdir tools" \
    TMPDIR="$fixture/local-tmp" CHECK_LOG="$test_root/in-repo-tmpdir.log" \
    "$fixture/scripts/check.sh" repository-safety \
    > "$test_root/in-repo-tmpdir.stdout" 2> "$test_root/in-repo-tmpdir.stderr" || in_repo_status=$?
[[ "$in_repo_status" -eq 1 ]] || fail 'in-repository TMPDIR did not fail'
grep -Fq 'TMPDIR must be outside the repository' "$test_root/in-repo-tmpdir.stderr" || fail 'in-repository TMPDIR lacked guidance'
tests_run=$((tests_run + 1))

# Component failures and handled signals both remove the external state root.
fixture="$(new_fixture failure-cleanup)"
run_mutation=failure
run_fixture failure-cleanup "$fixture" repository-safety
assert_status 17 failure-cleanup
leftover_state="$(find "$last_state_dir" -maxdepth 1 -name '86box-check-state.*' -print -quit)"
[[ -z "$leftover_state" ]] || fail "failure left check state behind: $leftover_state"
tests_run=$((tests_run + 1))

fixture="$(new_fixture signal-cleanup)"
run_mutation=signal
run_fixture signal-cleanup "$fixture" repository-safety
[[ "$last_status" -ne 0 ]] || fail 'TERM did not fail the check command'
leftover_state="$(find "$last_state_dir" -maxdepth 1 -name '86box-check-state.*' -print -quit)"
[[ -z "$leftover_state" ]] || fail "TERM left check state behind: $leftover_state"
tests_run=$((tests_run + 1))

printf 'Check command orchestration tests passed (%d cases).\n' "$tests_run"

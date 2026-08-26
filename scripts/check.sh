#!/usr/bin/env bash

set -euo pipefail

if ! command -v git >/dev/null 2>&1; then
    printf 'check.sh: Git is required. Install it with your system package manager.\n' >&2
    exit 1
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_dir="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" || {
    printf 'Unable to locate the Git repository containing %s.\n' "$script_dir" >&2
    exit 2
}

strict_optional=0
suite=""

usage() {
    cat <<'EOF'
Usage: scripts/check.sh [--require-optional] [SUITE]

Run the repository's project-owned checks from any working directory.

Suites:
  all                Run every suite supported by this host (default)
  repository-safety  Check patches, public contents, scripts, and manifests
  boot-floppy        Exercise the boot-floppy builder with synthetic inputs
  software-media     Validate the catalog and build a synthetic companion ISO
  macos-launcher     Exercise the launcher contract with synthetic inputs

Options:
  --require-optional  Fail if an optional local tool such as ShellCheck is absent
  -h, --help          Show this help
EOF
}

die() {
    printf 'check.sh: %s\n' "$1" >&2
    exit "${2:-1}"
}

while (($# > 0)); do
    case "$1" in
        --require-optional)
            strict_optional=1
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        all | repository-safety | boot-floppy | software-media | macos-launcher)
            if [[ -n "$suite" ]]; then
                die "only one suite may be selected" 2
            fi
            suite="$1"
            ;;
        *)
            usage >&2
            die "unknown argument: $1" 2
            ;;
    esac
    shift
done

suite="${suite:-all}"
cd "$repo_dir"

umask 077
repo_physical="$(pwd -P)"
temp_base="${TMPDIR:-/tmp}"
if [[ ! -d "$temp_base" ]]; then
    die "temporary directory does not exist: $temp_base"
fi
temp_physical="$(cd "$temp_base" && pwd -P)"
case "$temp_physical/" in
    "$repo_physical/"*)
        die 'TMPDIR must be outside the repository so check state is not self-observed'
        ;;
esac

state_root="$(mktemp -d "$temp_physical/86box-check-state.XXXXXX")"
: > "$state_root/.check-state-root"

cleanup_state() {
    if [[ -d "$state_root" && -f "$state_root/.check-state-root" ]]; then
        rm -rf -- "$state_root"
    fi
}

handle_signal() {
    local signal_name="$1"

    trap - EXIT HUP INT TERM
    cleanup_state
    kill -s "$signal_name" "$$"
}

# Register cleanup before inspecting repository contents so every later error
# and handled signal removes the external state directory.
trap cleanup_state EXIT
trap 'handle_signal HUP' HUP
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM

path_mode() {
    local path="$1"
    local mode

    if mode="$(stat -f '%Lp' "$path" 2>/dev/null)"; then
        printf '%s\n' "$mode"
    elif mode="$(stat -c '%a' "$path" 2>/dev/null)"; then
        printf '%s\n' "$mode"
    else
        return 1
    fi
}

snapshot_path() {
    local category="$1"
    local relative_path="$2"
    local absolute_path="$repo_physical/$relative_path"
    local kind
    local mode='-'
    local identity='-'

    if [[ -L "$absolute_path" ]]; then
        kind='symlink'
        mode="$(path_mode "$absolute_path")" || die "cannot inspect mode: $relative_path"
        identity="$(readlink "$absolute_path" | git hash-object --stdin)" || die "cannot inspect symlink: $relative_path"
    elif [[ -f "$absolute_path" ]]; then
        kind='regular'
        mode="$(path_mode "$absolute_path")" || die "cannot inspect mode: $relative_path"
        identity="$(git hash-object --no-filters -- "$absolute_path")" || die "cannot hash file: $relative_path"
    elif [[ -d "$absolute_path" ]]; then
        kind='directory'
        mode="$(path_mode "$absolute_path")" || die "cannot inspect mode: $relative_path"
    elif [[ -p "$absolute_path" ]]; then
        kind='fifo'
        mode="$(path_mode "$absolute_path")" || die "cannot inspect mode: $relative_path"
    elif [[ -e "$absolute_path" ]]; then
        kind='other'
        mode="$(path_mode "$absolute_path")" || die "cannot inspect mode: $relative_path"
    else
        kind='missing'
    fi

    printf '%s\0%s\0%s\0%s\0%s\0' \
        "$category" "$relative_path" "$kind" "$mode" "$identity"
}

snapshot_repository() {
    local output="$1"
    local relative_path
    local tracked_paths="$state_root/tracked-paths"
    local untracked_paths="$state_root/untracked-paths"

    {
        printf 'index-stage\0'
        git ls-files --stage -z
        printf 'index-flags\0'
        git ls-files -v -z
    } > "$output"

    git ls-files -z > "$tracked_paths"
    while IFS= read -r -d '' relative_path; do
        snapshot_path tracked "$relative_path" >> "$output"
    done < "$tracked_paths"

    # --exclude-standard is the safety boundary: ignored disks, media, ROMs,
    # and other private runtime files are never opened or hashed here.
    git ls-files --others --exclude-standard -z > "$untracked_paths"
    while IFS= read -r -d '' relative_path; do
        snapshot_path untracked "$relative_path" >> "$output"
    done < "$untracked_paths"
}

initial_state="$state_root/initial"
final_state="$state_root/final"
snapshot_repository "$initial_state"

finish() {
    local status=$?

    trap - EXIT HUP INT TERM
    if ! snapshot_repository "$final_state"; then
        printf 'check.sh: unable to verify final repository state\n' >&2
        status=1
    elif ! cmp -s "$initial_state" "$final_state"; then
        printf 'check.sh: checks changed repository content, type, mode, or index state:\n' >&2
        git status --short --untracked-files=all >&2 || true
        status=1
    fi
    cleanup_state
    exit "$status"
}
trap finish EXIT

if [[ "${CI:-}" == "true" ]] && [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    die 'CI must start from a clean Git checkout'
fi

shell_scripts=()
shell_script_paths="$state_root/shell-script-paths"
git ls-files -z -- '*.sh' > "$shell_script_paths"
while IFS= read -r -d '' shell_script; do
    shell_scripts+=("$shell_script")
done < "$shell_script_paths"
for shell_script in scripts/check.sh tests/check-orchestration.sh; do
    shell_script_seen=0
    for tracked_shell_script in "${shell_scripts[@]}"; do
        if [[ "$tracked_shell_script" == "$shell_script" ]]; then
            shell_script_seen=1
            break
        fi
    done
    if [[ "$shell_script_seen" -eq 0 ]]; then
        shell_scripts+=("$shell_script")
    fi
done

have_command() {
    command -v "$1" >/dev/null 2>&1
}

require_command() {
    local command_name="$1"
    local guidance="$2"

    if ! have_command "$command_name"; then
        die "$command_name is required. $guidance"
    fi
}

require_python() {
    require_command python3 'Install Python 3.11 or newer from https://www.python.org/downloads/.'
    if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        die 'Python 3.11 or newer is required. Install it from https://www.python.org/downloads/.'
    fi
}

check_shellcheck() {
    local path

    if ! have_command shellcheck; then
        if [[ "$strict_optional" -eq 1 ]]; then
            die 'ShellCheck is required in strict mode. With Homebrew, run: brew install shellcheck'
        fi
        printf 'SKIP: ShellCheck is not installed. With Homebrew, run: brew install shellcheck\n' >&2
        return 0
    fi

    for path in "${shell_scripts[@]}"; do
        shellcheck "$path"
    done
}

preflight_repository_safety() {
    require_command git 'Install Git with your system package manager.'
    require_command bash 'Install Bash with your system package manager.'
    require_python
    if [[ "$strict_optional" -eq 1 ]] && ! have_command shellcheck; then
        die 'ShellCheck is required in strict mode. With Homebrew, run: brew install shellcheck'
    fi
}

run_repository_safety() {
    local empty_tree
    local path

    printf '\n==> Repository safety\n'
    preflight_repository_safety

    empty_tree="$(git hash-object -t tree /dev/null)"
    git diff --check "$empty_tree" HEAD
    git diff --check
    git diff --cached --check
    ./scripts/audit-public-tree.sh

    for path in "${shell_scripts[@]}"; do
        bash -n "$path"
    done
    check_shellcheck

    tests/check-orchestration.sh
    tests/audit-public-tree.sh
    python3 -B scripts/validate-recipes.py
    python3 -B -m unittest discover -s tests -p 'test_*.py' -v
}

missing_boot_tool() {
    local tool

    for tool in mformat mmd mcopy mdir; do
        if ! have_command "$tool"; then
            printf '%s\n' "$tool"
            return 0
        fi
    done
    if ! have_command shasum && ! have_command sha256sum; then
        printf '%s\n' 'a SHA-256 utility'
        return 0
    fi
    return 1
}

boot_tool_guidance() {
    if [[ "$1" == "a SHA-256 utility" ]]; then
        printf '%s\n' 'Install shasum or sha256sum (usually provided by Perl or GNU coreutils).'
    else
        printf '%s\n' 'Install mtools with Homebrew by running: brew install mtools'
    fi
}

run_boot_floppy() {
    local missing_tool

    printf '\n==> Boot-floppy builder\n'
    require_command bash 'Install Bash with your system package manager.'
    if missing_tool="$(missing_boot_tool)"; then
        die "$missing_tool is required for boot-floppy tests. $(boot_tool_guidance "$missing_tool")"
    fi
    tests/boot-floppy-builder.sh
}

preflight_software_media() {
    require_python
    require_command xorriso 'Install xorriso with Homebrew or your system package manager.'
}

run_software_media() {
    printf '\n==> Software media\n'
    preflight_software_media
    python3 -B scripts/software-media.py validate
    python3 -B -m unittest tests.test_software_media -v
    python3 -B tests/software-media-integration.py -v
}

preflight_macos_launcher() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        die 'the macos-launcher suite requires macOS'
    fi
    require_command zsh 'Use the zsh included with macOS.'
    require_python
}

run_macos_launcher() {
    printf '\n==> macOS launcher\n'
    preflight_macos_launcher
    zsh -n machines/1995-dream-486/launch-macos.command
    python3 -B tests/macos-launcher-contract.py -v
}

run_all() {
    local missing_tool

    # Strict mode validates every host-supported dependency before starting a
    # component, so a missing CI tool cannot surface after an expensive suite.
    if [[ "$strict_optional" -eq 1 ]]; then
        preflight_repository_safety
        if missing_tool="$(missing_boot_tool)"; then
            die "$missing_tool is required in strict mode. $(boot_tool_guidance "$missing_tool")"
        fi
        preflight_software_media
        if [[ "$(uname -s)" == "Darwin" ]]; then
            preflight_macos_launcher
        fi
    fi

    run_repository_safety

    if missing_tool="$(missing_boot_tool)"; then
        if [[ "$strict_optional" -eq 1 ]]; then
            die "$missing_tool is required in strict mode. $(boot_tool_guidance "$missing_tool")"
        fi
        printf 'SKIP: boot-floppy tests need %s. %s\n' "$missing_tool" "$(boot_tool_guidance "$missing_tool")" >&2
    else
        run_boot_floppy
    fi

    if ! have_command xorriso; then
        if [[ "$strict_optional" -eq 1 ]]; then
            die 'xorriso is required in strict mode. Install it with Homebrew or your system package manager.'
        fi
        printf 'SKIP: software-media tests need xorriso. Install it with Homebrew or your system package manager.\n' >&2
    else
        run_software_media
    fi

    if [[ "$(uname -s)" == "Darwin" ]]; then
        run_macos_launcher
    else
        printf 'SKIP: macos-launcher tests require macOS.\n' >&2
    fi
}

case "$suite" in
    all)
        run_all
        ;;
    repository-safety)
        run_repository_safety
        ;;
    boot-floppy)
        run_boot_floppy
        ;;
    software-media)
        run_software_media
        ;;
    macos-launcher)
        run_macos_launcher
        ;;
esac

printf '\nAll requested checks passed.\n'

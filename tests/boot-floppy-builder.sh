#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
builder="$repo_dir/machines/1995-dream-486/boot-floppy/make-floppy.sh"
boot_dir="$repo_dir/machines/1995-dream-486/boot-floppy"
umask 077
test_root="$(mktemp -d "${TMPDIR:-/tmp}/86box-floppy-tests.XXXXXX")"
tests_run=0
: > "$test_root/.floppy-test-root"
bounded_status=0
bounded_runner="$test_root/bounded-runner"

# Run the target in the foreground so it retains normal INT handling. The
# helper's watchdog keeps every signal regression bounded without relying on a
# platform-specific timeout command.
# shellcheck disable=SC2016
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'timeout_marker="$1"' \
    'watchdog_pid_file="$2"' \
    'shift 2' \
    'target_pid=$$' \
    '(' \
    '    sleep 8' \
    '    if kill -0 "$target_pid" 2>/dev/null; then' \
    '        : > "$timeout_marker"' \
    '        kill -KILL "$target_pid" 2>/dev/null || true' \
    '    fi' \
    ') &' \
    'printf "%d\\n" "$!" > "$watchdog_pid_file"' \
    'exec "$@"' > "$bounded_runner"
chmod +x "$bounded_runner"

cleanup() {
    if [[ -d "$test_root" && -f "$test_root/.floppy-test-root" ]]; then
        rm -rf -- "$test_root"
    fi
}
trap cleanup EXIT

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

sha256_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

file_size() {
    local size
    size="$(wc -c < "$1")"
    printf '%d\n' "$size"
}

assert_no_builder_temps() {
    local directory="$1"
    local leftover

    leftover="$(find "$directory" -maxdepth 1 \
        \( -name '.*.tmp.*' -o -name '.*.verify.*' \) -print -quit)"
    if [[ -n "$leftover" ]]; then
        fail "temporary builder file remains: $leftover"
    fi
}

expect_failure() {
    local name="$1"
    shift

    if "$@" > "$test_root/$name.stdout" 2> "$test_root/$name.stderr"; then
        fail "$name unexpectedly succeeded"
    fi
    tests_run=$((tests_run + 1))
}

run_bounded() {
    local name="$1"
    shift
    local timeout_marker="$test_root/$name.timeout"
    local watchdog_pid

    if "$bounded_runner" "$timeout_marker" "$test_root/$name.watchdog-pid" "$@" \
        > "$test_root/$name.stdout" 2> "$test_root/$name.stderr"; then
        bounded_status=0
    else
        bounded_status=$?
    fi
    read -r watchdog_pid < "$test_root/$name.watchdog-pid"
    kill "$watchdog_pid" 2>/dev/null || true
    if [[ -e "$timeout_marker" ]]; then
        fail "$name exceeded 8 seconds"
    fi
}

for tool in mformat mmd mcopy mdir; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf '%s is required to run the boot-floppy tests.\n' "$tool" >&2
        exit 1
    fi
done

input_dir="$test_root/source inputs"
driver_dir="$input_dir/private drivers"
base_image="$input_dir/freedos base.img"
mkdir -p "$driver_dir"

dd if=/dev/zero of="$base_image" bs=1024 count=1440 2>/dev/null
mformat -i "$base_image" -f 1440 -v FD14-BOOT ::
mmd -i "$base_image" ::/FREEDOS
mmd -i "$base_image" ::/FREEDOS/BIN
printf 'FreeCOM placeholder\n' > "$test_root/COMMAND.COM"
mcopy -i "$base_image" "$test_root/COMMAND.COM" ::/FREEDOS/BIN/COMMAND.COM
printf 'HimemX placeholder\n' > "$driver_dir/HIMEMX.EXE"
printf 'UDVD2 placeholder\n' > "$driver_dir/UDVD2.SYS"
printf 'SHSUCDX placeholder\n' > "$driver_dir/SHSUCDX.COM"
base_hash="$(sha256_file "$base_image")"
himemx_hash="$(sha256_file "$driver_dir/HIMEMX.EXE")"
udvd2_hash="$(sha256_file "$driver_dir/UDVD2.SYS")"
shsucdx_hash="$(sha256_file "$driver_dir/SHSUCDX.COM")"

# Happy path, including spaces in every caller-controlled path.
success_dir="$test_root/output with spaces"
output_image="$success_dir/win95 startup image.img"
mkdir -p "$success_dir"
"$builder" "$base_image" "$driver_dir" "$output_image" > "$test_root/happy.stdout"
[[ "$(file_size "$output_image")" -eq 1474560 ]] || fail 'happy-path image has the wrong size'
mdir -i "$output_image" :: >/dev/null
extract_dir="$test_root/extracted files"
mkdir -p "$extract_dir"
for guest_path in HIMEMX.EXE UDVD2.SYS SHSUCDX.COM FDCONFIG.SYS FDAUTO.BAT; do
    mcopy -i "$output_image" "::/$guest_path" "$extract_dir/$guest_path"
done
cmp "$driver_dir/HIMEMX.EXE" "$extract_dir/HIMEMX.EXE"
cmp "$driver_dir/UDVD2.SYS" "$extract_dir/UDVD2.SYS"
cmp "$driver_dir/SHSUCDX.COM" "$extract_dir/SHSUCDX.COM"
cmp "$boot_dir/FDCONFIG.SYS" "$extract_dir/FDCONFIG.SYS"
cmp "$boot_dir/FDAUTO.BAT" "$extract_dir/FDAUTO.BAT"
[[ "$(sha256_file "$base_image")" == "$base_hash" ]] || fail 'builder changed the source image'
assert_no_builder_temps "$success_dir"
tests_run=$((tests_run + 1))

# Wrong argument count.
expect_failure wrong-argument-count "$builder"

# A missing base and each missing private driver fail without output.
missing_dir="$test_root/missing inputs"
mkdir -p "$missing_dir"
missing_output="$missing_dir/output.img"
expect_failure missing-base "$builder" "$missing_dir/no base.img" "$driver_dir" "$missing_output"
[[ ! -e "$missing_output" && ! -L "$missing_output" ]] || fail 'missing-base test left output'

for missing_driver in HIMEMX.EXE UDVD2.SYS SHSUCDX.COM; do
    case_dir="$missing_dir/$missing_driver"
    case_drivers="$case_dir/drivers"
    mkdir -p "$case_drivers"
    cp "$driver_dir/HIMEMX.EXE" "$case_drivers/HIMEMX.EXE"
    cp "$driver_dir/UDVD2.SYS" "$case_drivers/UDVD2.SYS"
    cp "$driver_dir/SHSUCDX.COM" "$case_drivers/SHSUCDX.COM"
    rm -f -- "$case_drivers/$missing_driver"
    case_output="$case_dir/output.img"
    expect_failure "missing-$missing_driver" "$builder" "$base_image" "$case_drivers" "$case_output"
    [[ ! -e "$case_output" && ! -L "$case_output" ]] || fail "$missing_driver test left output"
    assert_no_builder_temps "$case_dir"
done

# Valid FAT without FreeCOM and an invalid same-size image both fail cleanly.
no_command_dir="$test_root/no command"
mkdir -p "$no_command_dir"
no_command_base="$no_command_dir/base.img"
dd if=/dev/zero of="$no_command_base" bs=1024 count=1440 2>/dev/null
mformat -i "$no_command_base" -f 1440 -v FD14-BOOT ::
mmd -i "$no_command_base" ::/FREEDOS
mmd -i "$no_command_base" ::/FREEDOS/BIN
no_command_hash="$(sha256_file "$no_command_base")"
expect_failure missing-command "$builder" "$no_command_base" "$driver_dir" "$no_command_dir/output.img"
[[ "$(sha256_file "$no_command_base")" == "$no_command_hash" ]] || fail 'missing-command test changed its base'
[[ ! -e "$no_command_dir/output.img" ]] || fail 'missing-command test left output'
assert_no_builder_temps "$no_command_dir"

invalid_dir="$test_root/invalid base"
mkdir -p "$invalid_dir"
invalid_base="$invalid_dir/base.img"
dd if=/dev/zero of="$invalid_base" bs=1024 count=1440 2>/dev/null
invalid_hash="$(sha256_file "$invalid_base")"
expect_failure invalid-base "$builder" "$invalid_base" "$driver_dir" "$invalid_dir/output.img"
[[ "$(sha256_file "$invalid_base")" == "$invalid_hash" ]] || fail 'invalid-base test changed its base'
[[ ! -e "$invalid_dir/output.img" ]] || fail 'invalid-base test left output'
assert_no_builder_temps "$invalid_dir"

# Missing mtools commands are reported before output is created.
real_bash="$(command -v bash)"
real_dirname="$(command -v dirname)"
real_mcopy="$(command -v mcopy)"
real_mdir="$(command -v mdir)"
real_link="$(command -v link)"
for missing_tool in mcopy mdir link; do
    tool_dir="$test_root/tools missing $missing_tool"
    mkdir -p "$tool_dir"
    ln -s "$real_bash" "$tool_dir/bash"
    ln -s "$real_dirname" "$tool_dir/dirname"
    if [[ "$missing_tool" != mcopy ]]; then
        ln -s "$real_mcopy" "$tool_dir/mcopy"
    fi
    if [[ "$missing_tool" == link ]]; then
        ln -s "$real_mdir" "$tool_dir/mdir"
    fi
    tool_output="$test_root/$missing_tool-output.img"
    expect_failure "missing-tool-$missing_tool" env PATH="$tool_dir" \
        "$builder" "$base_image" "$driver_dir" "$tool_output"
    [[ ! -e "$tool_output" ]] || fail "$missing_tool test left output"
    if [[ "$missing_tool" == link ]]; then
        grep -Fq 'system link utility is required' \
            "$test_root/missing-tool-link.stderr" || fail 'link diagnostic is inaccurate'
    else
        grep -Fq 'install the mtools package' \
            "$test_root/missing-tool-$missing_tool.stderr" || \
            fail "$missing_tool diagnostic is inaccurate"
    fi
done

checksum_tool_dir="$test_root/tools missing checksum"
mkdir -p "$checksum_tool_dir"
ln -s "$real_bash" "$checksum_tool_dir/bash"
ln -s "$real_dirname" "$checksum_tool_dir/dirname"
ln -s "$real_mcopy" "$checksum_tool_dir/mcopy"
ln -s "$real_mdir" "$checksum_tool_dir/mdir"
ln -s "$real_link" "$checksum_tool_dir/link"
checksum_tool_output="$test_root/checksum-tool-output.img"
expect_failure missing-tool-checksum env PATH="$checksum_tool_dir" \
    "$builder" "$base_image" "$driver_dir" "$checksum_tool_output"
[[ ! -e "$checksum_tool_output" ]] || fail 'missing checksum-tool test left output'
grep -Fq 'shasum or sha256sum is required' "$test_root/missing-tool-checksum.stderr" || \
    fail 'checksum-tool diagnostic is inaccurate'

# A deterministic mcopy failure after two successful writes leaves no partial image.
copy_failure_dir="$test_root/mid-copy failure"
copy_wrapper_dir="$copy_failure_dir/tools"
mkdir -p "$copy_wrapper_dir"
# The single-quoted strings are source code for the generated wrapper.
# shellcheck disable=SC2016
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'count=0' \
    'if [[ -f "$MCOPY_STATE" ]]; then read -r count < "$MCOPY_STATE"; fi' \
    'count=$((count + 1))' \
    'printf "%d\\n" "$count" > "$MCOPY_STATE"' \
    'if [[ "$count" -eq "$FAIL_ON_MCOPY_CALL" ]]; then exit 77; fi' \
    'exec "$REAL_MCOPY" "$@"' > "$copy_wrapper_dir/mcopy"
chmod +x "$copy_wrapper_dir/mcopy"
copy_failure_output="$copy_failure_dir/output.img"
expect_failure mid-copy env PATH="$copy_wrapper_dir:$PATH" \
    REAL_MCOPY="$real_mcopy" MCOPY_STATE="$copy_failure_dir/mcopy.state" \
    FAIL_ON_MCOPY_CALL=3 \
    "$builder" "$base_image" "$driver_dir" "$copy_failure_output"
[[ ! -e "$copy_failure_output" ]] || fail 'mid-copy failure left output'
[[ "$(sha256_file "$base_image")" == "$base_hash" ]] || fail 'mid-copy failure changed the base'
assert_no_builder_temps "$copy_failure_dir"

# Failure during the first validation extraction also leaves no partial image.
validation_failure_dir="$test_root/validation extraction failure"
mkdir -p "$validation_failure_dir"
validation_failure_output="$validation_failure_dir/output.img"
expect_failure validation-extraction env PATH="$copy_wrapper_dir:$PATH" \
    REAL_MCOPY="$real_mcopy" MCOPY_STATE="$validation_failure_dir/mcopy.state" \
    FAIL_ON_MCOPY_CALL=6 \
    "$builder" "$base_image" "$driver_dir" "$validation_failure_output"
[[ ! -e "$validation_failure_output" ]] || fail 'validation extraction failure left output'
assert_no_builder_temps "$validation_failure_dir"

# A checksum failure happens before publication and cannot leave an output.
checksum_failure_dir="$test_root/checksum failure"
checksum_wrapper_dir="$checksum_failure_dir/tools"
mkdir -p "$checksum_wrapper_dir"
printf '%s\n' '#!/usr/bin/env bash' 'exit 74' > "$checksum_wrapper_dir/shasum"
chmod +x "$checksum_wrapper_dir/shasum"
checksum_failure_output="$checksum_failure_dir/output.img"
expect_failure checksum-failure env PATH="$checksum_wrapper_dir:$PATH" \
    "$builder" "$base_image" "$driver_dir" "$checksum_failure_output"
[[ ! -e "$checksum_failure_output" ]] || fail 'checksum failure published output'
assert_no_builder_temps "$checksum_failure_dir"

# Every handled termination signal removes both temporary files before exit.
signal_wrapper_dir="$test_root/signal tools"
mkdir -p "$signal_wrapper_dir"
# The single-quoted strings are source code for the generated wrapper.
# shellcheck disable=SC2016
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'kill -s "$BUILDER_SIGNAL" "$PPID"' \
    'exit 77' > "$signal_wrapper_dir/mcopy"
chmod +x "$signal_wrapper_dir/mcopy"
for signal_case in HUP:129 INT:130 TERM:143; do
    signal_name="${signal_case%%:*}"
    expected_status="${signal_case#*:}"
    signal_dir="$test_root/signal $signal_name"
    mkdir -p "$signal_dir"
    signal_output="$signal_dir/output.img"
    run_bounded "signal-$signal_name" env PATH="$signal_wrapper_dir:$PATH" \
        BUILDER_SIGNAL="$signal_name" \
        "$builder" "$base_image" "$driver_dir" "$signal_output"
    [[ "$bounded_status" -eq "$expected_status" ]] || \
        fail "$signal_name returned $bounded_status instead of $expected_status"
    [[ ! -e "$signal_output" ]] || fail "$signal_name published output"
    assert_no_builder_temps "$signal_dir"
    tests_run=$((tests_run + 1))
done

# Every kind of pre-existing target is left untouched.
existing_dir="$test_root/existing targets"
mkdir -p "$existing_dir"
regular_target="$existing_dir/regular.img"
printf 'KEEP REGULAR\n' > "$regular_target"
regular_hash="$(sha256_file "$regular_target")"
expect_failure existing-regular "$builder" "$base_image" "$driver_dir" "$regular_target"
[[ "$(sha256_file "$regular_target")" == "$regular_hash" ]] || fail 'existing regular file changed'

symlink_target="$existing_dir/symlink.img"
ln -s 'missing-target.img' "$symlink_target"
expect_failure existing-symlink "$builder" "$base_image" "$driver_dir" "$symlink_target"
[[ -L "$symlink_target" && "$(readlink "$symlink_target")" == 'missing-target.img' ]] || \
    fail 'existing symlink changed'

fifo_target="$existing_dir/fifo.img"
mkfifo "$fifo_target"
expect_failure existing-fifo "$builder" "$base_image" "$driver_dir" "$fifo_target"
[[ -p "$fifo_target" ]] || fail 'existing FIFO changed'

directory_target="$existing_dir/directory.img"
mkdir -p "$directory_target"
printf 'KEEP DIRECTORY\n' > "$directory_target/marker"
expect_failure existing-directory "$builder" "$base_image" "$driver_dir" "$directory_target"
[[ -f "$directory_target/marker" ]] || fail 'existing directory changed'
assert_no_builder_temps "$existing_dir"

# Simulate another process creating the output immediately before atomic publish.
race_dir="$test_root/concurrent target"
race_wrapper_dir="$race_dir/tools"
mkdir -p "$race_wrapper_dir"
# The single-quoted strings are source code for the generated wrapper.
# shellcheck disable=SC2016
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'if [[ "$RACE_TARGET_KIND" == directory ]]; then' \
    '    mkdir "$2"' \
    'else' \
    '    printf "CONCURRENT CREATOR\\n" > "$2"' \
    'fi' \
    'exec "$REAL_LINK" "$@"' > "$race_wrapper_dir/link"
chmod +x "$race_wrapper_dir/link"
race_output="$race_dir/output.img"
expect_failure concurrent-target env PATH="$race_wrapper_dir:$PATH" REAL_LINK="$real_link" \
    RACE_TARGET_KIND=file \
    "$builder" "$base_image" "$driver_dir" "$race_output"
[[ "$(sed -n '1p' "$race_output")" == 'CONCURRENT CREATOR' ]] || fail 'concurrent target was replaced'

race_directory="$race_dir/directory.img"
expect_failure concurrent-directory env PATH="$race_wrapper_dir:$PATH" REAL_LINK="$real_link" \
    RACE_TARGET_KIND=directory \
    "$builder" "$base_image" "$driver_dir" "$race_directory"
[[ -d "$race_directory" ]] || fail 'concurrent directory was replaced'
if [[ -n "$(find "$race_directory" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail 'builder published a file inside the concurrent directory'
fi
assert_no_builder_temps "$race_dir"
[[ "$(sha256_file "$base_image")" == "$base_hash" ]] || fail 'failure tests changed the source image'
[[ "$(sha256_file "$driver_dir/HIMEMX.EXE")" == "$himemx_hash" ]] || fail 'builder changed HIMEMX.EXE'
[[ "$(sha256_file "$driver_dir/UDVD2.SYS")" == "$udvd2_hash" ]] || fail 'builder changed UDVD2.SYS'
[[ "$(sha256_file "$driver_dir/SHSUCDX.COM")" == "$shsucdx_hash" ]] || fail 'builder changed SHSUCDX.COM'
leftover="$(find "$test_root" \( -name '.*.tmp.*' -o -name '.*.verify.*' \) -print -quit)"
[[ -z "$leftover" ]] || fail "temporary builder path remains after suite: $leftover"

printf 'Boot-floppy builder tests passed (%d cases).\n' "$tests_run"

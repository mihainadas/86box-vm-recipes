#!/bin/zsh

set -eu

vm_dir="${0:A:h}"
emulator_path="${EIGHTYSIXBOX_EXECUTABLE:-}"
rom_path="${EIGHTYSIXBOX_ROM_PATH:-}"
asset_path="${EIGHTYSIXBOX_ASSET_PATH:-}"
machine_config="$vm_dir/86box.cfg"
global_config="$vm_dir/86box_global.cfg"
disk_path="$vm_dir/disks/windows95.hdd"
disk_expected_size=2111864832

if [[ -z "$emulator_path" && -x "/Applications/86Box.app/Contents/MacOS/86Box" ]]; then
    emulator_path="/Applications/86Box.app/Contents/MacOS/86Box"
fi

if [[ -z "$emulator_path" || ! -f "$emulator_path" || ! -x "$emulator_path" ]]; then
    print -u2 "Set EIGHTYSIXBOX_EXECUTABLE to the 86Box executable."
    exit 1
fi

if [[ ! -f "$machine_config" || ! -r "$machine_config" ]]; then
    print -u2 "Create 86box.cfg as described in README.md before launching."
    exit 1
fi

if [[ ! -f "$global_config" || ! -r "$global_config" ]]; then
    print -u2 "Create 86box_global.cfg as described in README.md before launching."
    exit 1
fi

if [[ -z "$rom_path" || ! -d "$rom_path/machines" ]]; then
    print -u2 "Set EIGHTYSIXBOX_ROM_PATH to the official 86Box ROM checkout."
    exit 1
fi

if [[ -z "$asset_path" || ! -f "$asset_path/sounds/hdd/hdd_audio_profiles.cfg" ]]; then
    print -u2 "Set EIGHTYSIXBOX_ASSET_PATH to the official 86Box asset checkout."
    exit 1
fi

if [[ ! -f "$disk_path" ]]; then
    print -u2 "Create disks/windows95.hdd as described in README.md before launching."
    exit 1
fi

disk_size="$(/usr/bin/stat -L -f '%z' "$disk_path")"
if [[ "$disk_size" != "$disk_expected_size" ]]; then
    print -u2 "disks/windows95.hdd must contain exactly $disk_expected_size bytes as declared in recipe.toml."
    exit 1
fi

exec "$emulator_path" \
    --vmpath "$vm_dir" \
    --global "$global_config" \
    --rompath "$rom_path" \
    --assetpath "$asset_path" \
    --vmname "The 1995 Dream 486"

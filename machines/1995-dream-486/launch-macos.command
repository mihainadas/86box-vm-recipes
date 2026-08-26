#!/bin/zsh

set -eu

vm_dir="${0:A:h}"
emulator_path="${EIGHTYSIXBOX_EXECUTABLE:-}"
rom_path="${EIGHTYSIXBOX_ROM_PATH:-}"
asset_path="${EIGHTYSIXBOX_ASSET_PATH:-}"

if [[ -z "$emulator_path" && -x "/Applications/86Box.app/Contents/MacOS/86Box" ]]; then
    emulator_path="/Applications/86Box.app/Contents/MacOS/86Box"
fi

if [[ -z "$emulator_path" || ! -x "$emulator_path" ]]; then
    print -u2 "Set EIGHTYSIXBOX_EXECUTABLE to the 86Box executable."
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

if [[ ! -f "$vm_dir/disks/windows95.hdd" ]]; then
    print -u2 "Create disks/windows95.hdd as described in README.md before launching."
    exit 1
fi

exec "$emulator_path" \
    --vmpath "$vm_dir" \
    --global "$vm_dir/86box_global.cfg" \
    --rompath "$rom_path" \
    --assetpath "$asset_path" \
    --vmname "The 1995 Dream 486"

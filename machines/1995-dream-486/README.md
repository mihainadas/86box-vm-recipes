# The 1995 Dream 486

This is a deliberately luxurious late-486 machine built around the memory of a 486DX2/66 running Windows 95. It is period-conscious rather than a claim about one exact original PC.

## Hardware

- FIC 486-VIP-IO2 Socket 3 motherboard with dual IDE, PCI, ISA, APM, and a gameport
- Intel 486DX2 at 66 MHz with its internal floating-point unit
- 32 MB RAM
- S3 Vision864 PCI graphics with 4 MB video memory
- Sound Blaster AWE32 PnP with OPL3 compatibility and 28 MB sample RAM
- AMD PCnet-PCI II Ethernet using 86Box's SLiRP user-mode network
- Approximately 2 GB IDE disk with a 1995 performance profile and IBM H3171 drive sounds
- 4x ATAPI CD-ROM
- 3.5-inch 1.44 MB and 5.25-inch 1.2 MB floppy drives with recorded mechanism sounds
- Four-button PS/2 wheel mouse, four-button gameport joystick, and ESC/P 2 dot-matrix printer

The less obvious settings are intentional: `buttons = 4` exposes a wheel-capable PS/2 mouse, `language = 3` selects ESC/P 2 rather than the incompatible legacy printer mode, and the 28 MB AWE32 RAM allocation creates an indulgent period sound card.

## Requirements

- A current 86Box build
- The official [86Box ROM repository](https://github.com/86Box/roms)
- The official [86Box assets](https://github.com/86Box/assets) for recorded drive sounds
- Your own legitimate Windows 95 installation media and product key
- A bootable startup floppy with an ATAPI CD-ROM driver; see the [tested FreeDOS recipe](boot-floppy/README.md)
- A Windows 95-compatible wheel-mouse driver if you want scrolling; Microsoft IntelliPoint 2.2 was tested, but it is not distributed here

## Directory setup

The tracked recipe expects these local-only paths:

```text
1995-dream-486/
├── 86box.cfg
├── 86box_global.cfg
├── recipe.toml
├── disks/
│   └── windows95.hdd
├── media/
│   ├── win95-startup-udvd2.img
│   └── your-windows-95.iso
├── nvr/
├── printer/
└── screenshots/
```

The repository ignores every runtime or media file in those directories.

## Create the hard disk

Create `disks/windows95.hdd` from 86Box's hard-disk manager and attach it as IDE primary master. Use 1,023 cylinders, 64 heads, and 63 sectors per track. This presents 2,111,864,832 bytes, just under 2 GiB, and remains compatible with the original Windows 95 FAT16 limit.

If your 86Box build offers a speed profile, select the 1995-era 5,200 RPM profile. The recipe's `IBM_H3171_3600RPM` value controls only the recorded sound profile.

## Install Windows 95

1. Put your startup image and Windows 95 CD image in `media/`.
2. Open this VM in 86Box or configure the environment variables described under [Launching on macOS](#launching-on-macos).
3. Mount the startup image in the 3.5-inch drive and the Windows 95 image in the CD-ROM drive.
4. Boot from the floppy. The tested startup image exposes the CD-ROM as `D:`.
5. Run `FDISK`, create the primary DOS partition, and reboot from the floppy.
6. Run `FORMAT C: /S`, then start Setup from `D:\WIN95\SETUP`.
7. Eject the floppy before Setup's first reboot. Keep the CD mounted until Setup finishes.

Windows 95 should detect the PCI chipset, S3 display adapter, AWE32, and PCnet adapter. Driver availability varies by Windows 95 edition, so a device may temporarily use a generic driver.

## Wheel mouse

The host can deliver scroll events only after both layers are ready: 86Box must advertise the four-button PS/2 wheel model, and Windows 95 must load a wheel-aware guest driver. This recipe already sets `buttons = 4`. IntelliPoint 2.2 was tested successfully and scrolling survived subsequent reboots.

## Printer

Install the Windows 95 `Epson LQ-2500` driver on `LPT1:`. 86Box renders completed pages as PNG files under `printer/`; nothing is sent to a physical printer.

Use an 86Box revision containing the ESC/P 2 raster-graphics implementation merged in [86Box PR #7774](https://github.com/86Box/86Box/pull/7774). Older builds terminate when the Windows test page sends `ESC .` raster graphics.

## Soft reboot reliability

Use an 86Box revision containing the dynarec page-remapping fix merged in [86Box PR #7787](https://github.com/86Box/86Box/pull/7787). Older new-dynarec builds can abort after a Windows 95 guest-initiated soft reboot with `page_remove_from_evict_list: not in evict list!`.

## Useful controls

- Release captured input with `Ctrl+Alt+G`, displayed as `⌘⌥G` on macOS in current 86Box builds.
- Insert or eject media from the status-bar device icons.
- Windows 95 may stop at “It is now safe to turn off your computer.” This 486 has no ACPI, so close 86Box after reaching that screen.
- Shut Windows down before changing storage or core machine settings.

## Launching on macOS

`launch-macos.command` intentionally contains no personal paths. Set these variables before launching:

```zsh
export EIGHTYSIXBOX_EXECUTABLE="/Applications/86Box.app/Contents/MacOS/86Box"
export EIGHTYSIXBOX_ROM_PATH="/path/to/86Box-roms"
export EIGHTYSIXBOX_ASSET_PATH="/path/to/86Box-assets"
./launch-macos.command
```

The script can discover 86Box in `/Applications`, but the ROM and asset paths are always explicit so it never guesses which copy to use.

## Backups

Once installation and drivers are stable, stop the VM and copy `windows95.hdd` outside the repository. Do not copy a live disk and do not commit the backup. A clean post-install image is dramatically faster to restore than repeating Windows 95 hardware detection.

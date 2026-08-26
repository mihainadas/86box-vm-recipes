# Tested FreeDOS startup floppy

The known-good startup disk is based entirely on FreeDOS tooling and contains no Windows files. We still do not commit the binary image because each bundled component retains its own license and publishing a derived image correctly would require preserving the corresponding source and notices. This repository instead preserves the exact configuration, versions, checksums, and a reproducible customization script.

Start with the official [FreeDOS 1.4 Floppy Edition](https://www.freedos.org/download/). Use a writable 1.44 MB boot image whose command interpreter is at `A:\FREEDOS\BIN\COMMAND.COM`. Obtain `HIMEMX.EXE`, `UDVD2.SYS`, and `SHSUCDX.COM` from the FreeDOS distribution or their official package sources. The [FreeDOS UDVD2 archive](https://gitlab.com/FreeDOS/drivers/udvd2) and [FreeDOS HimemX source](https://github.com/FDOS/himemX) provide upstream provenance.

## Why this combination matters

Our first startup image loaded `ATAPICDD.SYS`. It booted, but did not expose the emulated ATAPI Windows 95 CD reliably. The working image makes two essential changes:

1. Load `HIMEMX.EXE` before the optical driver.
2. Replace `ATAPICDD.SYS` with `UDVD2.SYS /D:WIN95CD`.

`SHSUCDX.COM /D:WIN95CD,D` then assigns the CD-ROM to `D:`. The device name must match in both files.

## Build from your own FreeDOS files

Install `mtools`, put the three driver files together in a private directory, and run:

```sh
./make-floppy.sh /path/to/freedos-base.img /path/to/private-drivers win95-startup-udvd2.img
```

The output remains untracked because `*.img` is ignored. The script checks that the expected FreeCOM path exists before changing the copy.

## Known-good reference

The exact image used while installing this recipe had these properties:

- FAT12, 1,474,560 bytes, volume label `FD14-BOOT`
- Whole-image SHA-256: `fff431a4c8b1e1ab2ce27de5ee73141b788bcfe254c9e08dc5ea62a692c6e1ca`
- `KERNEL.SYS`: 46,485 bytes, SHA-256 `f34a7483c575fcf2709d9a7d0bc3db81c6211c279530f9e1bf78576b9233924d`
- `HIMEMX.EXE`: 6,060 bytes, SHA-256 `d88eba05057d6b7c123645acbe54798e60993ebedbfcae638af9e58044e78842`
- `UDVD2.SYS`: 3,987 bytes, SHA-256 `748a22d72e4245eb176ba6f6dd264a2ba571634d320fff19297791d5d1a57b5e`
- `SHSUCDX.COM`: 8,088 bytes, SHA-256 `9db5ccd8cb731a731b953a5ca4b04537cbc2d772d392de4a7ea173080e6d6488`

Different FreeDOS package revisions may produce a different whole-image checksum while behaving correctly. The functional test matters more: the boot log should show UDVD2 loading, SHSUCDX should install, and `DIR D:` should list the Windows 95 CD.

## Keep your working image safe

Keep at least two private copies of the working image outside this Git repository. The public recipe never reads, rewrites, moves, or deletes the original VM's media.

# 86Box VM Recipes

Reproducible, documented machine configurations for [86Box](https://86box.net/). This is an unofficial community project and is not affiliated with the 86Box project.

The repository contains configuration, setup instructions, and small helper scripts. It deliberately does not contain operating systems, product keys, hard-disk images, firmware, ROMs, commercial drivers, or installation media. Bring legally obtained media and use the official [86Box ROM repository](https://github.com/86Box/roms) and [asset repository](https://github.com/86Box/assets) separately.

## Available machines

- [The 1995 Dream 486](machines/1995-dream-486/README.md) — a luxurious late-486 Windows 95 machine built around a 486DX2/66, S3 Vision864, Sound Blaster AWE32, Ethernet, wheel mouse, and ESC/P 2 dot-matrix printer.

## What a recipe provides

Each machine directory contains a sanitized `86box.cfg`, hardware notes, installation instructions, required media names, known quirks, and recovery guidance. Runtime files remain local and are excluded by `.gitignore`.

## Safety and licensing

Never commit an installed disk, product key, ROM dump, NVR image, proprietary driver, or operating-system image. Checksums may be documented so users can identify their own media, but a checksum is not a download or a license to redistribute the file.

The original text, configuration recipes, and helper scripts in this repository are available under the [MIT License](LICENSE). Third-party software retains its own license.

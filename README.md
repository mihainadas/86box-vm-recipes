# 86Box VM Recipes

[![CI](https://github.com/mihainadas/86box-vm-recipes/actions/workflows/ci.yml/badge.svg)](https://github.com/mihainadas/86box-vm-recipes/actions/workflows/ci.yml)

Reproducible, documented machine configurations for [86Box](https://86box.net/). This is an unofficial community project and is not affiliated with the 86Box project.

The repository contains configuration, setup instructions, small helper scripts, and a [license-aware software catalog and media builder](software/README.md). It deliberately does not commit operating systems, product keys, hard-disk images, firmware, ROMs, commercial drivers, or installation media. Exact reviewed redistributable artifacts may be fetched from official sources into ignored local storage; everything else must be supplied privately. Use the official [86Box ROM repository](https://github.com/86Box/roms) and [asset repository](https://github.com/86Box/assets) separately.

## Available machines

- [The 1995 Dream 486](machines/1995-dream-486/README.md) — a luxurious late-486 Windows 95 machine built around a 486DX2/66, S3 Vision864, Sound Blaster AWE32, Ethernet, wheel mouse, and ESC/P 2 dot-matrix printer.

## What a recipe provides

Each machine directory contains a versioned `recipe.toml` manifest, a sanitized `86box.cfg`, hardware notes, installation instructions, required media names, known quirks, and recovery guidance. Runtime files remain local and are excluded by `.gitignore`.

The optional [private acceptance harness](docs/private-acceptance.md) can validate your locally supplied media and, only with an explicit launch flag, exercise a fully disposable copy without attaching the original writable images. The software-media builder can separately create a verified FreeDOS kit or a private companion applications CD without altering the VM’s boot floppy.

## Check changes locally

Run `scripts/check.sh` from anywhere in the checkout before contributing. Git, Bash, and Python 3.11 or newer are required. Install [ShellCheck](https://www.shellcheck.net/) for shell linting, [mtools](https://www.gnu.org/software/mtools/) for the synthetic boot-floppy tests, and [xorriso](https://www.gnu.org/software/xorriso/) for the synthetic software-media test; when an optional tool is unavailable, the command explains what was skipped and how to enable it. On macOS it also exercises the launcher against disposable mock files without requiring 86Box, ROMs, operating-system media, or a real virtual disk.

Use `scripts/check.sh --require-optional` for a fully provisioned run in which missing optional tools are errors. The same entry point and its named suites drive CI, so the project-owned check list does not drift between local and hosted runs.

## Safety and licensing

Never commit an installed disk, product key, ROM dump, NVR image, proprietary driver, or generated installation image. The software catalog may pin a reviewed official redistributable artifact for local fetching, but a checksum or recommendation is not by itself a license to redistribute a file.

The original text, configuration recipes, and helper scripts in this repository are available under the [MIT License](LICENSE). Third-party software retains its own license.

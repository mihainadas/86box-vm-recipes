# Software media kits

This directory is a license-aware catalog and media builder for the VM recipes. It supports two deliberately separate lanes: a redistributable kit made only from explicitly reviewed, checksum-pinned upstream artifacts, and a personal companion ISO made from files you supply locally. Neither lane changes or replaces the carefully tuned boot floppy.

## Curated starting set

| Pick | Fit | Distribution | What this repository does |
| --- | --- | --- | --- |
| [FreeDOS 1.4 LegacyCD and BonusCD](https://www.freedos.org/download/) | Modern-retro OS and DOS application collection | Redistributable aggregation with per-package licenses and sources on the official media | Fetches the two official ZIP archives over HTTPS, verifies their pinned byte size and upstream-published SHA-256, safely extracts the single ISO from each, and packages the untouched ISOs with an audit manifest |
| [Winamp 2.x](https://winamp.com/) | Quintessential late-1990s Windows audio player | User-supplied proprietary software | Records the recommendation but never supplies or fetches an installer |
| [DOOM game data](https://github.com/id-Software/DOOM) | Period-authentic DOS game | User-supplied data; the published engine source does not grant the commercial data | Records the legal boundary and accepts only a locally supplied, checksum-pinned file |

The structured catalog also includes SimCity 2000, Warcraft II, Duke Nukem 3D, Quake, Myst, Paint Shop Pro, Office 95, Netscape Navigator, PKZIP, and Norton Commander. Run `python3 -B scripts/software-media.py list` to see the complete catalog and policy labels. Their presence is a compatibility suggestion, not a download link or redistribution determination. Quake is ambitious on a DX2/66; DOOM, SimCity 2000, and Warcraft II are much more natural fits.

Compatibility labels mean:

- `period-authentic`: contemporary with the target machine’s historical era.
- `period-compatible`: later software known or expected to support the guest platform.
- `modern-retro`: maintained later for old hardware and useful today, but not historically contemporary.

Distribution labels mean:

- `redistributable`: reviewed for this exact pinned artifact; third-party terms still apply.
- `reference-only`: documented without an automated download or inclusion path.
- `user-supplied`: accepted only from a private local manifest and never fetched.

## Build the FreeDOS kit

Python 3.11 or newer is required. The fetch command is intentionally disabled in CI, and neither command overwrites an existing output. Allow roughly 2.5 GB of free space while the two source archives, extracted ISOs, staging tree, and final kit coexist.

```sh
install -d -m 700 software/cache software/dist
python3 -B scripts/software-media.py fetch --bundle freedos-14-kit --cache software/cache
python3 -B scripts/software-media.py build --bundle freedos-14-kit --cache software/cache --output software/dist/freedos-14-kit.zip
```

The result is a deterministic ZIP for the same inputs and repository revision. It contains the official bootable FreeDOS LegacyCD ISO, the official BonusCD ISO, `CATALOG/MEDIA.JSON`, and a short readme. The source ZIPs and extracted ISOs are never committed. The official archives are authenticated by the SHA-256 values published in FreeDOS’s [verification file](https://www.freedos.org/download/verify.txt); redistribution and source evidence is recorded from the official [FreeDOS 1.4 build report](https://www.ibiblio.org/pub/micro/pc-stuff/freedos/files/distributions/1.4/report.html). Package license notices and matching source sets remain inside the unchanged upstream ISOs. This project does not rewrite a bootable OS image or mix it into an applications disc.

## Build a personal companion CD

Install [xorriso](https://www.gnu.org/software/xorriso/) first. Generate a private manifest from the curated selections, delete the sections you do not want, and replace every remaining placeholder with an absolute path to a file or complete original-media directory you may legally use. Use the hash command below with the entry’s generated `source_type`, copy its lowercase digest into the manifest, and change `license_acknowledged` to `true` only after checking that source’s terms. The generated manifest is mode 0600 and is never overwritten.

```sh
install -d -m 700 software/cache software/dist
python3 -B scripts/software-media.py personal-template --bundle dream-486-personal --output software/private-software.toml
python3 -B scripts/software-media.py hash --source-type directory --path /absolute/path/to/your/original-media-directory
python3 -B scripts/software-media.py build --bundle dream-486-personal --cache software/cache --output software/dist/dream-486-personal.iso --private-manifest software/private-software.toml
```

The output is a nonbootable ISO intended to be mounted as a second CD after the guest OS is installed. Single-file picks copy one installer or data file; directory picks recursively copy the complete setup tree, including CAB and game-data siblings. Directory entries must map uniquely to uppercase ISO 9660 8.3 names; symlinks, special files, case collisions, and longer names fail closed instead of silently producing a broken disc. The builder verifies every file and whole-tree digest while copying, enforces both payload and final-image size, strips host paths from its generated metadata, refuses private operations in CI, creates intermediates inside a mode-0700 workspace, and publishes by a descriptor-anchored no-overwrite atomic link. It suppresses private path details in errors. Keep the manifest and ISO private; both are ignored by Git. ISO output is reproducible for identical inputs with the same xorriso version; that version and the scope are recorded in `CATALOG/MEDIA.JSON` because GNU does not promise identical output across xorriso releases.

## Catalog policy

Catalog and bundle files use a closed TOML schema validated by `scripts/software-media.py validate`. A redistributable entry must pin an official HTTPS URL, byte count, lowercase SHA-256, a supported SPDX expression or `NOASSERTION` for a documented aggregation, license basis and evidence URL, notice location, source-compliance explanation, compatibility tier, and security note. Entries labeled `reference-only` or `user-supplied` are forbidden from declaring download artifacts. Adding a URL is not enough: review the exact artifact’s license and source obligations first.

Do not treat “open source engine” as “open game.” For example, id Software’s DOOM source release explicitly says that real game data is still required. Likewise, 7-Zip 4.29 was not selected for an all-open-source direct-application set because its source archive mixes in separately restricted unRAR code; that is a non-OSI licensing distinction, not a claim that its terms prohibit redistribution. When licensing is mixed or unclear, use `reference-only` or `user-supplied`.

Run `scripts/check.sh software-media` to validate metadata and exercise a deterministic ISO build using synthetic bytes. No real operating system, application, game, product key, or private path is used by the tests.

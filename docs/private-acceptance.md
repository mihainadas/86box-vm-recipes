# Private acceptance harness

The private acceptance harness checks that your own 86Box archive, ROM and asset checkouts, installed hard disk, startup floppy, and installation ISO fit this public recipe. Its default mode is a read-only preflight: it reads and hashes the named files, validates the public recipe, constructs a network-free configuration in memory, and optionally creates a sanitized report. It does not create a temporary VM or start 86Box unless you add `--launch`.

## Trust model

Production launch accepts one exact artifact: `86Box-macOS-x86_64+arm64-b9001.zip` from the official `86Box/86Box` v6.0 release, build 9001, associated with source commit `4fef696a4eead1d55a28d6ac0e5bd2864e5454da`. The archive must contain exactly 124,110,592 bytes and match SHA-256 `fc66fc97225012af20145ae04193911bbf689fc75f89590774a904483140a5a9`. The harness also verifies the extracted universal executable, expected x86_64 and arm64 slices, archive bounds, and the release's exact internal framework-link layout. There is no command-line override for these values.

The digest of that exact official GitHub release asset is the trust anchor. The release's embedded Apple signatures are known to be invalid for both architectures, and the harness requires and reports that reviewed `known_invalid` result rather than claiming Apple publisher verification. The release association also does not cryptographically prove that the binary was built from the tagged source. A pinned digest prevents substitution after review; it does not make an upstream binary harmless.

The temporary 86Box configuration contains no `[Network]` section. At the allowlisted source revision, absence of the section means no emulated network card is created. This is not a host-process network sandbox: 86Box itself runs with your user account's ordinary permissions and could use host networking. For stronger isolation, run on an offline Mac under a dedicated account that cannot read unrelated personal data.

ROM and asset roots are passed directly as read-only dependencies because copying those trees would be excessive. The harness requires nonsymlink roots and expected markers but cannot enforce a read-only filesystem or prevent a trusted process from attempting writes. Private HDD, floppy, ISO, configuration, NVR, printer output, logs, and emulator extraction are confined to the disposable workspace.

## Private manifest

Copy `private-acceptance.example.toml` to a location outside the repository, replace every placeholder with an absolute path, and restrict it before use:

```zsh
chmod 600 /absolute/path/private-acceptance.local.toml
```

The manifest must be a nonsymlink regular file owned by the current user with mode `0600`. Every path is explicit and absolute. The six inputs must be nonsymlink files or directories as appropriate. Do not add a product key or any other field.

## Read-only preflight

```zsh
python3 -B scripts/private-acceptance.py \
  --private-manifest /absolute/path/private-acceptance.local.toml
```

Add `--report /absolute/path/acceptance-report.json` to atomically create a new sanitized report. Existing files are never overwritten. Reports contain only public recipe/config hashes, the allowlisted release identifiers, coarse platform and UTC time, fixed preflight/launch states, the no-guest-NIC assertion, and fixed guest checks. They never contain private paths, media or executable hashes, filenames, product keys, volume labels, logs, screenshots, printer output, or free-form text.

## Explicit disposable launch

First shut down every 86Box process that might use the installed HDD. Then run:

```zsh
python3 -B scripts/private-acceptance.py \
  --private-manifest /absolute/path/private-acceptance.local.toml \
  --launch \
  --source-vm-stopped
```

Launch is refused whenever a recognized CI marker is set. The harness creates a mode-`0700` sentinel workspace, copies only allowlisted public files, removes the entire `[Network]` section, reparses the exact disposable configuration, and stages every private medium as a distinct file. On macOS it first requests an APFS copy-on-write clone. If cloning is unavailable, the harness stops before launch; `--allow-full-copy` explicitly permits a capacity-checked full copy. Originals are hashed before staging and again after the emulator process and its children have stopped.

The official archive is safely streamed into the workspace rather than passed to `unzip` or recursively copied. Emulator output stays in private mode-`0600` workspace logs and is never included in a report. HUP, INT, and TERM stop the emulator process group, wait, escalate to KILL if required, prove the process group is gone, recheck sources, and then perform guarded sentinel cleanup. If the process group cannot be proven gone, the workspace is retained. Cleanup empties the verified workspace through an already-open directory descriptor but intentionally leaves the empty mode-`0700` root directory behind: portable macOS/Python APIs cannot remove that final pathname without reopening a same-user replacement race. Empty roots use the `86box-private-acceptance-` prefix in the system temporary directory and may be removed normally after the harness exits. `--keep-temp` is an explicit diagnostic escape hatch; it prints the private workspace location and warns not to publish it.

An emulator exit status of zero means only that the process exited cleanly. It does not prove BIOS POST or any guest behavior.

## Manual acceptance checklist

Complete these checks yourself while the disposable VM is running. Do not mark them passed from a screenshot, window title, elapsed time, or process exit alone.

- BIOS POST completes without an unexpected configuration error.
- The installation CD is available as drive `D:` from the startup floppy environment.
- Windows 95 reaches the desktop with the intended hardware configuration.
- Wheel scrolling works with the documented guest driver.
- The Epson LQ-2500 test page renders in the disposable printer directory.
- The first guest-initiated soft reboot returns to Windows.
- The second guest-initiated soft reboot returns to Windows.
- The third guest-initiated soft reboot returns to Windows.

The JSON report intentionally leaves all of these checks as `not_observed`. A future automated guest check would need an unambiguous guest-originated signal; this harness does not infer one.

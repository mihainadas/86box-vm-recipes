# Contributing

Contributions should describe reproducible machine recipes, not distribute complete virtual machines.

Every immediate `machines/*` directory must contain a `recipe.toml`. The manifest records only the stable recipe identity, tracked configuration and launcher paths, hard-disk geometry, and required ROM and asset paths. Keep it synchronized with `86box.cfg`; `scripts/validate-recipes.py` uses Python 3.11 or newer to reject unknown fields, unsafe paths, untracked references, unignored runtime disks, and storage settings that drift from the machine configuration.

The manifest also declares the ROM and asset layout expected from external checkouts. `tests/macos-launcher-contract.py` verifies launcher argument and runtime-prerequisite parity rather than making the manifest validator parse shell source text.

Stage recipe and configuration changes before validating so the checks describe exactly what the commit will contain. Before committing, run `scripts/check.sh` from anywhere in the checkout. It runs the public-tree audit and regression tests, manifest validation and unit tests, shell syntax checks, the boot-floppy builder tests when mtools is installed, and the launcher contract tests on macOS. Optional checks that cannot run include an installation hint; use `scripts/check.sh --require-optional` when you want missing optional tools to fail the run. The script also verifies that its checks do not change the Git working tree or index.

The named suites `repository-safety`, `boot-floppy`, and `macos-launcher` are useful while iterating on one area; for example, run `scripts/check.sh macos-launcher` after changing a machine manifest or launcher. An explicitly selected suite treats its core prerequisites as required. Keep configuration host-neutral, document unusual values, and test the recipe from a clean directory when practical.

Run `python3 -B -m unittest tests.test_private_acceptance -v` after changing the private acceptance harness, its report schema, trust record, or safety documentation. Tests use synthetic archives and media only; never introduce a real emulator archive or private medium as a fixture.

Do not include operating systems, product keys, installed disk images, ROMs, NVR state, firmware dumps, proprietary drivers, or media copied from another project. Link to official sources where redistribution is permitted and tell users to supply everything else themselves.

Write Markdown prose as one continuous source line per paragraph. Use line breaks only for real paragraph boundaries, list items, code blocks, tables, or other structural Markdown.

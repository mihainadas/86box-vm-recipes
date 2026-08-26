# Contributing

Contributions should describe reproducible machine recipes, not distribute complete virtual machines.

Every immediate `machines/*` directory must contain a `recipe.toml`. The manifest records only the stable recipe identity, tracked configuration and launcher paths, hard-disk geometry, and required ROM and asset paths. Keep it synchronized with `86box.cfg`; `scripts/validate-recipes.py` uses Python 3.11 or newer to reject unknown fields, unsafe paths, untracked references, unignored runtime disks, and storage settings that drift from the machine configuration.

The manifest also declares the ROM and asset layout expected from external checkouts. Launcher argument parity is intentionally verified by the contract tests tracked in issue #5 rather than by parsing launcher source text in the manifest validator.

Stage recipe and configuration changes before validating so the checks describe exactly what the commit will contain. Before committing, run `scripts/audit-public-tree.sh` and `python3 scripts/validate-recipes.py`. Keep configuration host-neutral, document unusual values, and test the recipe from a clean directory when practical.

Do not include operating systems, product keys, installed disk images, ROMs, NVR state, firmware dumps, proprietary drivers, or media copied from another project. Link to official sources where redistribution is permitted and tell users to supply everything else themselves.

Write Markdown prose as one continuous source line per paragraph. Use line breaks only for real paragraph boundaries, list items, code blocks, tables, or other structural Markdown.

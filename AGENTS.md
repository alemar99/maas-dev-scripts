# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

## Architecture

- `maas-env.py`: main CLI tool for creating/destroying MAAS LXD environments and driving the edit→sync→overlay inner loop. Flat argparse for the original flags (`--destroy`, `--sync`, `--overlay`, `--unoverlay`); new subcommands (`status`, `logs`, `exec`, `shell`) use positional `name` dispatch before the flat parser.
- `overlay-mount.py`: in-container tool to apply/remove OverlayFS mounts over the MAAS snap/deb paths. Invoked via `lxc exec`.
- Shell scripts (`*.sh`): run inside containers via `lxc exec`. All have `set -e`.
- `lxd-maas-profile.yaml`: LXD profile applied to all containers; bind-mounts the repo at `/scripts`.
- `overlay-config-37.yaml`, `overlay-config-master.yaml`: channel-specific overlay configurations.

## Testing

- Syntax check Python: `python3 -m py_compile maas-env.py`
- Syntax check shell: `bash -n <script>.sh`
- Unit tests: `python3 -m pytest tests/` (pytest may not be installed; install with `pip install pytest` if needed)
- Quick smoke test: `./maas-env.py --help` and each subcommand's `--help`

## Sharp edges

- `postgres-setup.sh` is idempotent (uses conditional SQL), safe to run twice.
- `base_candid_rbac_setup.sh` dynamically detects the installed PostgreSQL version via `ls /etc/postgresql`; do not add hardcoded version symlinks.
- `--overlay-config` is auto-selected from `--maas-channel` (3.7/* → overlay-config-37.yaml; latest/master/main/* → overlay-config-master.yaml). Pass explicitly to override.
- `argcomplete` integration is optional (graceful import fallback); enable with `pip install argcomplete && eval "$(register-python-argcomplete maas-env.py)"`.

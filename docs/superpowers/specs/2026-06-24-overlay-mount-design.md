# Design: `--overlay` / `--unoverlay` actions for `maas-env.py`

- **Date:** 2026-06-24
- **Status:** Approved (ready for implementation plan)
- **Component:** `maas-env.py`, `lxd-maas-profile.yaml`, vendored `overlay-mount.py`

## Overview

`maas-env.py` creates/destroys MAAS test environments in LXD. Each container gets
a clone of the MAAS source at `/work` (via `maas-install.sh`) and MAAS installed
from a snap. The existing `--sync` action pushes a host MAAS checkout into every
container's `/work`.

This adds the next step in the inner loop: overlay the synced `/work/src/...`
directories onto the **installed MAAS snap paths** inside each container using the
`overlay-mount` tool, so edited code runs without rebuilding/reinstalling the snap.
A matching teardown action removes the overlays. Both actions restart MAAS so the
change takes effect.

Full dev loop this completes:

1. Edit MAAS on the host (e.g. `~/work/maas`).
2. `--sync ~/work/maas` → push edits into each container's `/work` (existing).
3. `--overlay --overlay-config overlay-config-37.yaml` → overlay `/work/src/*`
   onto the snap paths and restart MAAS.
4. Iterate: re-run `--sync` then `--overlay` (overlay does unmount-then-mount, so
   re-running is clean).
5. `--unoverlay --overlay-config ...` → remove overlays and restart MAAS to stock.

## Goals

- Standalone `--overlay` / `--unoverlay` actions that drive the `overlay-mount`
  tool inside every container.
- Reuse the existing container-list, dry-run, and `lxc exec` machinery.
- Restart MAAS automatically so overlays take effect (and so teardown reverts).
- No per-container provisioning beyond what the LXD profile guarantees.

## Non-goals

- `deb` package mode. The containers install MAAS via snap; only `--snap` mode is
  wired up. (The vendored tool retains `--deb`, but the integration does not use it.)
- Building artifacts (UI / Go binaries). Build outputs are produced on the host and
  carried in by `--sync` like any other file.
- Running `--sync` automatically as part of `--overlay`. They stay composable:
  the user runs `--sync` then `--overlay`.
- Per-node overlay targeting (always all containers, like `--sync`).

## Background: the `overlay-mount` tool

Vendored verbatim from `github.com/alemar99/overlay-mount-tool` (same author as
this repo) as `overlay-mount.py` at the repo root, so it is visible at
`/scripts/overlay-mount.py` inside every container.

CLI:

```
overlay-mount.py {sync|unsync} --config PATH (--snap|--deb) [--dry-run]
```

- `--config` defaults to `config.yaml`, so it **must** be passed explicitly.
- `--snap`/`--deb` is a **required** mutually-exclusive group.
- `sync` = unmount-then-mount (idempotent; safe to re-run).
- `unsync` = unmount only.
- Imports `yaml` (PyYAML required in the container).
- `validate_config` runs `snap list <package>`; the snap must be installed.

For each `dirs:` mapping `source: dest` it runs:

```
sudo mount -t overlay overlay \
  -o lowerdir='<dest>',upperdir='<source>',workdir='.overlayfs_workdir/snap/<source>' \
  '<dest>'
```

- `lowerdir` = installed snap path (read-only squashfs), `upperdir` = `/work/src/...`
  (your code), mountpoint = the installed snap path. The snap `current` symlink is
  resolved to its real revision dir first.
- For `files:` mappings it uses `sudo mount -o bind,ro '<source>' '<dest>'`.
- **`workdir` is relative to the process CWD** and created with `mkdir -p`.
  overlayfs requires `upperdir` and `workdir` to be on the **same filesystem**.

## CLI interface

New action flags added to the existing mutually-exclusive action group
(alongside `--destroy` and `--sync`):

- `--overlay` — mount overlays in each running container, then restart MAAS.
- `--unoverlay` — unmount overlays in each running container, then restart MAAS.
- `--overlay-config PATH` — **required** when `--overlay`/`--unoverlay` is used.
  Path to an overlay config (e.g. `overlay-config-37.yaml`).

Reused existing arguments:

- `--name` (required) and `--mode` (`single`|`multi`) — determine the container
  list, identically to create/destroy/sync.
- `--dry-run` — print the commands that would run, execute nothing.

Constraints:

- `--overlay`, `--unoverlay`, `--sync`, and `--destroy` are mutually exclusive.
- `--snap` mode only.

Examples:

```
./maas-env.py --name mytest --mode multi --overlay   --overlay-config overlay-config-37.yaml
./maas-env.py --name mytest --mode multi --unoverlay --overlay-config overlay-config-37.yaml
./maas-env.py --name mytest --overlay --overlay-config overlay-config-master.yaml --dry-run
```

## Config path resolution

The config must be readable inside the container, where only the repo (bind-mounted
at `/scripts`) is visible. Therefore:

1. Resolve `--overlay-config` to an absolute host path; error if it does not exist
   or is not a file — before touching any container.
2. Require it to live **inside the repo directory** (the bind-mount source =
   `Path(__file__).resolve().parent`). If it is outside, hard error with a clear
   message (it would not be visible at `/scripts` in the container).
3. Compute the container-side path as `/scripts/<path-relative-to-repo-root>`
   (for the two existing configs this is simply `/scripts/overlay-config-*.yaml`).

This mapping is a small pure helper (`container_config_path`) so it is unit-testable.

## Behavior / flow

1. **Validate config** (above): exists, is a file, lives in the repo; map to the
   `/scripts/...` container path.
2. **Compute container list.** Same as create/destroy/sync:
   - `single` → `[name]`
   - `multi`  → `[name-1, name-2, name-3]`
3. **For each container:**
   1. Check it is running. If missing or not running, **warn and skip** it
      (counts as a failure for exit-code purposes), reusing `is_container_running`.
   2. Run the overlay tool (see Invocation).
   3. Restart MAAS (`sudo snap restart maas`).
4. **Summary.** Report per-container success/skip/failure; exit non-zero if any
   container failed. Mirrors `sync()`.

## Invocation (via existing `lxc_exec`, runs as root)

`lxc_exec` runs `lxc exec <c> -- sh -c "<cmd>"` as **root**, which is what `mount`
needs. The command per container, for `--overlay`:

```
cd /work && python3 /scripts/overlay-mount.py sync   --config /scripts/<config> --snap
```

For `--unoverlay`:

```
cd /work && python3 /scripts/overlay-mount.py unsync --config /scripts/<config> --snap
```

followed (on success) by:

```
sudo snap restart maas
```

`cd /work` is essential: the tool's `.overlayfs_workdir/` is created relative to
CWD, and overlayfs requires it to share a filesystem with the `/work/src/...`
upperdirs. With CWD=`/work`, the workdir lands at `/work/.overlayfs_workdir/...`
(container rootfs, same filesystem as `/work/src`). Running from `/scripts` (a host
bind mount = different filesystem) would make the mount fail.

`--dry-run` reuses the existing global `DRY_RUN` / `_echo_dry` path: `lxc_exec`
echoes the `lxc exec` command and executes nothing (the MAAS restart is likewise
echoed, not run).

The overlay command builder (CWD + interpreter + script + subcommand + config +
`--snap`) is factored into a small pure helper so it is unit-testable.

## Profile change (required)

Add `python3-yaml` to the `packages:` list in `lxd-maas-profile.yaml`'s
`user.vendor-data` cloud-config, so every newly created container has PyYAML at
first boot (the tool imports `yaml`):

```yaml
packages:
- git
- build-essential
- jq
- pgcli
- rsync
- python3-yaml
```

**Decision:** profile only — no runtime auto-install. Environments created before
this change must be recreated (or have `python3-yaml` installed manually) to be
overlay-able. Acceptable for a dev tool, consistent with how `rsync` was handled
for `--sync`.

## `--sync` exclude change (required)

Add `.overlayfs_workdir` to the `--sync` `EXCLUDES` list. Once overlays are mounted,
the tool's workdir lives at `/work/.overlayfs_workdir`. Without the exclude, a later
`--sync` (which uses `--delete` against a host source that has no such directory)
would try to delete it — churning or failing on the live workdir.

```python
EXCLUDES = [".git", "__pycache__", "*.pyc", ".overlayfs_workdir"]
```

## Edge cases & error handling

- **`--overlay`/`--unoverlay` without `--overlay-config`** → hard error, exit
  non-zero, no container touched.
- **Config missing / not a file / outside the repo** → hard error, exit non-zero.
- **Container missing or not running** → warn, skip, continue; counts toward a
  non-zero final exit code.
- **Overlay tool failure on a container** (e.g. snap not installed, mount denied) →
  the tool prints its own error and exits non-zero; surface it, continue with the
  rest, non-zero final exit code. MAAS is **not** restarted for a failed container.
- **More than one action flag** (`--overlay` + `--sync` etc.) → argparse-level error.
- **`--dry-run`** → echo each container's `lxc exec` overlay command and the snap
  restart; execute nothing.

## Testing

Extends the existing `tests/test_maas_env.py` (stdlib `unittest`, script loaded via
`importlib`). No live LXD required for unit tests.

- **New pure helpers (unit-tested):**
  - `container_config_path(host_config, repo_root) -> str` — returns
    `/scripts/<relpath>`; errors when the config is outside the repo.
  - `build_overlay_command(python, script_container_path, subcommand, config_container_path) -> str`
    — returns the `cd /work && python3 /scripts/overlay-mount.py <sub> --config ... --snap`
    string; assert subcommand is `sync`/`unsync`, config path, `--snap`, and the
    `cd /work` prefix.
- **Unit tests:**
  - config path mapping for a repo-root config and a nested config; rejection of a
    path outside the repo and of a non-existent file.
  - overlay command string for both `sync` and `unsync`.
  - (existing sync tests) `EXCLUDES` now contains `.overlayfs_workdir`.
- **Manual e2e checklist:**
  1. Create a multi env on the matching channel; confirm `python3-yaml` present.
  2. `--sync ~/work/maas`; then `--overlay --overlay-config overlay-config-37.yaml`;
     confirm overlays mounted (`mount | grep overlay`) on the snap paths.
  3. Make a visible code change on the host; `--sync` then `--overlay`; confirm the
     change is live after MAAS restarts.
  4. `--unoverlay --overlay-config ...`; confirm overlays gone and MAAS back to stock.
  5. `--dry-run` prints the overlay + restart commands and changes nothing.

## Risks

- **Nested overlay over snap squashfs.** Mounting an overlay onto a path inside the
  read-only snap squashfs, inside a nested LXD container, must be validated at
  runtime. `security.nesting: true` is already set in the profile.
- **Live upperdir writes.** overlayfs discourages modifying the `upperdir` while
  mounted. Re-running `--overlay` (unmount + mount) after each `--sync` sidesteps
  this by re-establishing a clean overlay, so the recommended loop is sync → overlay.

## Alternatives considered

- **Ship the tool via git submodule or runtime download** — rejected in favor of
  vendoring: it is the same author's tool, the repo is already bind-mounted at
  `/scripts`, and vendoring avoids network/submodule friction and pins the version.
- **Derive the config from `--maas-channel`** — rejected in favor of an explicit
  `--overlay-config`: no hidden channel→config mapping to maintain, and the user
  already knows which version they deployed.
- **`--overlay` implicitly runs `--sync` first** — rejected to keep the stages
  composable and individually debuggable.
- **Runtime auto-install of `python3-yaml`** — rejected (profile only) per the
  same reasoning used for `rsync` in `--sync`.

## Future / out of scope

- `--deb` mode support if a deb-based container flow appears.
- A combined one-shot `--sync --overlay` convenience flow.
- Per-node overlay targeting.

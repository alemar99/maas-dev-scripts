# Design: `--deb` install option for `maas-env.py`

- **Date:** 2026-06-30
- **Status:** Draft (pending user review)
- **Component:** `maas-env.py`, `maas-install.sh`, `tests/test_maas_env.py`

## Overview

`maas-env.py` creates/destroys MAAS test environments in LXD. Today the create
flow installs MAAS from a **snap**: it sets up a separate PostgreSQL on node1,
clones the MAAS source into `/work`, installs the maas snap, connects snap
interfaces, and runs `maas init region+rack` against the external database. The
`--overlay`/`--unoverlay` actions then overlay `/work/src/...` onto the installed
package paths via the vendored `overlay-mount.py` tool, which currently always
runs with `--snap`.

This adds a parallel **deb** install path. When `--deb` is passed, the create
flow installs MAAS from a PPA-provided `.deb` (which provisions its own local
PostgreSQL) and runs the deb-style `maas init`. The overlay actions can likewise
target the deb package paths by passing `--deb`, which selects the `deb:` section
of the overlay config (both sections already exist in `overlay-config-*.yaml`).

## Goals

- A `--deb` flag that switches the create flow from the snap install to a deb
  install via a user-specified PPA.
- The deb install runs exactly: `add-apt-repository`, `apt update`,
  `apt install -y maas`, `maas init --skip-admin --candid-domain '' --rbac-url ''`.
- `--overlay`/`--unoverlay --deb` drive `overlay-mount.py --deb` so the existing
  `deb:` overlay config section is used.
- Keep the snap path's behavior unchanged.
- Preserve the project's pure-helper + unit-test pattern.

## Non-goals

- Deb support for `--mode multi` (deb provisions a per-node local DB, so three
  nodes would be three independent MAAS instances, not a shared cluster). Deb is
  **single mode only** for now.
- Wiring the deb path to an external/shared PostgreSQL.
- Changing the snap install path, the `--sync` action, or `overlay-mount.py`.

## Decisions (from brainstorming)

- **Method selection:** a boolean `--deb` flag; absence = snap (current default).
- **PPA source:** a new `--ppa` CLI argument (e.g. `ppa:maas/3.7`).
- **Database:** deb uses the package's own local PostgreSQL; `setup_postgres` is
  skipped for the deb path.
- **Mode:** deb is single mode only; `--deb --mode multi` is an error.
- **Install script shape:** one `maas-install.sh` that branches on a leading
  method argument.
- **Clone branch:** a new `--branch` argument, used for the deb clone; **required
  when `--deb`** is given.
- **Stray flags:** `--ppa`/`--branch` without `--deb` is a hard error.

## CLI interface

New arguments on the existing argparse CLI:

- `--deb` — store_true. Selects the deb install path for `create`, and selects
  the `deb:` overlay section for `--overlay`/`--unoverlay`. Absence = snap.
- `--ppa PPA` — PPA string passed to `add-apt-repository` (e.g. `ppa:maas/3.7`).
- `--branch NAME` — git branch cloned into `/work` for the deb path.

Validation is two-tier (performed before any container work):

**Global rule** (checked early in `main()`, regardless of action):

- `--ppa` or `--branch` passed **without** `--deb` → error, exit non-zero. These
  flags are deb-only parameters and are meaningless on their own.

**Create-action rules** (checked only in the create branch):

- `--deb` **requires** both `--ppa` and `--branch`; missing either → error, exit
  non-zero.
- `--deb` with `--mode multi` → error, exit non-zero (single mode only).

For `--overlay`/`--unoverlay`, `--deb` only selects the `deb:` config section; it
does **not** require `--ppa`/`--branch` (the create-action rules don't run there).
Passing `--ppa`/`--branch` alongside `--overlay --deb` is harmless but unused. The
global rule still rejects `--ppa`/`--branch` whenever `--deb` is absent.

Examples:

```
# snap (unchanged)
./maas-env.py --name dev --maas-channel 3.7/edge

# deb
./maas-env.py --name dev --deb --ppa ppa:maas/3.7 --branch 3.7

# overlay the deb package paths
./maas-env.py --name dev --deb --overlay --overlay-config overlay-config-37.yaml
```

## Behavior / flow

### `create` with `--deb`

1. Validate deb args (see above).
2. Single mode → no network.
3. Create containers; wait for cloud-init.
4. Run `--pre` scripts.
5. **Skip `setup_postgres`.**
6. Install: run `maas-install.sh deb <ppa> <branch>` on the (single) container.
7. `create_admin()` on the container (init used `--skip-admin`, so an admin must
   still be created; the existing helper derives the node IP via `hostname -I`
   and works unchanged).
8. Run `--post` scripts.
9. Summary: there is no `db_ip` in the deb path, so capture the node's own IP
   (`hostname -I | cut -d' ' -f1`) for the "MAAS URL" line.

The snap path is unchanged: `setup_postgres` → `install_maas(... snap ...)` →
`create_admin` → post scripts, with the summary using the postgres/node IP as
today.

### `maas-install.sh` (restructured)

The script takes a leading **method** argument and branches. The git clone +
`chown` of `/work` is shared; only the branch source and the install/init steps
differ.

- `maas-install.sh snap <db_ip> <channel>` — current behavior:
  - branch derived from channel (`latest` → `master`, else the track),
  - clone `/work`, chown,
  - install snapd/core26/maas snap, `connect-snap-interfaces`,
  - `maas init region+rack --database-uri=... --maas-url=...`.
- `maas-install.sh deb <ppa> <branch>` — new path:
  - clone `/work` at `<branch>`, chown (no channel→branch derivation),
  - `sudo add-apt-repository -y "<ppa>"`,
  - `sudo apt update`,
  - `sudo apt install -y maas`,
  - `sudo maas init --skip-admin --candid-domain '' --rbac-url ''`,
  - **no** `connect-snap-interfaces` (snap-only).

`add-apt-repository` gets `-y` so it runs non-interactively in the container.

### `--overlay` / `--unoverlay` with `--deb`

`build_overlay_command()` gains a `method` parameter and emits `--deb` when
`method == "deb"`, else `--snap` (unchanged default). `overlay()` and `main()`
thread `args.deb` through. `--overlay --deb` therefore runs
`overlay-mount.py sync --config <cfg> --deb`, mounting the `deb:` section.

After (un)mounting, `overlay()` restarts MAAS so it picks up the overlaid code.
The restart command is method-dependent: snap → `sudo snap restart maas`
(unchanged); deb → `sudo systemctl restart 'maas-*'` (the deb's MAAS systemd
units). This is encapsulated in a `build_restart_command(method)` helper.

Overlay and create are independent invocations; `--deb` is supplied on whichever
command the user runs.

## Code structure / testability

Follow the existing pure-helper + `unittest` pattern:

- **`build_install_invocation(method, *, db_ip=None, channel=None, ppa=None,
  branch=None) -> str`** — returns the string passed to `lxc_exec`, e.g.
  `"/scripts/maas-install.sh snap <db_ip> <channel>"` or
  `"/scripts/maas-install.sh deb <ppa> <branch>"`. Unit-testable like the other
  builders. `install_maas()` uses it.
- **`build_overlay_command(python, script, subcommand, config, method)`** —
  add `method`; emit `--snap`/`--deb`.
- **`build_restart_command(method) -> str`** — `sudo snap restart maas` for snap,
  `sudo systemctl restart 'maas-*'` for deb. Used by `overlay()`.
- **`validate_deb_flags(deb, ppa, branch) -> str | None`** — the global rule:
  returns an error message if `ppa`/`branch` are set without `deb`, else `None`.
  Called early in `main()`.
- **`validate_deb_create_args(deb, mode, ppa, branch) -> str | None`** — the
  create-action rules: `deb` requires `ppa` and `branch`, and `deb` forbids
  `mode == "multi"`. Called from the create branch of `main()`. Both validators
  are pure and testable without invoking `main()`.

Tests (`tests/test_maas_env.py`):

- Update `TestBuildOverlayCommand` to pass `method` and assert `--snap`/`--deb`;
  add a deb case.
- Add `TestBuildRestartCommand`: snap → `snap restart maas`; deb → `systemctl
  restart 'maas-*'`.
- Add `TestBuildInstallInvocation` for the snap and deb forms.
- Add `TestValidateDebFlags`: ppa without deb → error; branch without deb →
  error; neither → ok; deb alone → ok.
- Add `TestValidateDebCreateArgs`: deb+multi → error; deb without ppa → error;
  deb without branch → error; valid deb args → ok; plain snap args → ok.

## Edge cases & error handling

- `--deb` without `--ppa` or without `--branch` → hard error before any container
  is touched.
- `--ppa`/`--branch` without `--deb` → hard error.
- `--deb --mode multi` → hard error.
- `--dry-run` → prints the `maas-install.sh deb ...` invocation and the overlay
  command without executing, reusing the existing `DRY_RUN`/`_echo_dry` path.

## Manual e2e checklist

1. `./maas-env.py --name dev --deb --ppa ppa:maas/3.7 --branch 3.7` creates a
   single container, installs the deb, and the MAAS UI is reachable.
2. `/work` exists in the container, owned by `ubuntu:ubuntu`, on branch `3.7`.
3. Admin `maas / maas` can log in.
4. `./maas-env.py --name dev --deb --overlay --overlay-config overlay-config-37.yaml`
   mounts the `deb:` overlay paths; `--unoverlay --deb` removes them.
5. `--deb --mode multi`, `--deb` without `--ppa`, `--deb` without `--branch`, and
   `--ppa`/`--branch` without `--deb` each fail fast with a clear message.
6. The snap path (no `--deb`) is unchanged end to end.

## Future / out of scope

- Deb `--mode multi` with a shared external database.
- Deriving the deb branch automatically from the PPA.

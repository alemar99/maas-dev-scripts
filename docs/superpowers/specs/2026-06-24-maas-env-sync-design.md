# Design: `--sync` action for `maas-env.py`

- **Date:** 2026-06-24
- **Status:** Approved (ready for implementation plan)
- **Component:** `maas-env.py`, `lxd-maas-profile.yaml`

## Overview

`maas-env.py` creates/destroys MAAS test environments in LXD. Each container gets
a clone of the MAAS source at `/work` (via `maas-install.sh`), and the external
`overlay_mount` tool overlays `/work/src/...` onto the installed package paths
(see `overlay-config-*.yaml`).

This adds a development inner-loop capability: edit the MAAS repo **once** on the
host (e.g. `~/work/maas`), then push those edits into **every** container's `/work`
with a single command. Applying the synced code (running `overlay_mount`) remains
a separate, later step and is out of scope here.

## Goals

- A standalone `--sync PATH` action that rsyncs a host MAAS checkout into each
  container's codebase directory.
- Fast, repeatable incremental syncs suitable for a tight edit→sync→test loop.
- Exact mirror semantics (`--delete`) so containers don't drift from the host.
- No per-container provisioning beyond what the LXD profile already guarantees.

## Non-goals

- Running or integrating with `overlay_mount` (separate, later step).
- Building artifacts (UI/Go binaries) — the user builds on the host if needed;
  build outputs under `/work/src/.../build` are synced like any other file.
- Bidirectional sync (container → host). One-way host → container only.
- SSH-based transport (evaluated and rejected; see Alternatives).

## CLI interface

New action flag and option added to the existing argparse-based CLI:

- `--sync PATH` — host MAAS repo to push (e.g. `~/work/maas`). `~` is expanded.
  Presence of this flag selects the **sync action**: the script syncs and exits;
  it does not create or destroy.
- `--sync-dest DIR` — destination directory inside each container.
  Default: `/work`.

Reused existing arguments:

- `--name` (required) and `--mode` (`single`|`multi`) — determine the container
  list, identically to create/destroy.
- `--dry-run` — print the commands that would run, execute nothing.

Constraints:

- `--sync` and `--destroy` are mutually exclusive (error if both given).
- Excludes are fixed (not user-configurable in this iteration):
  `.git/`, `__pycache__/`, `*.pyc`.

Example:

```
./maas-env.py --name mytest --mode multi --sync ~/work/maas
./maas-env.py --name mytest --mode multi --sync ~/work/maas --sync-dest /work --dry-run
```

## Behavior / flow

1. **Validate source.** Expand `~` on `PATH`; error and exit non-zero if it does
   not exist or is not a directory — before touching any container.
2. **Compute container list.** Same logic as create/destroy:
   - `single` → `[name]`
   - `multi`  → `[name-1, name-2, name-3]`
3. **For each container:**
   1. Check it is running. If missing or not running, **warn and skip** it.
   2. Run the rsync (see Transport + rsync options).
4. **Summary.** Report per-container success/skip/failure. Warn-and-continue on a
   container failure; exit non-zero if any container failed (skips count as
   failures for exit-code purposes so the loop is scriptable).

## Transport implementation (rsync via `lxc exec` shim)

rsync copies to a "remote" by launching a transport program and running a second
rsync on the far end in `--server` mode, piping data over that transport's
stdin/stdout. Normally the transport is SSH:

```
rsync -a src/ user@host:/dest/   # internally: ssh user@host rsync --server -a . /dest/
```

i.e. rsync calls `<transport> <host> <remote-command...>`. The transport is
overridable via `-e`/`--rsh`. LXD has no SSH; you enter a container with
`lxc exec <container> -- <command>`. The argument order differs and `lxc exec`
rejects rsync's flags unless they follow `--`, so `-e "lxc exec"` cannot work
directly.

**Self-shim.** `maas-env.py` acts as its own transport adapter:

- The sync sets `-e "<python> <abspath-to-this-script> --rsh-shim"`, where
  `<python>` is `sys.executable` and the script path is `Path(__file__).resolve()`.
  (Assumption: this path contains no spaces, which holds for the current layout;
  rsync splits the `-e` string on whitespace.)
- rsync invokes:
  `<python> maas-env.py --rsh-shim <container> rsync --server ...`
- A check at the **very top of `main()`** (before normal argparse) detects
  `sys.argv[1] == "--rsh-shim"`, then:
  - `container = sys.argv[2]`
  - `remote_cmd = sys.argv[3:]`
  - `os.execvp("lxc", ["lxc", "exec", "--user", "1000", "--group", "1000",
    container, "--", *remote_cmd])`

This re-issues rsync's request as a proper `lxc exec`, establishing the pipe.
No temp files, no separate shim script, nothing to install in the container
beyond rsync itself.

**Ownership.** The container-side rsync runs as **uid/gid 1000 (ubuntu)** via
`lxc exec --user 1000 --group 1000`, so synced files keep the `ubuntu:ubuntu`
ownership that `maas-install.sh` sets on `/work` (rather than becoming root-owned).
The destination path is passed to `rsync --server` explicitly, so the shim's
working directory is irrelevant.

**rsync availability.** rsync is guaranteed present in every container by adding
it to the LXD profile's cloud-init packages (see Profile change). There is **no**
runtime check or auto-install step.

## rsync options

Host-side invocation per container:

```
rsync -a --no-owner --no-group --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  -e "<python> <script> --rsh-shim" \
  <source>/ <container>:<dest>/
```

- `-a` (archive) preserves perms/times/symlinks/recursion. `--no-owner
  --no-group` disable owner/group preservation, which the non-root (uid 1000)
  transport cannot set anyway; files end up owned by the executing user.
- `--delete` makes `<dest>` an exact mirror of `<source>`. Excluded paths (e.g.
  the container's `.git`) are **preserved**, not deleted, because `--delete`
  does not remove excluded files (no `--delete-excluded`).
- **Trailing slash** is enforced on `<source>` so its *contents* land in
  `<dest>` (`/work`), not nested under `/work/maas`.

## Profile change (required)

Add `rsync` to the `packages:` list in `lxd-maas-profile.yaml`'s
`user.vendor-data` cloud-config, so every newly created container has rsync at
first boot:

```yaml
packages:
- git
- build-essential
- jq
- pgcli
- rsync
```

Note: pre-existing environments created before this change must be recreated (or
have rsync installed manually) to be syncable. This is acceptable for a dev tool.

## Edge cases & error handling

- **Source missing / not a directory** → hard error, exit non-zero, no container
  touched.
- **Container missing or not running** → warn, skip that container, continue with
  the rest; counts toward a non-zero final exit code.
- **rsync failure on a container** (e.g. transport error) → warn with the exit
  code, continue with the rest; non-zero final exit code.
- **Both `--sync` and `--destroy`** → argparse-level error.
- **`--dry-run`** → print the exact rsync command (including the `-e` shim string
  and `container:dest` target) for each container; execute nothing. Reuses the
  existing global `DRY_RUN` mechanism / `_echo_dry` helper.

## Testing

The repo currently has no tests. To keep this verifiable without a live LXD:

- **Refactor for testability** (pure functions, no side effects):
  - `compute_containers(name, mode) -> list[str]` — extract the container-list
    logic currently inlined in `main()`.
  - `normalize_source(path) -> str` — expand `~`, ensure a single trailing slash.
  - `build_rsh_value(python, script) -> str` — the `-e` transport string.
  - `build_rsync_command(source, container, dest, rsh, excludes) -> list[str]` —
    the full host-side rsync argv.
- **Unit tests** (e.g. a `tests/` dir, stdlib `unittest` or pytest if available):
  - container list for single vs multi.
  - source normalization adds exactly one trailing slash and expands `~`.
  - rsync command contains `-a`, `--no-owner`, `--no-group`, `--delete`, all
    three `--exclude` patterns, the correct `-e` shim string, and a
    `container:dest/` target with trailing slash.
  - shim arg parsing: given `["--rsh-shim", "c1", "rsync", "--server", "x"]`,
    the constructed `lxc exec` argv is
    `["lxc","exec","--user","1000","--group","1000","c1","--","rsync","--server","x"]`
    (validate via a builder function rather than calling `os.execvp`).
- **Manual e2e checklist:**
  1. Create a multi env; confirm each container has rsync.
  2. Edit a file under `~/work/maas`; run `--sync`; verify the change in all three
     `/work` copies.
  3. Delete a file on the host; run `--sync`; verify it is removed in containers.
  4. Verify each container's `/work/.git` still exists after sync.
  5. Verify synced files are owned by `ubuntu:ubuntu` in the container.
  6. `--dry-run` prints the rsync commands and changes nothing.

## Alternatives considered

- **`lxc file push --recursive`** — no shim needed, but no delta (full re-copy
  each time) and no native `--delete`/`--exclude`; rejected for failing the
  chosen mirror semantics and being slow on a large repo.
- **Bind-mount host repo → `/work`** — instant and zero-copy, but it is not rsync,
  forces all containers to share identical files, and may interfere with
  `overlay_mount` using `/work` as an overlay lowerdir; rejected.
- **rsync over SSH** — standard transport, but requires installing/running
  `openssh-server`, provisioning SSH keys into each container, discovering
  container IPs, and managing host-key churn across container recreation. More
  moving parts than the shim; rejected for this feature. (Reconsider if SSH into
  the containers becomes generally desirable for other tooling.)

## Future / out of scope

- Optional integration to run `overlay_mount` after a sync.
- User-configurable excludes / opt-in `--delete` toggle.
- Per-node sync targeting (currently always all containers).

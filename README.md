# MAAS Dev Scripts

Helper scripts I use when developing [MAAS](https://github.com/canonical/maas).

The centrepiece is `maas-env.py`, a CLI that spins up full MAAS environments inside LXD containers and enables a fast edit → sync → test inner loop without rebuilding or reinstalling MAAS.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [maas-env.py Reference](#maas-envpy-reference)
  - [create snap](#create-snap)
  - [create deb](#create-deb)
  - [destroy](#destroy)
  - [list](#list)
  - [sync](#sync)
  - [overlay apply](#overlay-apply)
  - [overlay remove](#overlay-remove)
- [Supporting Scripts](#supporting-scripts)
- [Configuration Files](#configuration-files)
- [Real-World Use Cases](#real-world-use-cases)
- [Running the Tests](#running-the-tests)

---

## How It Works

`maas-env.py` manages MAAS environments that live inside LXD containers. Each environment is a fully installed MAAS instance. This repo is bind-mounted into every container at `/scripts`, so all helper scripts are automatically available inside containers without copying.

The **overlay** feature is the key productivity tool. Instead of reinstalling MAAS every time you change a Python file, `overlay-mount.py` uses Linux overlayFS to shadow the installed MAAS package directories with your local source tree. Edit code on the host, sync it in, apply the overlay — MAAS restarts with your changes live in seconds.

```
Host                            LXD Container
──────────────────────          ──────────────────────────────────────────
~/work/maas/src/  ──sync──►  /work/src/
                                  │
                             overlayFS
                                  │
                             /snap/maas/current/lib/python3.12/site-packages/
                             (MAAS sees your edits, not the installed files)
```

---

## Prerequisites

- **LXD** installed and initialised (`lxd init`)
- **Python 3.10+** on the host
- **rsync** on the host
- A MAAS source checkout (e.g. `~/work/maas`) for the sync/overlay workflow

---

## Quick Start

```bash
# 1. Clone this repo
git clone <this-repo> ~/maas-dev-scripts
cd ~/maas-dev-scripts

# 2. Create a single-node MAAS snap environment
./maas-env.py create snap myenv --channel 3.7/edge

# 3. Edit MAAS source on your host, then sync it in
./maas-env.py sync myenv ~/work/maas

# 4. Overlay your source over the installed paths — MAAS restarts automatically
./maas-env.py overlay apply myenv --config overlay-config-37.yaml

# 5. Iterate: edit, sync, overlay apply (repeat)

# 6. Remove overlays to go back to the stock installation
./maas-env.py overlay remove myenv --config overlay-config-37.yaml

# 7. Tear everything down when you're done
./maas-env.py destroy myenv
```

---

## maas-env.py Reference

```
usage: maas-env.py <command> [options]

commands:
  create snap   Create a MAAS environment using a snap package
  create deb    Create a MAAS environment using a deb package
  destroy       Tear down an environment and its LXD resources
  list          List all tracked environments
  sync          Rsync a host source tree into environment containers
  overlay apply  Mount source over installed MAAS paths
  overlay remove Unmount source overlays
```

All commands accept `--dry-run` to print what would happen without executing.

---

### create snap

Creates one or more LXD containers, installs MAAS from a snap channel, and initialises the database.

```
./maas-env.py create snap NAME [options]

Arguments:
  NAME              Name of the environment (also used as LXD container name)

Options:
  --mode            single (default) or multi (3-node region+rack cluster)
  --channel         MAAS snap channel  [default: latest/edge]
  --ubuntu          Ubuntu release for the container  [default: 26.04]
  --profile         LXD profile YAML  [default: ./lxd-maas-profile.yaml]
  --pre PATH:TARGET  Script to run before MAAS init. TARGET is all, node1, node2, or node3
  --post PATH:TARGET Script to run after MAAS init. Same format. Repeatable.
  --dry-run         Print commands, do not execute
```

**Examples:**

```bash
# Minimal: single-node on latest/edge
./maas-env.py create snap dev1

# Specific channel, explicit Ubuntu version
./maas-env.py create snap dev37 --channel 3.7/edge --ubuntu 24.04

# Three-node cluster on 3.7/stable
./maas-env.py create snap cluster1 --mode multi --channel 3.7/stable

# Run a custom setup script on all nodes before MAAS init, another only on node1 after
./maas-env.py create snap myenv \
  --pre ./pre-setup.sh:all \
  --post ./node1-setup.sh:node1

# Preview what would happen without running anything
./maas-env.py create snap preview-env --dry-run
```

---

### create deb

Creates a single-node LXD container and installs MAAS from a PPA (deb package). Also clones the specified MAAS git branch into `/work` inside the container.

```
./maas-env.py create deb NAME --ppa PPA --branch BRANCH [options]

Required:
  --ppa             PPA to install MAAS from  (e.g. ppa:maas/3.7)
  --branch          Git branch to clone into /work  (e.g. main, 3.7)

Options:
  --ubuntu          Ubuntu release for the container  [default: 26.04]
  --profile         LXD profile YAML  [default: ./lxd-maas-profile.yaml]
  --pre / --post    Same as create snap
  --dry-run
```

**Examples:**

```bash
# Install from the 3.7 PPA, check out the 3.7 branch
./maas-env.py create deb deb-env --ppa ppa:maas/3.7 --branch 3.7
```

---

### destroy

Stops and deletes all containers belonging to an environment, and removes the associated LXD network.

```
./maas-env.py destroy NAME [--dry-run]
```

**Examples:**

```bash
./maas-env.py destroy dev1
./maas-env.py destroy cluster1 --dry-run   # preview teardown steps
```

---

### list

Lists all environments currently tracked in the local registry.

```
./maas-env.py list
```

**Example output:**

```
NAME        MODE    TYPE   CHANNEL       CREATED
──────────  ──────  ─────  ────────────  ───────────────────
dev37       single  snap   3.7/edge      2026-07-10 09:00:00
cluster1    multi   snap   3.7/stable    2026-07-11 14:22:05
deb-env     single  deb    ppa:maas/3.7  2026-07-12 08:45:30
```

---

### sync

Rsyncs a host directory into all containers of an environment. Uses LXD exec as the rsync transport — no SSH required.

```
./maas-env.py sync NAME PATH [options]

Arguments:
  NAME    Environment name
  PATH    Host directory to sync (e.g. ~/work/maas)

Options:
  --dest  Destination path inside containers  [default: /work]
  --dry-run
```

**Examples:**

```bash
# Sync your MAAS checkout into /work (default)
./maas-env.py sync dev37 ~/work/maas

# Sync to a custom destination
./maas-env.py sync dev37 ~/work/maas --dest /opt/maas-src

# Preview what rsync would transfer
./maas-env.py sync dev37 ~/work/maas --dry-run
```

Syncing into a multi-node environment pushes to all three containers in parallel.

---

### overlay apply

Mounts your synced source tree over the installed MAAS Python packages using overlayFS (or bind mounts for single files). MAAS is restarted automatically so changes take effect immediately.

```
./maas-env.py overlay apply NAME --config PATH [--dry-run]

Arguments:
  NAME        Environment name
  --config    Path to the overlay config YAML (must be inside this repo)
```

**Examples:**

```bash
# Apply overlays for a 3.7 snap environment
./maas-env.py overlay apply dev37 --config overlay-config-37.yaml

# Apply overlays for a master branch environment
./maas-env.py overlay apply main-dev --config overlay-config-master.yaml

# Dry run to see what mounts would be created
./maas-env.py overlay apply dev37 --config overlay-config-37.yaml --dry-run
```

---

### overlay remove

Unmounts all overlays and reverts MAAS to the stock installed files. MAAS is restarted.

```
./maas-env.py overlay remove NAME --config PATH [--dry-run]
```

**Examples:**

```bash
./maas-env.py overlay remove dev37 --config overlay-config-37.yaml
```

---

## Supporting Scripts

These scripts run inside containers (via `lxc exec`) or on the host for specific setup tasks.

| Script | Purpose |
|--------|---------|
| `maas-install.sh` | Installs MAAS (snap or deb) and initialises it. Called by `create`. |
| `postgres-setup.sh` | Installs PostgreSQL and creates the `maasdb` database. |
| `postgres-cleanup.sh` | Drops and recreates `maasdb`. Useful for resetting state without destroying the container. |
| `setup_tls.sh` | Generates a self-signed cert and enables TLS on the MAAS snap (HTTPS on port 5443). |
| `candid_setup.sh` | Configures MAAS to use a local Candid server for external authentication. |
| `rbac_setup.sh` | Configures MAAS to use RBAC (Role-Based Access Control) via Candid. |
| `oidc_setup.sh` | Registers an OIDC provider (Auth0) with MAAS. Credentials live in `oidc_vars.sh`. |
| `ss-mirror-setup.sh` | Sets up a local simplestreams mirror of Ubuntu images served via Apache on port 8001. |
| `custom_boot_source_setup.sh` | Registers the local image mirror as a MAAS boot source. |
| `upload_custom_boot_resource.sh` | Uploads a custom OS image to MAAS via the REST API. |
| `curl-v2.sh` | Helper to call MAAS API v2 endpoints with OAuth 1.0 authentication. |
| `temporal-codecserver-setup.sh` | Builds the Temporal codec server binary from source. |
| `temporal-codecserver-run.sh` | Runs the Temporal codec server on port 8090. |
| `temporal-ui-setup.sh` | Installs and configures the Temporal UI snap with MAAS TLS certificates. |


## Configuration Files

### lxd-maas-profile.yaml

Applied to every container at creation time. Key settings:

- `security.nesting: true` — required for overlayFS to work inside LXD
- cloud-init installs `git`, `rsync`, `python3-yaml`, `build-essential`, `jq`
- Bind-mounts this repo into every container at `/scripts`

If you move this repo to a different path, update the `source:` entry in this file.

---

### overlay-config-37.yaml

Overlay mappings for MAAS **3.7**. Maps `/work/src/<module>` to the installed snap/deb paths.

Use with: `./maas-env.py overlay apply myenv --config overlay-config-37.yaml`

---

### overlay-config-master.yaml

Overlay mappings for MAAS **master branch**. Includes additional mappings for the UI static files and binary agents (`maas-agent`, `maas-netmon`) that are present in master but not in 3.7.

Use with: `./maas-env.py overlay apply myenv --config overlay-config-master.yaml`

---

## Environment Registry

Environments are persisted to `~/.local/share/maas-env/environments.db` (SQLite). This is how `destroy` and `list` know about previously created environments across shell sessions.

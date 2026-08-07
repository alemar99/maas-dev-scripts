# maas-env.py `--overlay` / `--unoverlay` Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--overlay` and `--unoverlay` actions to `maas-env.py` that drive the vendored `overlay-mount.py` tool inside every container to overlay-mount `/work/src/*` onto the installed MAAS snap paths (and tear them down), restarting MAAS each time.

**Architecture:** The `overlay-mount` tool is vendored into the repo root so it is visible at `/scripts/overlay-mount.py` inside every container (the repo is bind-mounted at `/scripts`). For each container, `maas-env.py` runs `cd /work && python3 /scripts/overlay-mount.py {sync,unsync} --config /scripts/<cfg> --snap` via the existing root `lxc_exec`, then `sudo snap restart maas`. Path mapping and command building are factored into pure functions for unit testing. PyYAML (needed by the tool) is guaranteed in containers by adding `python3-yaml` to the LXD profile's cloud-init packages.

**Tech Stack:** Python 3 stdlib (`argparse`, `subprocess`, `os`, `pathlib`), `unittest`, LXD (`lxc`), OverlayFS, the MAAS snap, PyYAML (in-container).

**Spec:** `docs/superpowers/specs/2026-06-24-overlay-mount-design.md`

---

## File Structure

- **Modify `maas-env.py`** — add pure helpers (`container_config_path`, `build_overlay_command`), the `OVERLAY_SCRIPT` constant, the `overlay()` action, `.overlayfs_workdir` in `EXCLUDES`, the `--overlay`/`--unoverlay`/`--overlay-config` CLI args, and `main()` wiring.
- **Create `overlay-mount.py`** (repo root) — the vendored overlay tool, reproduced verbatim from `github.com/alemar99/overlay-mount-tool`.
- **Modify `lxd-maas-profile.yaml`** — add `python3-yaml` to the cloud-init `packages:` list.
- **Modify `tests/test_maas_env.py`** — add unit tests for the two new pure helpers and the `EXCLUDES` change.

**Insertion convention for new functions:** Each new top-level pure helper is inserted immediately *before* `def build_parser() -> argparse.ArgumentParser:`. The `overlay()` action is inserted immediately *before* `def main() -> None:`. Both anchors remain present for every subsequent task.

**Test-append convention:** Each task adds a test class by replacing the file's trailing:

```python
if __name__ == "__main__":
    unittest.main()
```

block with the new class followed by that same trailing block.

**Run tests with:** `python3 -m unittest tests.test_maas_env -v` (from the repo root). The suite currently has **16** passing tests.

---

### Task 1: `container_config_path` helper

**Files:**
- Modify: `maas-env.py` (add `container_config_path` before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestContainerConfigPath`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestContainerConfigPath(unittest.TestCase):
    def test_maps_repo_root_config(self):
        self.assertEqual(
            maas_env.container_config_path(
                "/repo/overlay-config-37.yaml", "/repo"
            ),
            "/scripts/overlay-config-37.yaml",
        )

    def test_maps_nested_config(self):
        self.assertEqual(
            maas_env.container_config_path("/repo/configs/x.yaml", "/repo"),
            "/scripts/configs/x.yaml",
        )

    def test_rejects_config_outside_repo(self):
        with self.assertRaises(ValueError):
            maas_env.container_config_path("/etc/x.yaml", "/repo")

    def test_respects_custom_mount(self):
        self.assertEqual(
            maas_env.container_config_path(
                "/repo/c.yaml", "/repo", mount="/mnt/repo"
            ),
            "/mnt/repo/c.yaml",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'container_config_path'`

- [ ] **Step 3: Implement `container_config_path`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def container_config_path(
    host_config: str, repo_root: str, mount: str = "/scripts"
) -> str:
    """Map a host overlay-config path to its path inside a container.

    The repo is bind-mounted at `mount` (default /scripts) in every container, so
    the config must live inside `repo_root`. Returns `<mount>/<relpath>`. Raises
    ValueError if the config is outside the repo.
    """
    abs_config = os.path.abspath(os.path.expanduser(host_config))
    abs_root = os.path.abspath(os.path.expanduser(repo_root))
    rel = os.path.relpath(abs_config, abs_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError(
            f"overlay config must live inside the repo ({abs_root}): {host_config}"
        )
    return f"{mount}/{rel}"


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (20 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add container_config_path helper for overlay configs"
```

---

### Task 2: `build_overlay_command` helper

**Files:**
- Modify: `maas-env.py` (add `build_overlay_command` before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestBuildOverlayCommand`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestBuildOverlayCommand(unittest.TestCase):
    def test_sync_command(self):
        self.assertEqual(
            maas_env.build_overlay_command(
                "python3",
                "/scripts/overlay-mount.py",
                "sync",
                "/scripts/overlay-config-37.yaml",
            ),
            "cd /work && python3 /scripts/overlay-mount.py sync "
            "--config /scripts/overlay-config-37.yaml --snap",
        )

    def test_unsync_command(self):
        self.assertEqual(
            maas_env.build_overlay_command(
                "python3",
                "/scripts/overlay-mount.py",
                "unsync",
                "/scripts/overlay-config-master.yaml",
            ),
            "cd /work && python3 /scripts/overlay-mount.py unsync "
            "--config /scripts/overlay-config-master.yaml --snap",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'build_overlay_command'`

- [ ] **Step 3: Implement `build_overlay_command`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def build_overlay_command(
    python: str,
    script: str,
    subcommand: str,
    config: str,
    workdir: str = "/work",
) -> str:
    """Build the in-container shell command that runs the overlay tool.

    Runs from `workdir` (must be on the container rootfs) so the tool's relative
    `.overlayfs_workdir` shares a filesystem with the `/work/src/...` upperdirs.
    """
    return (
        f"cd {workdir} && {python} {script} {subcommand} "
        f"--config {config} --snap"
    )


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (22 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add build_overlay_command helper"
```

---

### Task 3: Vendor `overlay-mount.py` into the repo

**Files:**
- Create: `overlay-mount.py` (repo root)

- [ ] **Step 1: Create the vendored tool**

Create `overlay-mount.py` with exactly this content (verbatim from `github.com/alemar99/overlay-mount-tool`, same author as this repo):

```python
#!/usr/bin/env python3

import argparse
import subprocess
import sys
import yaml
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class Executor(ABC):
    """Abstract base class for command executors."""

    @abstractmethod
    def run_command(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        pass

    @abstractmethod
    def create_directory(self, path: str) -> None:
        pass

    @abstractmethod
    def print_action(
        self, action: str, source: str, dest: Optional[str] = None
    ) -> None:
        pass


class DryRunExecutor(Executor):
    """Executor that prints commands instead of running them."""

    def run_command(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        print(f"[DRY RUN] {cmd}")
        # Return always a successful result for dry run
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def create_directory(self, path: str) -> None:
        print(f"[DRY RUN] create directory: {path}")

    def print_action(
        self, action: str, source: str, dest: Optional[str] = None
    ) -> None:
        if dest:
            print(f"[DRY RUN] {action}: {source} -> {dest}")
        else:
            print(f"[DRY RUN] {action}: {source}")


class RealExecutor(Executor):
    """Executor that actually runs commands."""

    def run_command(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if check and result.returncode != 0:
            print(f"Command failed: {cmd}")
            print(f"Error: {result.stderr}")
            sys.exit(1)
        return result

    def create_directory(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def print_action(
        self, action: str, source: str, dest: Optional[str] = None
    ) -> None:
        if dest:
            print(f"{action}: {source} -> {dest}")
        else:
            print(f"{action}: {source}")


def is_mounted(path: str, executor: Executor) -> bool:
    """Check if a path is currently mounted."""
    result = executor.run_command(f"mountpoint -q '{path}'", check=False)
    return result.returncode == 0


def get_snap_revision(package_name: str, executor: Executor) -> Optional[str]:
    """Get the current snap revision number for a package."""
    result = executor.run_command(f"readlink /snap/{package_name}/current", check=False)
    if result.returncode == 0:
        return result.stdout.strip().replace(f"/snap/{package_name}/", "")
    return None


def resolve_snap_path(path: str, package_name: str, revision: Optional[str]) -> str:
    """Replace /snap/{package}/current with actual revision in path."""
    if revision:
        return path.replace(
            f"/snap/{package_name}/current", f"/snap/{package_name}/{revision}"
        )
    return path


def validate_config(config: Dict[str, Any], method: str) -> Dict[str, Any]:
    """Validate configuration correctness."""
    if method not in config:
        print(f"No '{method}' section found in config file")
        sys.exit(1)

    section_config = config[method]

    if "package" not in section_config:
        print(f"No 'package' field specified in {method} section")
        sys.exit(1)

    package_name = section_config["package"]

    # Validate package is installed
    if method == "snap":
        result = subprocess.run(
            f"snap list {package_name}", shell=True, capture_output=True
        )
        if result.returncode != 0:
            print(f"Snap package '{package_name}' is not installed")
            sys.exit(1)
    elif method == "deb":
        result = subprocess.run(
            f"dpkg -l | grep -q '^ii.*{package_name}'",
            shell=True,
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"Deb package '{package_name}' is not installed")
            sys.exit(1)

    # Validate dirs and files sections exist (can be empty)
    if "dirs" not in section_config:
        section_config["dirs"] = {}
    if "files" not in section_config:
        section_config["files"] = {}

    return section_config


def unmount_overlays(config: Dict[str, Any], method: str, executor: Executor) -> None:
    """Unmount all overlay filesystems."""
    section_config = config[method]
    package_name = section_config["package"]

    revision = None
    if method == "snap":
        revision = get_snap_revision(package_name, executor)

    # Unmount file bind mounts
    files = section_config.get("files", {})
    for dest in files.values():
        if method == "snap":
            dest = resolve_snap_path(dest, package_name, revision)

        if is_mounted(dest, executor):
            executor.print_action("Unmounting file", dest)
            executor.run_command(f"sudo umount '{dest}'")

    # Unmount directory overlays
    dirs = section_config.get("dirs", {})
    for dest in dirs.values():
        if method == "snap":
            dest = resolve_snap_path(dest, package_name, revision)

        if is_mounted(dest, executor):
            executor.print_action("Unmounting overlay", dest)
            executor.run_command(f"sudo umount '{dest}'")


def mount_overlays(config: Dict[str, Any], method: str, executor: Executor) -> None:
    """Mount all overlay filesystems."""
    section_config = config[method]
    package_name = section_config["package"]

    workdir_base = f".overlayfs_workdir/{method}"
    revision = None

    if method == "snap":
        revision = get_snap_revision(package_name, executor)

    # Mount directory overlays
    dirs = section_config.get("dirs", {})
    for source, dest in dirs.items():
        work_dir = f"{workdir_base}/{source}"

        if method == "snap":
            dest = resolve_snap_path(dest, package_name, revision)

        # Create work directory
        executor.create_directory(work_dir)

        executor.print_action("Mounting overlay", source, dest)
        cmd = f"sudo mount -t overlay overlay -o lowerdir='{dest}',upperdir='{source}',workdir='{work_dir}' '{dest}'"
        executor.run_command(cmd)

    # Mount file bind mounts
    files = section_config.get("files", {})
    for source, dest in files.items():
        if method == "snap":
            dest = resolve_snap_path(dest, package_name, revision)

        executor.print_action("Mounting file", source, dest)
        executor.run_command(f"sudo mount -o bind,ro '{source}' '{dest}'")


def load_config(config_file: str) -> Dict[str, Any]:
    """Load YAML configuration file."""
    try:
        with open(config_file, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Config file not found: {config_file}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML config: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generic overlay filesystem manager for snap/deb packages"
    )
    parser.add_argument(
        "command", choices=["sync", "unsync"], help="Command to execute"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )

    method_group = parser.add_mutually_exclusive_group(required=True)
    method_group.add_argument(
        "--snap", action="store_true", help="Use snap configuration"
    )
    method_group.add_argument(
        "--deb", action="store_true", help="Use deb configuration"
    )

    args = parser.parse_args()

    method = "snap" if args.snap else "deb"

    config = load_config(args.config)
    validate_config(config, method)

    executor = DryRunExecutor() if args.dry_run else RealExecutor()

    if args.command == "sync":
        unmount_overlays(config, method, executor)
        mount_overlays(config, method, executor)
    elif args.command == "unsync":
        unmount_overlays(config, method, executor)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it is valid Python (no import needed)**

Run: `python3 -m py_compile overlay-mount.py`
Expected: exits 0 with no output. (Do **not** run the tool on the host — it imports `yaml`, which the host Python may lack; it runs only inside containers.)

- [ ] **Step 3: Commit**

```bash
git add overlay-mount.py
git commit -m "feat: vendor overlay-mount tool for --overlay action"
```

---

### Task 4: Add `.overlayfs_workdir` to `--sync` `EXCLUDES`

**Files:**
- Modify: `maas-env.py` (`EXCLUDES` constant)
- Modify: `tests/test_maas_env.py` (add `TestExcludes`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestExcludes(unittest.TestCase):
    def test_excludes_overlayfs_workdir(self):
        self.assertIn(".overlayfs_workdir", maas_env.EXCLUDES)

    def test_keeps_sync_excludes(self):
        for pattern in (".git", "__pycache__", "*.pyc"):
            self.assertIn(pattern, maas_env.EXCLUDES)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL on `test_excludes_overlayfs_workdir` with `AssertionError: '.overlayfs_workdir' not found in ['.git', '__pycache__', '*.pyc']`

- [ ] **Step 3: Add `.overlayfs_workdir` to `EXCLUDES`**

In `maas-env.py`, replace:

```python
EXCLUDES = [".git", "__pycache__", "*.pyc"]
```

with:

```python
EXCLUDES = [".git", "__pycache__", "*.pyc", ".overlayfs_workdir"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (24 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "fix: exclude .overlayfs_workdir from --sync --delete"
```

---

### Task 5: `OVERLAY_SCRIPT` constant, `overlay()` action, CLI args, and `main()` wiring

**Files:**
- Modify: `maas-env.py` (add `OVERLAY_SCRIPT`, `overlay()`, CLI args, `main()` branch)

- [ ] **Step 1: Add the `OVERLAY_SCRIPT` constant**

In `maas-env.py`, replace:

```python
EXCLUDES = [".git", "__pycache__", "*.pyc", ".overlayfs_workdir"]
```

with:

```python
EXCLUDES = [".git", "__pycache__", "*.pyc", ".overlayfs_workdir"]

# In-container path to the vendored overlay-mount tool. The repo is bind-mounted
# at /scripts in every container (see lxd-maas-profile.yaml).
OVERLAY_SCRIPT = "/scripts/overlay-mount.py"
```

- [ ] **Step 2: Add the `--overlay`/`--unoverlay` actions and `--overlay-config` option**

In `maas-env.py` `build_parser`, replace:

```python
    action.add_argument(
        "--sync",
        metavar="PATH",
        default=None,
        help="Sync a host MAAS repo (e.g. ~/work/maas) into each container's "
        "codebase dir, then exit. Mutually exclusive with --destroy.",
    )
    parser.add_argument(
        "--sync-dest",
        default="/work",
        help="Destination dir inside each container for --sync (default: /work)",
    )
```

with:

```python
    action.add_argument(
        "--sync",
        metavar="PATH",
        default=None,
        help="Sync a host MAAS repo (e.g. ~/work/maas) into each container's "
        "codebase dir, then exit. Mutually exclusive with --destroy.",
    )
    action.add_argument(
        "--overlay",
        action="store_true",
        help="Overlay-mount /work/src/* onto the MAAS snap paths in each "
        "container, then restart MAAS. Requires --overlay-config.",
    )
    action.add_argument(
        "--unoverlay",
        action="store_true",
        help="Remove the overlay mounts in each container, then restart MAAS. "
        "Requires --overlay-config.",
    )
    parser.add_argument(
        "--sync-dest",
        default="/work",
        help="Destination dir inside each container for --sync (default: /work)",
    )
    parser.add_argument(
        "--overlay-config",
        metavar="PATH",
        default=None,
        help="Path to the overlay config (e.g. overlay-config-37.yaml). Must "
        "live inside this repo. Required for --overlay/--unoverlay.",
    )
```

- [ ] **Step 3: Add the `overlay()` action function**

In `maas-env.py`, insert before `def main() -> None:`:

```python
def overlay(
    containers: list[str],
    subcommand: str,
    config: str,
) -> None:
    """Run the overlay tool (sync/unsync) in each container, then restart MAAS."""
    verb = "Overlaying" if subcommand == "sync" else "Removing overlays from"
    log.info(
        "=== %s %d container(s) (config %s) ===",
        verb,
        len(containers),
        config,
    )
    cmd = build_overlay_command("python3", OVERLAY_SCRIPT, subcommand, config)
    failures: list[str] = []

    for c in containers:
        if not DRY_RUN and not is_container_running(c):
            log.warning("  [overlay] %s is missing or not running — skipping", c)
            failures.append(c)
            continue

        result = lxc_exec(c, cmd, check=False)
        if not DRY_RUN and result.returncode != 0:
            log.warning(
                "  [overlay] %s failed on %s (exit code %d)",
                subcommand,
                c,
                result.returncode,
            )
            failures.append(c)
            continue

        restart = lxc_exec(c, "sudo snap restart maas", check=False)
        if not DRY_RUN and restart.returncode != 0:
            log.warning(
                "  [overlay] snap restart failed on %s (exit code %d)",
                c,
                restart.returncode,
            )
            failures.append(c)
            continue

        log.info("  [overlay] %s done", c)

    if failures:
        log.error(
            "=== Overlay finished with failures: %s ===", ", ".join(failures)
        )
        sys.exit(1)
    log.info("=== Overlay complete ===")


```

- [ ] **Step 4: Wire the overlay branch into `main()`**

In `maas-env.py` `main`, replace:

```python
        sync(containers, source, args.sync_dest)
    else:
        create(
```

with:

```python
        sync(containers, source, args.sync_dest)
    elif args.overlay or args.unoverlay:
        if not args.overlay_config or not args.overlay_config.strip():
            log.error("ERROR: --overlay/--unoverlay require --overlay-config")
            sys.exit(1)
        host_config = os.path.expanduser(args.overlay_config)
        if not os.path.isfile(host_config):
            log.error(
                "ERROR: overlay config not found or not a file: %s",
                args.overlay_config,
            )
            sys.exit(1)
        repo_root = str(Path(__file__).resolve().parent)
        try:
            config_in_container = container_config_path(host_config, repo_root)
        except ValueError as exc:
            log.error("ERROR: %s", exc)
            sys.exit(1)
        overlay(
            containers,
            "sync" if args.overlay else "unsync",
            config_in_container,
        )
    else:
        create(
```

- [ ] **Step 5: Run the full unit suite (no regressions)**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (24 tests)

- [ ] **Step 6: Verify `--help` shows the new flags**

Run: `python3 maas-env.py --help`
Expected: output lists `--overlay`, `--unoverlay`, and `--overlay-config PATH`.

- [ ] **Step 7: Verify action mutual exclusion**

Run: `python3 maas-env.py --name x --overlay --destroy --overlay-config overlay-config-37.yaml`
Expected: argparse error `argument --destroy: not allowed with argument --overlay` (exit code 2).

- [ ] **Step 8: Verify the missing-config guard**

Run: `python3 maas-env.py --name mytest --mode multi --overlay`
Expected: `ERROR: --overlay/--unoverlay require --overlay-config` and exit code 1.

- [ ] **Step 9: Verify the config-not-found guard**

Run: `python3 maas-env.py --name mytest --mode multi --overlay --overlay-config /no/such.yaml`
Expected: `ERROR: overlay config not found or not a file: /no/such.yaml` and exit code 1.

- [ ] **Step 10: Verify the config-outside-repo guard**

Run: `python3 maas-env.py --name mytest --mode multi --overlay --overlay-config /etc/hostname`
Expected: `ERROR: overlay config must live inside the repo (...): /etc/hostname` and exit code 1.

- [ ] **Step 11: Verify dry-run prints overlay + restart commands without LXD**

Run: `python3 maas-env.py --name mytest --mode multi --overlay --overlay-config overlay-config-37.yaml --dry-run`
Expected: for each of `mytest-1`, `mytest-2`, `mytest-3`, two lines:
- `[dry-run] lxc exec mytest-N -- sh -c cd /work && python3 /scripts/overlay-mount.py sync --config /scripts/overlay-config-37.yaml --snap`
- `[dry-run] lxc exec mytest-N -- sh -c sudo snap restart maas`

and exits 0. No real `lxc` calls happen.

- [ ] **Step 12: Verify `--unoverlay` dry-run uses the `unsync` subcommand**

Run: `python3 maas-env.py --name mytest --mode single --unoverlay --overlay-config overlay-config-37.yaml --dry-run`
Expected: a line containing `python3 /scripts/overlay-mount.py unsync --config /scripts/overlay-config-37.yaml --snap` for `mytest`, plus the restart line; exits 0.

- [ ] **Step 13: Commit**

```bash
git add maas-env.py
git commit -m "feat: add --overlay/--unoverlay actions driving overlay-mount"
```

---

### Task 6: Add `python3-yaml` to the LXD profile + final verification

**Files:**
- Modify: `lxd-maas-profile.yaml`

- [ ] **Step 1: Add `python3-yaml` to the cloud-init packages**

In `lxd-maas-profile.yaml`, replace:

```yaml
        packages:
        - git 
        - build-essential
        - jq
        - pgcli
        - rsync
```

with:

```yaml
        packages:
        - git 
        - build-essential
        - jq
        - pgcli
        - rsync
        - python3-yaml
```

- [ ] **Step 2: Verify the profile lists python3-yaml**

Run: `grep -n 'python3-yaml' lxd-maas-profile.yaml`
Expected: a line showing `- python3-yaml` under the packages list.

- [ ] **Step 3: Run the full unit suite one more time**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (24 tests).

- [ ] **Step 4: Commit**

```bash
git add lxd-maas-profile.yaml
git commit -m "feat: preinstall python3-yaml in containers for --overlay"
```

- [ ] **Step 5: Manual end-to-end checklist (requires a live LXD env)**

Perform once against a real environment whose MAAS channel matches the chosen config:

1. Create an env and confirm PyYAML is present: `lxc exec <c> -- python3 -c 'import yaml'` (exit 0).
2. `python3 maas-env.py --name dev --mode multi --sync ~/work/maas` to push code into `/work`.
3. `python3 maas-env.py --name dev --mode multi --overlay --overlay-config overlay-config-37.yaml`; confirm overlays mounted: `lxc exec dev-1 -- sh -c 'mount | grep overlay'` shows the snap site-packages paths.
4. Make a visible code change on the host, re-run `--sync` then `--overlay`; confirm the change is live after MAAS restarts.
5. `python3 maas-env.py --name dev --mode multi --unoverlay --overlay-config overlay-config-37.yaml`; confirm `mount | grep overlay` is empty and MAAS is back to stock.
6. Confirm `--dry-run` for both actions prints commands only and changes nothing.

---

## Self-Review

**Spec coverage:**
- §Background / vendored tool → Task 3 (verbatim vendor + `py_compile`).
- §CLI interface (`--overlay`, `--unoverlay`, `--overlay-config`, `--name`/`--mode` reuse, `--dry-run`, action mutual exclusion, snap-only) → Task 5 (Steps 2, 6, 7, 11, 12).
- §Config path resolution (exists/is-file/inside-repo → `/scripts/<rel>`) → Task 1 (`container_config_path`) + Task 5 Step 4 (`main()` validation) + Steps 8–10.
- §Behavior/flow (validate config, compute containers, per-container running check + overlay + restart, summary + non-zero exit) → Task 5 (`overlay()` + `main()`); reuses `compute_containers`/`is_container_running` from the `--sync` work.
- §Invocation (CWD=/work, `--config`, `--snap`, restart) → Task 2 (`build_overlay_command`) + Task 5 Step 3 (`overlay()` issues `sudo snap restart maas`).
- §Profile change (`python3-yaml`) → Task 6.
- §`--sync` exclude change (`.overlayfs_workdir`) → Task 4.
- §Edge cases (no config, missing/outside-repo config, container not running, tool failure, restart failure, action mutual exclusion, dry-run) → Task 5 (`overlay()` logic + Steps 7–12).
- §Testing (pure-helper unit tests + manual e2e) → Tasks 1, 2, 4 (unit) + Task 6 Step 5 (e2e).

No gaps found.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/vague steps — every code step shows complete code; every run step shows the exact command and expected result. Task 3 reproduces the full vendored file rather than referencing it.

**Type/name consistency:** Helper names/signatures are consistent across tasks and with the consuming code: `container_config_path(host_config, repo_root, mount="/scripts")` (Task 1) is called in `main()` with `(host_config, repo_root)` (Task 5 Step 4); `build_overlay_command(python, script, subcommand, config, workdir="/work")` (Task 2) is called in `overlay()` as `build_overlay_command("python3", OVERLAY_SCRIPT, subcommand, config)` (Task 5 Step 3); `OVERLAY_SCRIPT` is defined once (Task 5 Step 1) before its use; `overlay(containers, subcommand, config)` (Task 5 Step 3) is called with `("sync"|"unsync")` matching the tool's `command` choices and the `build_overlay_command` subcommand; `EXCLUDES` gains `.overlayfs_workdir` (Task 4) and is already consumed by `sync()`. The `--snap` flag and `cd /work` prefix in `build_overlay_command` match the spec's invocation and the tool's required `--snap`/`--config` arguments.

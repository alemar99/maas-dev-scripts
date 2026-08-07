# maas-env.py `--sync` Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `--sync PATH` action to `maas-env.py` that rsyncs a host MAAS checkout into every container's `/work`, using an `lxc exec` transport shim.

**Architecture:** rsync runs on the host and tunnels into each container via `--rsh`, pointing at the script itself (`maas-env.py --rsh-shim <container> ...`), which `os.execvp`s into `lxc exec --user 1000 --group 1000 <container> -- ...`. The command-building and container-list logic are factored into pure functions for unit testing. rsync is guaranteed present in containers by adding it to the LXD profile's cloud-init packages.

**Tech Stack:** Python 3 stdlib (`argparse`, `subprocess`, `os`, `pathlib`), `unittest`, `rsync`, LXD (`lxc`).

**Spec:** `docs/superpowers/specs/2026-06-24-maas-env-sync-design.md`

---

## File Structure

- **Modify `maas-env.py`** — add pure helpers (`compute_containers`, `normalize_source`, `build_rsh_value`, `build_rsync_command`, `build_shim_exec_argv`, `parse_container_running`), the `is_container_running` wrapper, the `sync()` action, the `EXCLUDES` constant, the `--sync`/`--sync-dest` CLI args, the `--rsh-shim` early-exit, and `main()` wiring.
- **Modify `lxd-maas-profile.yaml`** — add `rsync` to the cloud-init `packages:` list.
- **Create `tests/__init__.py`** — make `tests` a package.
- **Create `tests/test_maas_env.py`** — unit tests for the pure helpers (loads the hyphenated script via `importlib`).

**Insertion convention for new functions:** Each new top-level function is inserted immediately *before* `def build_parser() -> argparse.ArgumentParser:`. Because we always insert before that anchor, it remains present for every subsequent task.

**Run tests with:** `python3 -m unittest tests.test_maas_env -v` (from the repo root).

---

### Task 1: Test harness + `compute_containers`

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_maas_env.py`
- Modify: `maas-env.py` (add `compute_containers` before `build_parser`)

- [ ] **Step 1: Create the empty package marker**

Create `tests/__init__.py` with no content (empty file).

- [ ] **Step 2: Write the failing test + harness**

Create `tests/test_maas_env.py`:

```python
import importlib.util
import os
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "maas-env.py"
_spec = importlib.util.spec_from_file_location("maas_env", _MODULE_PATH)
maas_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maas_env)


class TestComputeContainers(unittest.TestCase):
    def test_single_mode_returns_one_container(self):
        self.assertEqual(maas_env.compute_containers("foo", "single"), ["foo"])

    def test_multi_mode_returns_three_numbered_containers(self):
        self.assertEqual(
            maas_env.compute_containers("foo", "multi"),
            ["foo-1", "foo-2", "foo-3"],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'compute_containers'`

- [ ] **Step 4: Implement `compute_containers`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def compute_containers(name: str, mode: str) -> list[str]:
    """Return the container names for a given base name and mode."""
    if mode == "single":
        return [name]
    return [f"{name}-{i}" for i in (1, 2, 3)]


```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/__init__.py tests/test_maas_env.py maas-env.py
git commit -m "test: add harness and compute_containers helper"
```

---

### Task 2: `normalize_source`

**Files:**
- Modify: `maas-env.py` (add `normalize_source` before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestNormalizeSource`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestNormalizeSource(unittest.TestCase):
    def test_adds_single_trailing_slash(self):
        self.assertEqual(maas_env.normalize_source("/tmp/foo"), "/tmp/foo/")

    def test_collapses_existing_trailing_slash(self):
        self.assertEqual(maas_env.normalize_source("/tmp/foo/"), "/tmp/foo/")

    def test_expands_user(self):
        result = maas_env.normalize_source("~/foo")
        self.assertTrue(result.startswith(os.path.expanduser("~")))
        self.assertTrue(result.endswith("/foo/"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'normalize_source'`

- [ ] **Step 3: Implement `normalize_source`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def normalize_source(path: str) -> str:
    """Expand ~ and ensure exactly one trailing slash (rsync 'contents of')."""
    expanded = os.path.expanduser(path)
    return expanded.rstrip("/") + "/"


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add normalize_source helper"
```

---

### Task 3: `build_rsh_value`

**Files:**
- Modify: `maas-env.py` (add `build_rsh_value` before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestBuildRshValue`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestBuildRshValue(unittest.TestCase):
    def test_builds_rsh_string(self):
        self.assertEqual(
            maas_env.build_rsh_value("/usr/bin/python3", "/x/maas-env.py"),
            "/usr/bin/python3 /x/maas-env.py --rsh-shim",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'build_rsh_value'`

- [ ] **Step 3: Implement `build_rsh_value`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def build_rsh_value(python: str, script: str) -> str:
    """Build the rsync --rsh transport string that re-invokes this script."""
    return f"{python} {script} --rsh-shim"


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add build_rsh_value helper"
```

---

### Task 4: `build_rsync_command`

**Files:**
- Modify: `maas-env.py` (add `build_rsync_command` before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestBuildRsyncCommand`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestBuildRsyncCommand(unittest.TestCase):
    def test_builds_expected_argv(self):
        cmd = maas_env.build_rsync_command(
            "/src/",
            "c1",
            "/work",
            "PYBIN /x/maas-env.py --rsh-shim",
            [".git", "__pycache__", "*.pyc"],
        )
        self.assertEqual(cmd[0], "rsync")
        self.assertIn("-a", cmd)
        self.assertIn("--no-owner", cmd)
        self.assertIn("--no-group", cmd)
        self.assertIn("--delete", cmd)
        self.assertEqual(cmd.count("--exclude"), 3)
        self.assertIn(".git", cmd)
        self.assertIn("__pycache__", cmd)
        self.assertIn("*.pyc", cmd)
        e_idx = cmd.index("-e")
        self.assertEqual(cmd[e_idx + 1], "PYBIN /x/maas-env.py --rsh-shim")
        self.assertEqual(cmd[-2], "/src/")
        self.assertEqual(cmd[-1], "c1:/work/")

    def test_dest_trailing_slash_is_normalized(self):
        cmd = maas_env.build_rsync_command("/src/", "c1", "/work/", "RSH", [])
        self.assertEqual(cmd[-1], "c1:/work/")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'build_rsync_command'`

- [ ] **Step 3: Implement `build_rsync_command`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def build_rsync_command(
    source: str,
    container: str,
    dest: str,
    rsh: str,
    excludes: list[str],
) -> list[str]:
    """Build the host-side rsync argv that syncs into a container via the shim."""
    cmd = ["rsync", "-a", "--no-owner", "--no-group", "--delete"]
    for pattern in excludes:
        cmd += ["--exclude", pattern]
    cmd += ["-e", rsh]
    dest_spec = f"{container}:{dest.rstrip('/')}/"
    cmd += [source, dest_spec]
    return cmd


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add build_rsync_command helper"
```

---

### Task 5: `build_shim_exec_argv`

**Files:**
- Modify: `maas-env.py` (add `build_shim_exec_argv` before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestBuildShimExecArgv`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestBuildShimExecArgv(unittest.TestCase):
    def test_builds_lxc_exec_argv(self):
        self.assertEqual(
            maas_env.build_shim_exec_argv(["c1", "rsync", "--server", "x"]),
            [
                "lxc", "exec", "--user", "1000", "--group", "1000",
                "c1", "--", "rsync", "--server", "x",
            ],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'build_shim_exec_argv'`

- [ ] **Step 3: Implement `build_shim_exec_argv`**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def build_shim_exec_argv(shim_args: list[str]) -> list[str]:
    """Translate rsync's '<container> <remote-cmd...>' into an lxc exec argv.

    Runs the container-side command as uid/gid 1000 (ubuntu) so synced files
    keep the ownership maas-install.sh sets on /work.
    """
    container = shim_args[0]
    remote_cmd = shim_args[1:]
    return [
        "lxc", "exec", "--user", "1000", "--group", "1000",
        container, "--", *remote_cmd,
    ]


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add build_shim_exec_argv helper"
```

---

### Task 6: `parse_container_running` + `is_container_running`

**Files:**
- Modify: `maas-env.py` (add both functions before `build_parser`)
- Modify: `tests/test_maas_env.py` (add `TestParseContainerRunning`)

- [ ] **Step 1: Write the failing test**

In `tests/test_maas_env.py`, replace:

```python
if __name__ == "__main__":
    unittest.main()
```

with:

```python
class TestParseContainerRunning(unittest.TestCase):
    def test_running(self):
        self.assertTrue(
            maas_env.parse_container_running("Name: c1\nStatus: RUNNING\n")
        )

    def test_stopped(self):
        self.assertFalse(
            maas_env.parse_container_running("Name: c1\nStatus: STOPPED\n")
        )

    def test_case_insensitive(self):
        self.assertTrue(maas_env.parse_container_running("Status: Running"))

    def test_no_status_line(self):
        self.assertFalse(maas_env.parse_container_running("Name: c1\n"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'parse_container_running'`

- [ ] **Step 3: Implement both functions**

In `maas-env.py`, insert before `def build_parser() -> argparse.ArgumentParser:`:

```python
def parse_container_running(lxc_info_stdout: str) -> bool:
    """Return True if `lxc info` output reports the container as RUNNING."""
    for line in lxc_info_stdout.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("status:"):
            return "running" in stripped.lower()
    return False


def is_container_running(container: str) -> bool:
    """Check whether a container exists and is running (via `lxc info`)."""
    result = subprocess.run(
        ["lxc", "info", container],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    return parse_container_running(result.stdout)


```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add container running-state detection"
```

---

### Task 7: Wire CLI args, shim early-exit, `sync()` action, and `main()`

**Files:**
- Modify: `maas-env.py` (`build_parser`, add `EXCLUDES`, add `sync()`, rewrite `main()`)

- [ ] **Step 1: Add `--sync`/`--sync-dest` args and make `--destroy`/`--sync` mutually exclusive**

In `maas-env.py` `build_parser`, replace:

```python
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="Destroy containers and network instead of creating",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without doing it",
    )
    return parser
```

with:

```python
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--destroy",
        action="store_true",
        help="Destroy containers and network instead of creating",
    )
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without doing it",
    )
    return parser
```

- [ ] **Step 2: Add the `EXCLUDES` constant**

In `maas-env.py`, replace:

```python
DRY_RUN = False
```

with:

```python
DRY_RUN = False

# Paths excluded from --sync. With --delete, excluded paths are preserved in
# the container (not deleted), so each container's own .git survives.
EXCLUDES = [".git", "__pycache__", "*.pyc"]
```

- [ ] **Step 3: Add the `sync()` action function**

In `maas-env.py`, insert before `def main() -> None:`:

```python
def sync(
    containers: list[str],
    source: str,
    dest: str,
) -> None:
    """Rsync a host source dir into each container's dest dir via the shim."""
    log.info(
        "=== Syncing %s -> %d container(s) at %s ===",
        source,
        len(containers),
        dest,
    )
    rsh = build_rsh_value(sys.executable, str(Path(__file__).resolve()))
    failures: list[str] = []

    for c in containers:
        if not DRY_RUN and not is_container_running(c):
            log.warning("  [sync] %s is missing or not running — skipping", c)
            failures.append(c)
            continue

        cmd = build_rsync_command(source, c, dest, rsh, EXCLUDES)
        result = run(cmd, check=False)
        if not DRY_RUN and result.returncode != 0:
            log.warning(
                "  [sync] rsync to %s failed (exit code %d)", c, result.returncode
            )
            failures.append(c)
        else:
            log.info("  [sync] %s done", c)

    if failures:
        log.error("=== Sync finished with failures: %s ===", ", ".join(failures))
        sys.exit(1)
    log.info("=== Sync complete ===")


```

- [ ] **Step 4: Rewrite `main()` (shim early-exit + sync branch + `compute_containers`)**

In `maas-env.py`, replace the entire `main` function:

```python
def main() -> None:
    global DRY_RUN
    parser = build_parser()
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    containers = (
        [args.name]
        if args.mode == "single"
        else [f"{args.name}-{i}" for i in (1, 2, 3)]
    )

    if args.destroy:
        destroy(args.name, args.mode, containers)
    else:
        create(
            name=args.name,
            mode=args.mode,
            containers=containers,
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
            maas_channel=args.maas_channel,
        )
```

with:

```python
def main() -> None:
    global DRY_RUN

    # rsync transport shim: when invoked as the rsync --rsh, re-exec into
    # `lxc exec`. This must run before argparse, which would reject these args.
    if len(sys.argv) >= 2 and sys.argv[1] == "--rsh-shim":
        argv = build_shim_exec_argv(sys.argv[2:])
        os.execvp(argv[0], argv)
        return  # unreachable: execvp replaces the process image

    parser = build_parser()
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    containers = compute_containers(args.name, args.mode)

    if args.destroy:
        destroy(args.name, args.mode, containers)
    elif args.sync is not None:
        source = normalize_source(args.sync)
        if not os.path.isdir(source):
            log.error(
                "ERROR: sync source not found or not a directory: %s", args.sync
            )
            sys.exit(1)
        sync(containers, source, args.sync_dest)
    else:
        create(
            name=args.name,
            mode=args.mode,
            containers=containers,
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
            maas_channel=args.maas_channel,
        )
```

- [ ] **Step 5: Verify the full unit suite still passes**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (13 tests)

- [ ] **Step 6: Verify `--help` shows the new flags**

Run: `./maas-env.py --help`
Expected: output lists `--sync PATH` and `--sync-dest SYNC_DEST`.

- [ ] **Step 7: Verify mutual exclusion**

Run: `./maas-env.py --name x --sync . --destroy`
Expected: argparse error `argument --sync: not allowed with argument --destroy` (exit code 2).

- [ ] **Step 8: Verify dry-run prints rsync commands without needing LXD**

Run: `./maas-env.py --name mytest --mode multi --sync . --dry-run`
Expected: prints three `[dry-run] rsync -a --no-owner --no-group --delete --exclude .git --exclude __pycache__ --exclude *.pyc -e <python> <abs path>/maas-env.py --rsh-shim <source>/ mytest-1:/work/` lines (one per container), and exits 0. No `lxc` calls happen.

- [ ] **Step 9: Verify source validation**

Run: `./maas-env.py --name mytest --mode multi --sync /no/such/path`
Expected: `ERROR: sync source not found or not a directory: /no/such/path` and exit code 1.

- [ ] **Step 10: Commit**

```bash
git add maas-env.py
git commit -m "feat: add --sync action to push host code into containers"
```

---

### Task 8: Add `rsync` to the LXD profile + final verification

**Files:**
- Modify: `lxd-maas-profile.yaml`

- [ ] **Step 1: Add `rsync` to the cloud-init packages**

In `lxd-maas-profile.yaml`, replace:

```yaml
        packages:
        - git 
        - build-essential
        - jq
        - pgcli
```

with:

```yaml
        packages:
        - git 
        - build-essential
        - jq
        - pgcli
        - rsync
```

- [ ] **Step 2: Verify the profile lists rsync**

Run: `grep -n 'rsync' lxd-maas-profile.yaml`
Expected: a line showing `- rsync` under the packages list.

- [ ] **Step 3: Run the full unit suite one more time**

Run: `python3 -m unittest tests.test_maas_env -v`
Expected: PASS (13 tests).

- [ ] **Step 4: Commit**

```bash
git add lxd-maas-profile.yaml
git commit -m "feat: preinstall rsync in containers for --sync"
```

- [ ] **Step 5: Manual end-to-end checklist (requires a live LXD env)**

Perform once against a real multi-node environment:

1. Create a multi env (`./maas-env.py --name dev --mode multi ...`) and confirm each container has rsync: `lxc exec dev-1 -- which rsync`.
2. Edit a file under your host `~/work/maas`, run `./maas-env.py --name dev --mode multi --sync ~/work/maas`, and verify the change landed in all three `/work` copies (`lxc exec dev-2 -- cat /work/<edited-file>`).
3. Delete a file on the host, re-run `--sync`, and verify it is removed in the containers.
4. Verify each container's `/work/.git` still exists after sync (`lxc exec dev-3 -- ls -d /work/.git`).
5. Verify synced files are owned by `ubuntu:ubuntu` (`lxc exec dev-1 -- stat -c '%U:%G' /work/<edited-file>`).
6. Confirm `--dry-run` prints commands only and changes nothing.

---

## Self-Review

**Spec coverage:**
- §CLI interface (`--sync`, `--sync-dest`, `--name`/`--mode` reuse, `--dry-run`, mutual exclusion, fixed excludes) → Task 7 (Steps 1–2) + Task 4/Task 7 EXCLUDES.
- §Behavior/flow (validate source, compute containers, per-container running check + rsync, summary + non-zero exit) → Tasks 1, 6, 7 (`sync()`).
- §Transport (self-shim, uid/gid 1000, no runtime install) → Tasks 5, 7 (Step 4 early-exit).
- §rsync options (`-a --no-owner --no-group --delete`, excludes, trailing slashes) → Task 4.
- §Profile change (rsync in packages) → Task 8.
- §Edge cases (missing source, container not running, rsync failure, mutual exclusion, dry-run) → Task 7 Steps 7–9 + `sync()` logic.
- §Testing (pure-function refactor + unit tests + manual e2e) → Tasks 1–6 + Task 8 Step 5.

No gaps found.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/vague steps — every code step shows complete code; every run step shows the exact command and expected result.

**Type/name consistency:** Function names and signatures are consistent across tasks: `compute_containers(name, mode)`, `normalize_source(path)`, `build_rsh_value(python, script)`, `build_rsync_command(source, container, dest, rsh, excludes)`, `build_shim_exec_argv(shim_args)`, `parse_container_running(stdout)`, `is_container_running(container)`, `sync(containers, source, dest)`. `EXCLUDES` defined once (Task 7 Step 2) and consumed in `sync()`. The shim string built in `build_rsh_value` (`... --rsh-shim`) matches the `sys.argv[1] == "--rsh-shim"` check and `build_shim_exec_argv` consumer.

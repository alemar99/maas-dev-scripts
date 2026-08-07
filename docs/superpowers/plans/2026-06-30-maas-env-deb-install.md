# `--deb` install option Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--deb` install path to `maas-env.py` that installs MAAS from a PPA-provided `.deb` (single mode), alongside the existing snap path, and lets `--overlay`/`--unoverlay` target the `deb:` config section.

**Architecture:** Thread a `method` ("snap"/"deb") through the create and overlay flows. `maas-install.sh` gains a leading method argument and branches. New pure helpers (`build_install_invocation`, two validators) keep the logic unit-testable. The deb package provisions its own local PostgreSQL, so the deb create flow skips `setup_postgres`.

**Tech Stack:** Python 3 (stdlib `argparse`, `unittest`), Bash, LXD, cloud-init.

**Spec:** `docs/superpowers/specs/2026-06-30-maas-env-deb-install-design.md`

**Note on commits:** The user prefers to leave work uncommitted for now. Commit steps are included for completeness but may be skipped or batched at the user's discretion.

**Test command (run from repo root):** `python3 -m unittest discover -s tests -v`

---

## File structure

- **`maas-env.py`** (modify) — new CLI args, two pure validators, `build_install_invocation`, `INSTALL_SCRIPT` constant, `node_ip` helper, `method` param on `build_overlay_command`, `build_restart_command` helper, refactored `install_maas`, deb branch in `create`, `method` + method-aware restart threaded through `overlay`/`main`.
- **`maas-install.sh`** (modify) — dispatch on leading `snap`/`deb` method arg; shared clone; deb steps.
- **`lxd-maas-profile.yaml`** (modify) — add `software-properties-common` so `add-apt-repository` exists in containers.
- **`tests/test_maas_env.py`** (modify) — update `TestBuildOverlayCommand`; add `TestBuildRestartCommand`, `TestBuildInstallInvocation`, `TestValidateDebFlags`, `TestValidateDebCreateArgs`, `TestBuildParserDebArgs`.

---

## Task 1: `build_install_invocation` pure helper

**Files:**
- Modify: `maas-env.py` (add `INSTALL_SCRIPT` constant near `OVERLAY_SCRIPT`; add function near `build_overlay_command`)
- Test: `tests/test_maas_env.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_maas_env.py` (before `if __name__ == "__main__":`):

```python
class TestBuildInstallInvocation(unittest.TestCase):
    def test_snap_form(self):
        self.assertEqual(
            maas_env.build_install_invocation(
                "snap", db_ip="10.0.0.5", channel="3.7/edge"
            ),
            "/scripts/maas-install.sh snap 10.0.0.5 3.7/edge",
        )

    def test_deb_form(self):
        self.assertEqual(
            maas_env.build_install_invocation(
                "deb", ppa="ppa:maas/3.7", branch="3.7"
            ),
            "/scripts/maas-install.sh deb ppa:maas/3.7 3.7",
        )

    def test_unknown_method_raises(self):
        with self.assertRaises(ValueError):
            maas_env.build_install_invocation("flatpak")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_maas_env.TestBuildInstallInvocation -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'build_install_invocation'`

- [ ] **Step 3: Add the constant and function**

In `maas-env.py`, find the constant block (currently):

```python
# In-container path to the vendored overlay-mount tool. The repo is bind-mounted
# at /scripts in every container (see lxd-maas-profile.yaml).
OVERLAY_SCRIPT = "/scripts/overlay-mount.py"
```

Add directly below it:

```python
# In-container path to the install script (bind-mounted at /scripts).
INSTALL_SCRIPT = "/scripts/maas-install.sh"
```

Then add the function **immediately after the constants block** (right after the
`INSTALL_SCRIPT` line, before the `def _echo_dry` that follows). It must come
*after* `INSTALL_SCRIPT` because its `script: str = INSTALL_SCRIPT` default is
evaluated at function-definition time — placing it earlier in the file (e.g. up
by `build_overlay_command`) would raise `NameError` at import:

```python
def build_install_invocation(
    method: str,
    *,
    db_ip: str | None = None,
    channel: str | None = None,
    ppa: str | None = None,
    branch: str | None = None,
    script: str = INSTALL_SCRIPT,
) -> str:
    """Build the in-container command that runs maas-install.sh for a method.

    snap: '<script> snap <db_ip> <channel>'
    deb:  '<script> deb <ppa> <branch>'
    """
    if method == "snap":
        return f"{script} snap {db_ip} {channel}"
    if method == "deb":
        return f"{script} deb {ppa} {branch}"
    raise ValueError(f"unknown install method: {method}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_maas_env.TestBuildInstallInvocation -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add build_install_invocation helper"
```

---

## Task 2: deb argument validators

**Files:**
- Modify: `maas-env.py` (add two functions after `build_install_invocation`)
- Test: `tests/test_maas_env.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_maas_env.py`:

```python
class TestValidateDebFlags(unittest.TestCase):
    def test_ppa_without_deb_is_error(self):
        self.assertIsNotNone(
            maas_env.validate_deb_flags(False, "ppa:maas/3.7", None)
        )

    def test_branch_without_deb_is_error(self):
        self.assertIsNotNone(
            maas_env.validate_deb_flags(False, None, "3.7")
        )

    def test_neither_without_deb_is_ok(self):
        self.assertIsNone(maas_env.validate_deb_flags(False, None, None))

    def test_deb_alone_is_ok(self):
        self.assertIsNone(maas_env.validate_deb_flags(True, None, None))


class TestValidateDebCreateArgs(unittest.TestCase):
    def test_deb_multi_is_error(self):
        self.assertIsNotNone(
            maas_env.validate_deb_create_args(
                True, "multi", "ppa:maas/3.7", "3.7"
            )
        )

    def test_deb_without_ppa_is_error(self):
        self.assertIsNotNone(
            maas_env.validate_deb_create_args(True, "single", None, "3.7")
        )

    def test_deb_without_branch_is_error(self):
        self.assertIsNotNone(
            maas_env.validate_deb_create_args(
                True, "single", "ppa:maas/3.7", None
            )
        )

    def test_valid_deb_args_ok(self):
        self.assertIsNone(
            maas_env.validate_deb_create_args(
                True, "single", "ppa:maas/3.7", "3.7"
            )
        )

    def test_plain_snap_ok(self):
        self.assertIsNone(
            maas_env.validate_deb_create_args(False, "multi", None, None)
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_maas_env.TestValidateDebFlags tests.test_maas_env.TestValidateDebCreateArgs -v`
Expected: FAIL with `AttributeError: module 'maas_env' has no attribute 'validate_deb_flags'`

- [ ] **Step 3: Add the validators**

In `maas-env.py`, add immediately after `build_install_invocation`:

```python
def validate_deb_flags(
    deb: bool, ppa: str | None, branch: str | None
) -> str | None:
    """Global rule: --ppa/--branch are deb-only. Return an error message or None."""
    if not deb and (ppa or branch):
        return "--ppa/--branch require --deb"
    return None


def validate_deb_create_args(
    deb: bool, mode: str, ppa: str | None, branch: str | None
) -> str | None:
    """Create-action rules for --deb. Return an error message or None."""
    if not deb:
        return None
    if mode == "multi":
        return "--deb is only supported with --mode single"
    if not ppa or not branch:
        return "--deb requires both --ppa and --branch"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_maas_env.TestValidateDebFlags tests.test_maas_env.TestValidateDebCreateArgs -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add deb argument validators"
```

---

## Task 3: `method` parameter on `build_overlay_command` + `build_restart_command`

**Files:**
- Modify: `maas-env.py:147-162` (`build_overlay_command`); add `build_restart_command` after it
- Test: `tests/test_maas_env.py:142-165` (`TestBuildOverlayCommand`)

- [ ] **Step 1: Update tests to assert method + restart (failing)**

Replace the existing `TestBuildOverlayCommand` class in `tests/test_maas_env.py` with:

```python
class TestBuildOverlayCommand(unittest.TestCase):
    def test_sync_command_snap(self):
        self.assertEqual(
            maas_env.build_overlay_command(
                "python3",
                "/scripts/overlay-mount.py",
                "sync",
                "/scripts/overlay-config-37.yaml",
                "snap",
            ),
            "cd /work && python3 /scripts/overlay-mount.py sync "
            "--config /scripts/overlay-config-37.yaml --snap",
        )

    def test_unsync_command_snap(self):
        self.assertEqual(
            maas_env.build_overlay_command(
                "python3",
                "/scripts/overlay-mount.py",
                "unsync",
                "/scripts/overlay-config-master.yaml",
                "snap",
            ),
            "cd /work && python3 /scripts/overlay-mount.py unsync "
            "--config /scripts/overlay-config-master.yaml --snap",
        )

    def test_sync_command_deb(self):
        self.assertEqual(
            maas_env.build_overlay_command(
                "python3",
                "/scripts/overlay-mount.py",
                "sync",
                "/scripts/overlay-config-37.yaml",
                "deb",
            ),
            "cd /work && python3 /scripts/overlay-mount.py sync "
            "--config /scripts/overlay-config-37.yaml --deb",
        )

    def test_defaults_to_snap(self):
        cmd = maas_env.build_overlay_command(
            "python3", "/scripts/overlay-mount.py", "sync", "/cfg.yaml"
        )
        self.assertTrue(cmd.endswith("--snap"))


class TestBuildRestartCommand(unittest.TestCase):
    def test_snap(self):
        self.assertEqual(
            maas_env.build_restart_command("snap"), "sudo snap restart maas"
        )

    def test_deb(self):
        self.assertEqual(
            maas_env.build_restart_command("deb"),
            "sudo systemctl restart 'maas-*'",
        )

    def test_unknown_defaults_to_snap(self):
        self.assertEqual(
            maas_env.build_restart_command("other"), "sudo snap restart maas"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_maas_env.TestBuildOverlayCommand tests.test_maas_env.TestBuildRestartCommand -v`
Expected: FAIL — `test_sync_command_deb` ends with `--snap` (method ignored), and `TestBuildRestartCommand` fails with `AttributeError: module 'maas_env' has no attribute 'build_restart_command'`.

- [ ] **Step 3: Add the `method` parameter and the restart helper**

In `maas-env.py`, replace `build_overlay_command` (lines 147-162) with:

```python
def build_overlay_command(
    python: str,
    script: str,
    subcommand: str,
    config: str,
    method: str = "snap",
    workdir: str = "/work",
) -> str:
    """Build the in-container shell command that runs the overlay tool.

    Runs from `workdir` (must be on the container rootfs) so the tool's relative
    `.overlayfs_workdir` shares a filesystem with the `/work/src/...` upperdirs.
    `method` selects the package section: '--snap' (default) or '--deb'.
    """
    flag = "--deb" if method == "deb" else "--snap"
    return (
        f"cd {workdir} && {python} {script} {subcommand} "
        f"--config {config} {flag}"
    )


def build_restart_command(method: str) -> str:
    """Command to restart MAAS after (un)overlaying, per install method."""
    if method == "deb":
        return "sudo systemctl restart 'maas-*'"
    return "sudo snap restart maas"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_maas_env.TestBuildOverlayCommand tests.test_maas_env.TestBuildRestartCommand -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: thread method into overlay command and restart helper"
```

---

## Task 4: `--deb`, `--ppa`, `--branch` CLI arguments

**Files:**
- Modify: `maas-env.py` (`build_parser`, after the `--maas-channel` argument)
- Test: `tests/test_maas_env.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_maas_env.py`:

```python
class TestBuildParserDebArgs(unittest.TestCase):
    def test_deb_args_parse(self):
        parser = maas_env.build_parser()
        args = parser.parse_args(
            ["--name", "dev", "--deb", "--ppa", "ppa:maas/3.7", "--branch", "3.7"]
        )
        self.assertTrue(args.deb)
        self.assertEqual(args.ppa, "ppa:maas/3.7")
        self.assertEqual(args.branch, "3.7")

    def test_deb_defaults_off(self):
        parser = maas_env.build_parser()
        args = parser.parse_args(["--name", "dev"])
        self.assertFalse(args.deb)
        self.assertIsNone(args.ppa)
        self.assertIsNone(args.branch)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_maas_env.TestBuildParserDebArgs -v`
Expected: FAIL with `AttributeError: 'Namespace' object has no attribute 'deb'`

- [ ] **Step 3: Add the arguments**

In `maas-env.py`, find the `--maas-channel` argument block in `build_parser`:

```python
    parser.add_argument(
        "--maas-channel",
        default="latest/edge",
        help="MAAS snap channel to install (default: latest/edge)",
    )
```

Add directly after it:

```python
    parser.add_argument(
        "--deb",
        action="store_true",
        help="Install MAAS from a deb/PPA instead of the snap. Single mode "
        "only; requires --ppa and --branch.",
    )
    parser.add_argument(
        "--ppa",
        default=None,
        help="PPA to install the MAAS deb from (e.g. ppa:maas/3.7). "
        "Required with --deb.",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="MAAS git branch cloned into /work for the deb path. "
        "Required with --deb.",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_maas_env.TestBuildParserDebArgs -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add maas-env.py tests/test_maas_env.py
git commit -m "feat: add --deb/--ppa/--branch CLI arguments"
```

---

## Task 5: Refactor `install_maas` to use the method helper

**Files:**
- Modify: `maas-env.py:492-496` (`install_maas`) and its call site in `create` (`maas-env.py:552`)

This task has no new unit test (the function calls `lxc_exec`); verify with the full suite (no regression) and keep snap behavior identical.

- [ ] **Step 1: Replace `install_maas`**

In `maas-env.py`, replace `install_maas` (lines 492-496) with:

```python
def install_maas(
    containers: list[str],
    method: str,
    *,
    db_ip: str | None = None,
    channel: str | None = None,
    ppa: str | None = None,
    branch: str | None = None,
) -> None:
    """Install MAAS on all nodes via maas-install.sh for the given method."""
    log.info("[%s] Installing and initializing MAAS on all nodes", method)
    cmd = build_install_invocation(
        method, db_ip=db_ip, channel=channel, ppa=ppa, branch=branch
    )
    for c in containers:
        lxc_exec(c, cmd)
```

- [ ] **Step 2: Update the call site in `create`**

In `maas-env.py`, find this line inside `create` (currently line 552):

```python
    install_maas(containers, db_ip, maas_channel)
```

Replace it with:

```python
    install_maas(containers, "snap", db_ip=db_ip, channel=maas_channel)
```

- [ ] **Step 3: Run the full suite to verify no regression**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (all tests; count is now 24 + the 19 added in Tasks 1-4 = 43)

- [ ] **Step 4: Sanity-check the snap create path with --dry-run**

Run: `python3 maas-env.py --name dev --dry-run`
Expected: Output includes a line containing `maas-install.sh snap` (proves the snap invocation is built correctly). No errors.

- [ ] **Step 5: Commit**

```bash
git add maas-env.py
git commit -m "refactor: install_maas builds invocation via method helper"
```

---

## Task 6: deb branch in `create` (+ `node_ip` helper)

**Files:**
- Modify: `maas-env.py` (add `node_ip` helper near `setup_postgres`; change `create` signature and body, lines 513-560)

- [ ] **Step 1: Add the `node_ip` helper**

In `maas-env.py`, add directly above `setup_postgres` (line 483):

```python
def node_ip(container: str) -> str:
    """Return the container's primary IP address."""
    return lxc_exec_capture(container, "hostname -I | cut -d' ' -f1")
```

- [ ] **Step 2: Update the `create` signature**

In `maas-env.py`, replace the `create` signature (lines 513-522):

```python
def create(
    name: str,
    mode: str,
    containers: list[str],
    ubuntu: str,
    profile: str,
    pre_scripts: list[ScriptTarget],
    post_scripts: list[ScriptTarget],
    maas_channel: str,
) -> None:
```

with (adds `deb`, `ppa`, `branch` with defaults so existing callers keep working):

```python
def create(
    name: str,
    mode: str,
    containers: list[str],
    ubuntu: str,
    profile: str,
    pre_scripts: list[ScriptTarget],
    post_scripts: list[ScriptTarget],
    maas_channel: str,
    deb: bool = False,
    ppa: str | None = None,
    branch: str | None = None,
) -> None:
```

- [ ] **Step 3: Update the install/summary section of `create`**

In `maas-env.py`, find this block inside `create` (currently lines 548-560):

```python
    run_scripts(containers, pre_scripts, "pre-install", abort_on_failure=True)

    db_ip = setup_postgres(containers[0])

    install_maas(containers, "snap", db_ip=db_ip, channel=maas_channel)
    create_admin(containers[0])

    run_scripts(containers, post_scripts, "post-install", abort_on_failure=False)

    log.info("=== MAAS environment '%s' ready ===", name)
    log.info("  Containers: %s", ", ".join(containers))
    log.info("  MAAS URL:   http://%s:5240/MAAS", db_ip)
    log.info("  Admin:      maas / maas")
```

Replace it with:

```python
    run_scripts(containers, pre_scripts, "pre-install", abort_on_failure=True)

    if deb:
        # The deb package provisions its own local PostgreSQL; no separate DB.
        install_maas(containers, "deb", ppa=ppa, branch=branch)
        maas_ip = node_ip(containers[0])
    else:
        db_ip = setup_postgres(containers[0])
        install_maas(containers, "snap", db_ip=db_ip, channel=maas_channel)
        maas_ip = db_ip

    create_admin(containers[0])

    run_scripts(containers, post_scripts, "post-install", abort_on_failure=False)

    log.info("=== MAAS environment '%s' ready ===", name)
    log.info("  Containers: %s", ", ".join(containers))
    log.info("  MAAS URL:   http://%s:5240/MAAS", maas_ip)
    log.info("  Admin:      maas / maas")
```

- [ ] **Step 4: Run the full suite to verify no regression**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (43 tests).

- [ ] **Step 5: Commit**

```bash
git add maas-env.py
git commit -m "feat: deb install branch in create()"
```

---

## Task 7: Thread method + validation through `overlay` and `main`

**Files:**
- Modify: `maas-env.py` (`overlay` signature/body, lines 617-667; `main`, lines 670-737)

- [ ] **Step 1: Add a `method` parameter to `overlay`**

In `maas-env.py`, replace the `overlay` signature (lines 617-621):

```python
def overlay(
    containers: list[str],
    subcommand: str,
    config: str,
) -> None:
```

with:

```python
def overlay(
    containers: list[str],
    subcommand: str,
    config: str,
    method: str = "snap",
) -> None:
```

Then in `overlay`'s body, find (currently line 630):

```python
    cmd = build_overlay_command("python3", OVERLAY_SCRIPT, subcommand, config)
```

and replace it with:

```python
    cmd = build_overlay_command(
        "python3", OVERLAY_SCRIPT, subcommand, config, method
    )
```

Then, still in `overlay`'s body, find the restart block (currently lines 650-657):

```python
        restart = lxc_exec(c, "sudo snap restart maas", check=False)
        if not DRY_RUN and restart.returncode != 0:
            log.warning(
                "  [overlay] snap restart failed on %s (exit code %d)",
                c,
                restart.returncode,
            )
```

and replace it with (uses the method-aware restart command; warning is now method-neutral):

```python
        restart = lxc_exec(c, build_restart_command(method), check=False)
        if not DRY_RUN and restart.returncode != 0:
            log.warning(
                "  [overlay] MAAS restart failed on %s (exit code %d)",
                c,
                restart.returncode,
            )
```

- [ ] **Step 2: Add global validation in `main`**

In `maas-env.py`, find this block in `main` (currently lines 681-684):

```python
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    containers = compute_containers(args.name, args.mode)
```

Replace it with:

```python
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    flag_err = validate_deb_flags(args.deb, args.ppa, args.branch)
    if flag_err:
        log.error("ERROR: %s", flag_err)
        sys.exit(1)

    containers = compute_containers(args.name, args.mode)
```

- [ ] **Step 3: Pass method into the overlay dispatch**

In `maas-env.py`, find the overlay call in `main` (currently lines 722-726):

```python
        overlay(
            containers,
            "sync" if args.overlay else "unsync",
            config_in_container,
        )
```

Replace it with:

```python
        overlay(
            containers,
            "sync" if args.overlay else "unsync",
            config_in_container,
            "deb" if args.deb else "snap",
        )
```

- [ ] **Step 4: Add create-action validation and pass deb args**

In `maas-env.py`, find the final `else` branch of `main` (currently lines 727-737):

```python
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

Replace it with:

```python
    else:
        create_err = validate_deb_create_args(
            args.deb, args.mode, args.ppa, args.branch
        )
        if create_err:
            log.error("ERROR: %s", create_err)
            sys.exit(1)
        create(
            name=args.name,
            mode=args.mode,
            containers=containers,
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
            maas_channel=args.maas_channel,
            deb=args.deb,
            ppa=args.ppa,
            branch=args.branch,
        )
```

- [ ] **Step 5: Run the full suite to verify no regression**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (43 tests).

- [ ] **Step 6: Verify validation and deb dry-run behavior end to end**

Run each and confirm the expected result:

```bash
# deb + multi rejected
python3 maas-env.py --name dev --deb --ppa ppa:maas/3.7 --branch 3.7 --mode multi
# Expected: prints "ERROR: --deb is only supported with --mode single", exit 1

# deb without ppa rejected
python3 maas-env.py --name dev --deb --branch 3.7
# Expected: prints "ERROR: --deb requires both --ppa and --branch", exit 1

# ppa without deb rejected
python3 maas-env.py --name dev --ppa ppa:maas/3.7
# Expected: prints "ERROR: --ppa/--branch require --deb", exit 1

# valid deb dry-run
python3 maas-env.py --name dev --deb --ppa ppa:maas/3.7 --branch 3.7 --dry-run
# Expected: output contains "maas-install.sh deb ppa:maas/3.7 3.7"; no setup_postgres/snap lines; no error

# valid deb overlay dry-run
python3 maas-env.py --name dev --deb --overlay --overlay-config overlay-config-37.yaml --dry-run
# Expected: output contains "overlay-mount.py sync --config /scripts/overlay-config-37.yaml --deb"
#           and the restart line "sudo systemctl restart 'maas-*'" (not "snap restart maas")
```

- [ ] **Step 7: Commit**

```bash
git add maas-env.py
git commit -m "feat: thread deb method and validation through overlay/main"
```

---

## Task 8: Restructure `maas-install.sh` for method dispatch

**Files:**
- Modify: `maas-install.sh` (entire file)

- [ ] **Step 1: Replace the script**

Replace the entire contents of `maas-install.sh` with:

```bash
#!/bin/bash
#
# Install MAAS (snap or deb) and initialize region+rack.
# Usage:
#   /scripts/maas-install.sh snap <DB_IP> <MAAS_CHANNEL>
#   /scripts/maas-install.sh deb  <PPA> <BRANCH>
# This is mounted at /scripts inside containers via the LXD profile.

METHOD="$1"

clone_maas() {
    # $1 = git branch to clone into /work
    sudo git clone --branch "$1" --depth 10 --shallow-submodules \
        https://github.com/canonical/maas /work
    sudo chown -R ubuntu:ubuntu /work
}

case "$METHOD" in
    snap)
        DB_IP="$2"
        MAAS_CHANNEL="$3"
        HOST_IP="$(hostname -I | cut -d' ' -f1)"

        TRACK="${MAAS_CHANNEL%%/*}"
        if [ "$TRACK" = "latest" ]; then
            BRANCH="master"
        else
            BRANCH="$TRACK"
        fi

        clone_maas "$BRANCH"

        sudo snap install --beta snapd
        sudo snap install --beta core26
        sudo snap install maas --channel="$MAAS_CHANNEL"
        /work/utilities/connect-snap-interfaces

        sudo maas init region+rack \
            --database-uri="postgres://maas:maas@${DB_IP}/maasdb" \
            --maas-url="http://$HOST_IP:5240/MAAS"
        ;;
    deb)
        PPA="$2"
        BRANCH="$3"

        clone_maas "$BRANCH"

        sudo add-apt-repository -y "$PPA"
        sudo apt update
        sudo apt install -y maas
        sudo maas init --skip-admin --candid-domain '' --rbac-url ''
        ;;
    *)
        echo "Usage: $0 snap <DB_IP> <MAAS_CHANNEL> | deb <PPA> <BRANCH>" >&2
        exit 1
        ;;
esac
```

- [ ] **Step 2: Syntax-check the script**

Run: `bash -n maas-install.sh`
Expected: no output, exit 0 (valid bash syntax).

- [ ] **Step 3: Verify the dispatch arms with a stubbed run**

Run: `bash -c 'set -- ; source /dev/stdin <<"EOF"
METHOD="other"
case "$METHOD" in
  snap) echo snap;; deb) echo deb;; *) echo "usage"; exit 1;; esac
EOF'`
Expected: prints `usage` and exits 1 — confirms the fall-through arm works. (This mirrors the script's `case`.)

- [ ] **Step 4: Commit**

```bash
git add maas-install.sh
git commit -m "feat: dispatch maas-install.sh on snap/deb method arg"
```

---

## Task 9: Add `software-properties-common` to the LXD profile

`add-apt-repository` (used by the deb path) is provided by `software-properties-common`, which is not present on minimal Ubuntu images. Add it to the profile's cloud-init packages so it exists before `maas-install.sh deb` runs. (Mirrors how `rsync` and `python3-yaml` were added for earlier features.)

**Files:**
- Modify: `lxd-maas-profile.yaml:5-11`

- [ ] **Step 1: Add the package**

In `lxd-maas-profile.yaml`, find:

```yaml
        packages:
        - git 
        - build-essential
        - jq
        - pgcli
        - rsync
        - python3-yaml
```

Replace it with:

```yaml
        packages:
        - git 
        - build-essential
        - jq
        - pgcli
        - rsync
        - python3-yaml
        - software-properties-common
```

- [ ] **Step 2: Validate the YAML**

Run: `python3 -c "import yaml; yaml.safe_load(open('lxd-maas-profile.yaml'))"`
Expected: no output, exit 0 (valid YAML).

- [ ] **Step 3: Commit**

```bash
git add lxd-maas-profile.yaml
git commit -m "feat: preinstall software-properties-common for deb install"
```

---

## Task 10: Final verification

- [ ] **Step 1: Run the complete test suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (43 tests), exit 0.

- [ ] **Step 2: Re-run the dry-run/validation matrix from Task 7 Step 6**

Confirm all five commands still behave as specified (3 errors, 2 successful dry-runs).

- [ ] **Step 3: Manual e2e (requires a live LXD host — run when available)**

1. `python3 maas-env.py --name dev --deb --ppa ppa:maas/3.7 --branch 3.7` → single container created, deb installed, MAAS UI reachable at the printed URL.
2. In the container: `/work` exists, owned by `ubuntu:ubuntu`, on branch `3.7`; `add-apt-repository` was available.
3. Admin `maas / maas` logs in.
4. `python3 maas-env.py --name dev --deb --overlay --overlay-config overlay-config-37.yaml` mounts the `deb:` overlay paths; `--unoverlay --deb` removes them; MAAS restarts cleanly.
5. The snap path (`python3 maas-env.py --name dev2`) still works end to end (regression check).

---

## Spec coverage check

- `--deb` flag, absence = snap → Task 4, Task 6/7.
- `--ppa` / `--branch`, required with `--deb` → Task 4 (args), Task 2 + Task 7 (validation).
- Two-tier validation (global + create-only) → Task 2 (helpers), Task 7 (wiring).
- `--deb --mode multi` rejected → Task 2 + Task 7.
- `maas-install.sh snap|deb` dispatch, shared clone, deb runs the four commands, no `connect-snap-interfaces` → Task 8.
- deb skips `setup_postgres`, uses node IP for summary → Task 6.
- Overlay `--deb` selects `deb:` section → Task 3 + Task 7.
- Method-aware restart after overlay (`snap restart maas` vs `systemctl restart 'maas-*'`) → Task 3 (`build_restart_command`) + Task 7 (wiring).
- Pure helpers + unit tests (`build_install_invocation`, validators, overlay method, restart) → Tasks 1-3.
- `add-apt-repository` availability (implementation detail beyond the spec) → Task 9.

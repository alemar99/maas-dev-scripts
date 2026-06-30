#!/usr/bin/env python3

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("maas-env")
log.setLevel(logging.INFO)

_out = logging.StreamHandler(sys.stdout)
_out.setLevel(logging.INFO)
_out.addFilter(lambda r: r.levelno < logging.WARNING)
_out.setFormatter(logging.Formatter("%(message)s"))

_err = logging.StreamHandler(sys.stderr)
_err.setLevel(logging.WARNING)
_err.setFormatter(logging.Formatter("%(message)s"))

log.addHandler(_out)
log.addHandler(_err)


class ScriptTarget(NamedTuple):
    path: str
    target: str  # "all", "node1", "node2", "node3"


def parse_script_arg(raw: str) -> ScriptTarget:
    """Parse 'path:target' into (path, target)."""
    if ":" not in raw:
        raise argparse.ArgumentTypeError(
            f"invalid script spec '{raw}': expected 'path:target' "
            f"(target is 'all', 'node1', 'node2', or 'node3')"
        )
    path, target = raw.rsplit(":", 1)
    if target not in ("all", "node1", "node2", "node3"):
        raise argparse.ArgumentTypeError(
            f"invalid target '{target}': must be 'all', 'node1', 'node2', or 'node3'"
        )
    return ScriptTarget(path=path, target=target)


def compute_containers(name: str, mode: str) -> list[str]:
    """Return the container names for a given base name and mode."""
    if mode == "single":
        return [name]
    return [f"{name}-{i}" for i in (1, 2, 3)]


def normalize_source(path: str) -> str:
    """Expand ~ and ensure exactly one trailing slash (rsync 'contents of')."""
    expanded = os.path.expanduser(path)
    return expanded.rstrip("/") + "/"


def source_resolves_to_root(path: str) -> bool:
    """True if path (after ~ expansion) resolves to filesystem root '/'.

    Catches '/', '/.', '/..', '/home/..', and symlinks to '/', any of which
    would make `rsync -a --delete` mirror the entire host root filesystem.
    """
    return os.path.realpath(os.path.expanduser(path)) == "/"


def build_rsh_value(python: str, script: str) -> str:
    """Build the rsync --rsh transport string that re-invokes this script."""
    return f"{python} {script} --rsh-shim"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/destroy MAAS test environments in LXD",
    )
    parser.add_argument(
        "--maas-channel",
        default="latest/edge",
        help="MAAS snap channel to install (default: latest/edge)",
    )
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
    parser.add_argument(
        "--mode",
        choices=["single", "multi"],
        default="single",
        help="Deployment mode (default: single)",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Base name for containers",
    )
    parser.add_argument(
        "--ubuntu",
        default="26.04",
        help="Ubuntu release for containers (default: 26.04)",
    )
    parser.add_argument(
        "--profile",
        default="./lxd-maas-profile.yaml",
        help="Path to LXD profile YAML (default: ./lxd-maas-profile.yaml)",
    )
    parser.add_argument(
        "--pre",
        action="append",
        default=[],
        type=parse_script_arg,
        help="Script to run before MAAS init. Repeatable. "
        "Format: path:all|node1|node2|node3",
    )
    parser.add_argument(
        "--post",
        action="append",
        default=[],
        type=parse_script_arg,
        help="Script to run after MAAS init. Repeatable. "
        "Format: path:all|node1|node2|node3",
    )
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without doing it",
    )
    return parser


DRY_RUN = False

# Paths excluded from --sync. With --delete, excluded paths are preserved in
# the container (not deleted), so each container's own .git survives.
EXCLUDES = [".git", "__pycache__", "*.pyc", ".overlayfs_workdir"]

# In-container path to the vendored overlay-mount tool. The repo is bind-mounted
# at /scripts in every container (see lxd-maas-profile.yaml).
OVERLAY_SCRIPT = "/scripts/overlay-mount.py"

# In-container path to the install script (bind-mounted at /scripts).
INSTALL_SCRIPT = "/scripts/maas-install.sh"


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


def _echo_dry(cmd_args: list[str]) -> None:
    log.info("[dry-run] %s", " ".join(cmd_args))


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a host command. If DRY_RUN, print it instead."""
    if DRY_RUN:
        _echo_dry(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    log.info("  $ %s", " ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        log.error(result.stderr.strip())
        sys.exit(1)
    return result


def lxc_exec(
    container: str, cmd: str, *, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a command inside an LXD container."""
    if DRY_RUN:
        _echo_dry(["lxc", "exec", container, "--", "sh", "-c", cmd])
        return subprocess.CompletedProcess(
            ["lxc", "exec", container, "--", "sh", "-c", cmd], 0, stdout="", stderr=""
        )
    log.info("  [%s] $ %s", container, cmd)
    result = subprocess.run(
        ["lxc", "exec", container, "--", "sh", "-c", cmd],
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        log.error(result.stderr.strip())
        sys.exit(1)
    if result.stdout.strip():
        log.info(result.stdout.strip())
    return result


def lxc_exec_capture(container: str, cmd: str) -> str:
    """Run a command inside a container and return its stdout."""
    if DRY_RUN:
        _echo_dry(["lxc", "exec", container, "--", "sh", "-c", cmd])
        return ""
    result = subprocess.run(
        ["lxc", "exec", container, "--", "sh", "-c", cmd],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        log.error(result.stderr.strip())
        sys.exit(1)
    return result.stdout.strip()


def create_network(name: str) -> None:
    """Create an LXD managed network (multi mode only)."""
    network = f"{name}-net"
    log.info("[net] Creating LXD network %s", network)
    result = run(["lxc", "network", "create", network], check=False)
    if result.returncode != 0:
        log.info("  (network already exists, skipping)")


def delete_network(name: str) -> None:
    """Delete an LXD managed network (multi mode only)."""
    network = f"{name}-net"
    log.info("[net] Deleting LXD network %s", network)
    result = run(["lxc", "network", "delete", network], check=False)
    if result.returncode != 0:
        log.warning("  (network may not exist, skipping)")


def create_containers(
    containers: list[str],
    ubuntu: str,
    profile: str,
    network: str | None,
) -> None:
    """Launch and start LXD containers."""
    image = f"ubuntu:{ubuntu}"

    for c in containers:
        log.info("[container] Initializing %s from %s", c, image)
        with open(profile, "rb") as f:
            run_cmd = ["lxc", "init", image, c]
            if DRY_RUN:
                _echo_dry(run_cmd + ["<", profile])
            else:
                log.info("  $ %s < %s", " ".join(run_cmd), profile)
                result = subprocess.run(
                    run_cmd,
                    stdin=f,
                    text=True,
                    capture_output=True,
                )
                if result.returncode != 0:
                    log.error(result.stderr.strip())
                    sys.exit(1)
        # Set idmap for correct bind mount permissions
        uid = os.getuid()
        gid = os.getgid()
        idmap_input = f"uid {uid} 1000\ngid {gid} 1000\n"
        if not DRY_RUN:
            result = subprocess.run(
                ["lxc", "config", "set", c, "raw.idmap", "-"],
                input=idmap_input,
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                log.error(result.stderr.strip())
                sys.exit(1)
        if network:
            run(
                [
                    "lxc",
                    "config",
                    "device",
                    "add",
                    c,
                    "eth0",
                    "nic",
                    f"network={network}",
                ]
            )

    for c in containers:
        log.info("[container] Starting %s", c)
        run(["lxc", "start", c])


def delete_containers(containers: list[str]) -> None:
    """Stop and delete LXD containers."""
    for c in containers:
        log.info("[container] Stopping %s", c)
        result = run(["lxc", "stop", c, "--force"], check=False)
        if result.returncode != 0:
            log.warning("  (container may not exist, skipping)")
    for c in containers:
        log.info("[container] Deleting %s", c)
        result = run(["lxc", "delete", c], check=False)
        if result.returncode != 0:
            log.warning("  (container may not exist, skipping)")


def run_scripts(
    containers: list[str],
    scripts: list[ScriptTarget],
    label: str,
    *,
    abort_on_failure: bool,
) -> None:
    """Run user-provided scripts on specified containers.

    Args:
        containers: List of container names (e.g. ['mytest-1', 'mytest-2', 'mytest-3'])
        scripts: List of (path, target) pairs
        label: Human label for log output (e.g. "pre-install")
        abort_on_failure: If True, exit on first failure. If False, warn and continue.
    """
    if not scripts:
        return

    for st in scripts:
        target_containers: list[str]
        match st.target:
            case "all":
                target_containers = list(containers)
            case "node1":
                target_containers = [containers[0]]
            case "node2":
                if len(containers) >= 2:
                    target_containers = [containers[1]]
                else:
                    log.warning(
                        "  [%s] skipping %s:node2 (container does not exist)",
                        label,
                        st.path,
                    )
                    continue
            case "node3":
                if len(containers) >= 3:
                    target_containers = [containers[2]]
                else:
                    log.warning(
                        "  [%s] skipping %s:node3 (container does not exist)",
                        label,
                        st.path,
                    )
                    continue
            case _:
                log.error(
                    "  [%s] unknown target '%s' — skipping",
                    label,
                    st.target,
                )
                continue

        for c in target_containers:
            log.info("  [%s] running %s on %s", label, st.path, c)
            try:
                result = lxc_exec(c, st.path, check=abort_on_failure)
                if not abort_on_failure and result.returncode != 0:
                    log.warning(
                        "  [%s] WARNING: %s on %s failed (exit code %d)",
                        label,
                        st.path,
                        c,
                        result.returncode,
                    )
            except SystemExit:
                raise


def setup_postgres(container: str) -> str:
    """Install and configure PostgreSQL on a container. Return its IP."""
    log.info("[postgres] Installing PostgreSQL on %s", container)
    lxc_exec(container, "/scripts/postgres-setup.sh")
    db_ip = lxc_exec_capture(container, "hostname -I | cut -d' ' -f1")
    log.info("[postgres] DB IP: %s", db_ip)
    return db_ip


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


def create_admin(container: str) -> None:
    """Create admin user and login on the first container."""
    log.info("[maas] Creating admin user on %s", container)
    lxc_exec(
        container,
        "sudo maas createadmin --username maas --password maas --email maas@admin",
    )
    lxc_exec(
        container,
        "maas login admin http://$(hostname -I | cut -d' ' -f1):5240/MAAS/api/2.0 "
        "$(sudo maas apikey --username maas)",
    )


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
    network = f"{name}-net" if mode == "multi" else None

    log.info(
        "=== Creating MAAS environment: %s (%s, %d nodes) ===",
        name,
        mode,
        len(containers),
    )

    if network:
        create_network(name)
    else:
        log.info("[net] Single mode — skipping network creation")

    profile_path = Path(profile)
    if not profile_path.exists():
        log.error("ERROR: profile not found: %s", profile)
        sys.exit(1)

    create_containers(containers, ubuntu, str(profile_path), network)

    log.info("[init] Waiting for cloud-init to finish on all nodes...")
    for c in containers:
        lxc_exec(c, "cloud-init status --wait > /dev/null 2>&1 || true")

    run_scripts(containers, pre_scripts, "pre-install", abort_on_failure=True)

    db_ip = setup_postgres(containers[0])

    install_maas(containers, "snap", db_ip=db_ip, channel=maas_channel)
    create_admin(containers[0])

    run_scripts(containers, post_scripts, "post-install", abort_on_failure=False)

    log.info("=== MAAS environment '%s' ready ===", name)
    log.info("  Containers: %s", ", ".join(containers))
    log.info("  MAAS URL:   http://%s:5240/MAAS", db_ip)
    log.info("  Admin:      maas / maas")


def destroy(name: str, mode: str, containers: list[str]) -> None:
    log.info("=== Destroying MAAS environment: %s ===", name)

    delete_containers(containers)

    if mode == "multi":
        delete_network(name)
    else:
        log.info("[net] Single mode — no network to delete")

    log.info("=== MAAS environment '%s' destroyed ===", name)


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
            detail = result.stderr.strip()
            log.warning(
                "  [sync] rsync to %s failed (exit code %d)%s",
                c,
                result.returncode,
                f": {detail}" if detail else "",
            )
            failures.append(c)
        else:
            log.info("  [sync] %s done", c)

    if failures:
        log.error("=== Sync finished with failures: %s ===", ", ".join(failures))
        sys.exit(1)
    log.info("=== Sync complete ===")


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
        if not args.sync.strip() or not args.sync_dest.strip():
            log.error("ERROR: --sync and --sync-dest must be non-empty paths")
            sys.exit(1)
        if source_resolves_to_root(args.sync):
            log.error("ERROR: refusing to sync from filesystem root '/'")
            sys.exit(1)
        if os.path.normpath(args.sync_dest) == "/":
            log.error("ERROR: refusing to sync into container root '/'")
            sys.exit(1)
        source = normalize_source(args.sync)
        if not os.path.isdir(source):
            log.error(
                "ERROR: sync source not found or not a directory: %s", args.sync
            )
            sys.exit(1)
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
            name=args.name,
            mode=args.mode,
            containers=containers,
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
            maas_channel=args.maas_channel,
        )


if __name__ == "__main__":
    main()


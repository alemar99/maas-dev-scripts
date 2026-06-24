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


DRY_RUN = False


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


def install_maas(containers: list[str], db_ip: str, maas_channel: str) -> None:
    """Install MAAS snap, connect interfaces, and init region+rack on all containers."""
    log.info("[snap] Installing and initializing MAAS on all nodes")
    for c in containers:
        lxc_exec(c, f"/scripts/maas-install.sh {db_ip} {maas_channel}")


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

    install_maas(containers, db_ip, maas_channel)
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


if __name__ == "__main__":
    main()


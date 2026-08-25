#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, NamedTuple, Self

try:
    import argcomplete  # pip install argcomplete; eval "$(register-python-argcomplete maas-env.py)"
except ImportError:
    argcomplete = None  # type: ignore[assignment]

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

# Paths excluded from sync. With --delete, excluded paths are preserved in
# the container (not deleted), so each container's own .git survives.
EXCLUDES = [".git", "__pycache__", "*.pyc", ".overlayfs_workdir"]

# In-container path to the vendored overlay-mount tool. The repo is bind-mounted
# at /scripts in every container (see lxd-maas-profile.yaml).
OVERLAY_SCRIPT = "/scripts/overlay-mount.py"

# In-container path to the install script (bind-mounted at /scripts).
INSTALL_SCRIPT = "/scripts/maas-install.sh"

# LXD project under which all containers and networks are created.
LXD_PROJECT = "maas-env"

# The `ubuntu` user inside the containers. Host uid/gid are idmapped onto it so
# bind mounts and rsynced files keep the ownership maas-install.sh expects.
CONTAINER_UID = 1000
CONTAINER_GID = 1000


class Mode(StrEnum):
    SINGLE = "single"
    MULTI = "multi"


class InstallType(StrEnum):
    SNAP = "snap"
    DEB = "deb"


class NodeTarget(StrEnum):
    ALL = "all"
    NODE1 = "node1"
    NODE2 = "node2"
    NODE3 = "node3"


def containers_for(name: str, mode: Mode) -> list[str]:
    """Return the container names for a given base name and mode."""
    if mode is Mode.SINGLE:
        return [name]
    return [f"{name}-{i}" for i in (1, 2, 3)]


class ScriptTarget(NamedTuple):
    path: str
    target: NodeTarget


class NetworkInfo(NamedTuple):
    """A managed LXD network and its auto-assigned IPv4 subnet.

    LXD DHCP is disabled on this network (MAAS runs its own), so containers get
    static IPs derived from `gateway`/`prefixlen` via cloud-init.
    """

    name: str
    gateway: str
    prefixlen: int

    def host_ip(self, index: int) -> str:
        """Return a stable static host IP for the Nth container in the subnet.

        Containers are placed at gateway-base + 10 + index (e.g. a /24 with
        gateway .1 yields .10, .11, .12), well clear of the gateway itself.
        """
        network = ipaddress.ip_network(
            f"{self.gateway}/{self.prefixlen}", strict=False
        )
        return str(network.network_address + 10 + index)


@dataclass
class Env:
    """An environment as recorded in the registry."""

    name: str
    mode: Mode
    install_type: InstallType
    maas_channel: str | None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> Self | None:
        if row is None:
            return None
        return cls(
            name=row["name"],
            mode=Mode(row["mode"]),
            install_type=InstallType(row["type"]),
            maas_channel=row["maas_channel"],
            created_at=row["created_at"],
        )


@dataclass
class EnvironmentSpec:
    """Everything needed to create a new MAAS environment."""

    name: str
    mode: Mode
    ubuntu: str
    profile: str
    pre_scripts: list[ScriptTarget]
    post_scripts: list[ScriptTarget]


class Registry(ABC):
    @abstractmethod
    def path(self) -> Path: ...

    @abstractmethod
    def add(self, env: Env) -> None: ...

    @abstractmethod
    def list_envs(self) -> list[Env]: ...

    @abstractmethod
    def get(self, name: str) -> Env | None: ...

    @abstractmethod
    def delete(self, name: str) -> None: ...


class SqliteRegistry(Registry):
    def __init__(self, dry_run: bool = False) -> None:
        super().__init__()
        self.dry_run = dry_run
        self.ensure_db()

    def path(self) -> Path:
        """Return the path to the sqlite registry, honouring XDG_DATA_HOME."""
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
            "~/.local/share"
        )
        return Path(data_home) / "maas-env" / "environments.db"

    def ensure_db(self) -> None:
        db_path = self.path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS environments (
                    name        TEXT PRIMARY KEY,
                    mode        TEXT NOT NULL,
                    type        TEXT NOT NULL,
                    maas_channel TEXT,
                    created_at  TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def connect(self) -> Generator[sqlite3.Cursor]:
        """Open (creating if needed) the registry DB and yield a cursor."""
        conn = sqlite3.connect(self.path())
        conn.row_factory = sqlite3.Row
        try:
            yield conn.cursor()
            conn.commit()
        finally:
            conn.close()

    def add(self, env: Env) -> None:
        """Insert (or replace) an environment record in the registry."""
        if self.dry_run:
            log.info("[dry-run] registry: add %s", env)
            return
        try:
            with self.connect() as cursor:
                cursor.execute(
                    "INSERT OR REPLACE INTO environments "
                    "(name, mode, type, maas_channel, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        env.name,
                        str(env.mode),
                        str(env.install_type),
                        env.maas_channel,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except sqlite3.Error as exc:
            log.warning("  (failed to add environment in registry: %s)", exc)

    def delete(self, name: str) -> None:
        """Delete an environment record from the registry."""
        if self.dry_run:
            log.info("[dry-run] registry: delete %s", name)
            return
        try:
            with self.connect() as cursor:
                cursor.execute("DELETE FROM environments WHERE name = ?", (name,))
        except sqlite3.Error as exc:
            log.warning("  (failed to remove environment from registry: %s)", exc)

    def list_envs(self) -> list[Env]:
        """Return all recorded environments, newest first."""
        with self.connect() as cursor:
            rows = cursor.execute(
                "SELECT name, mode, type, maas_channel, created_at "
                "FROM environments ORDER BY created_at DESC"
            ).fetchall()
        return [env for row in rows if (env := Env.from_row(row)) is not None]

    def get(self, name: str) -> Env | None:
        """Return the recorded row for an environment, or None if not tracked."""
        try:
            with self.connect() as cursor:
                row = cursor.execute(
                    "SELECT name, mode, type, maas_channel, created_at "
                    "FROM environments WHERE name = ?",
                    (name,),
                ).fetchone()
                return Env.from_row(row)
        except sqlite3.Error as exc:
            log.warning("  (failed to read environment from registry: %s)", exc)
            return None


class Install(ABC):
    install_type: InstallType

    @abstractmethod
    def invocation(self, db_ip: str | None) -> str:
        """The in-container command that runs maas-install.sh for this method."""

    @property
    def provisions_own_db(self) -> bool:
        """True if the package brings its own PostgreSQL (no separate DB node)."""
        return False

    @property
    def registry_channel(self) -> str | None:
        """The channel to persist in the registry (None when not applicable)."""
        return None


@dataclass
class SnapInstall(Install):
    install_type = InstallType.SNAP
    channel: str

    def invocation(self, db_ip: str | None) -> str:
        return f"{INSTALL_SCRIPT} snap {db_ip} {self.channel}"

    @property
    def registry_channel(self) -> str | None:
        return self.channel


@dataclass
class DebInstall(Install):
    install_type = InstallType.DEB
    ppa: str
    branch: str

    def invocation(self, db_ip: str | None = None) -> str:
        return f"{INSTALL_SCRIPT} deb {self.ppa} {self.branch}"

    @property
    def provisions_own_db(self) -> bool:
        # The deb package provisions its own local PostgreSQL; no separate DB.
        return True


class Lxd:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    @staticmethod
    def _log_dry(cmd_args: list[str]) -> None:
        log.info("[dry-run] %s", " ".join(cmd_args))

    @staticmethod
    def _fail_on_error(result: subprocess.CompletedProcess) -> None:
        """Log stderr and exit if the process failed."""
        if result.returncode != 0:
            log.error(result.stderr.strip())
            sys.exit(1)

    @staticmethod
    def _project_args() -> list[str]:
        """Return `--project maas-env` for every LXD command."""
        return ["--project", LXD_PROJECT]

    def ensure_project(self) -> None:
        """Create the maas-env project if it does not exist."""
        result = self.run(
            [
                "lxc",
                "project",
                "create",
                LXD_PROJECT,
                "--config",
                "features.images=false",
                "--config",
                "features.profiles=true",
                "--config",
                "features.storage.volumes=false",
            ],
            check=False,
        )
        if result.returncode == 0:
            log.info("[project] Created LXD project %s", LXD_PROJECT)
        else:
            if f"Project \"{LXD_PROJECT}\" already exists" in result.stderr:
                log.info(
                    "[project] LXD project %s already exists", LXD_PROJECT,
                )
            else:
                log.error("[project] Failed creating the LXD project %s. Stderr: %s", LXD_PROJECT, result.stderr)
                sys.exit(1)

    def run(self, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        """Run a host command. If dry_run, print it instead."""
        if self.dry_run:
            self._log_dry(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        log.info("  $ %s", " ".join(cmd))
        result = subprocess.run(cmd, text=True, capture_output=True)
        if check:
            self._fail_on_error(result)
        return result

    def exec(
        self, container: str, cmd: str, *, check: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a shell command inside an LXD container."""
        argv = ["lxc", *self._project_args(), "exec", container, "--", "sh", "-c", cmd]
        if self.dry_run:
            self._log_dry(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        log.info("  [%s] $ %s", container, cmd)
        result = subprocess.run(argv, text=True, capture_output=True)
        if check:
            self._fail_on_error(result)
        if result.stdout.strip():
            log.info(result.stdout.strip())
        return result

    def exec_capture(self, container: str, cmd: str) -> str:
        """Run a shell command inside a container and return its stdout."""
        argv = ["lxc", *self._project_args(), "exec", container, "--", "sh", "-c", cmd]
        if self.dry_run:
            self._log_dry(argv)
            return ""
        result = subprocess.run(argv, text=True, capture_output=True)
        self._fail_on_error(result)
        return result.stdout.strip()

    def exec_passthrough(self, container: str, argv: list[str]) -> int:
        """Run a command in a container with the caller's stdio. Return its exit code."""
        cmd = ["lxc", *self._project_args(), "exec", container, "--", *argv]
        if self.dry_run:
            self._log_dry(cmd)
            return 0
        return subprocess.run(cmd).returncode

    def exec_replace(self, container: str, argv: list[str]) -> None:
        """Replace this process with a command running in the container."""
        cmd = ["lxc", *self._project_args(), "exec", container, "--", *argv]
        if self.dry_run:
            self._log_dry(cmd)
            return
        os.execvp(cmd[0], cmd)

    def is_container_running(self, container: str) -> bool:
        """Check whether a container exists and is running (via `lxc ls`)."""
        result = subprocess.run(
            [
                "lxc",
                *self._project_args(),
                "ls",
                "--columns",
                "s",
                "--format",
                "csv",
                container,
            ],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "RUNNING"

    def node_ip(self, container: str) -> str:
        """Return the container's primary IP address."""
        return self.exec_capture(container, "hostname -I | cut -d' ' -f1")

    def create_network(self, name: str) -> NetworkInfo:
        """Create an LXD managed network with DHCP disabled.

        MAAS runs its own DHCP server on this subnet; LXD's managed dnsmasq DHCP
        would fight it, so we turn LXD DHCP off (v4 and v6) and disable IPv6
        entirely. dnsmasq keeps serving DNS on the gateway and NAT stays on, so
        the containers still reach the internet once they have a static IP.
        """
        network = f"{name}-net"
        log.info("[net] Creating LXD network %s", network)
        result = self.run(
            ["lxc", *self._project_args(), "network", "create", network], check=False
        )
        if result.returncode != 0:
            log.info("  (network already exists, skipping)")

        log.info("[net] Disabling LXD-managed DHCP on %s", network)
        self.run(
            [
                "lxc",
                *self._project_args(),
                "network",
                "set",
                network,
                "ipv4.dhcp",
                "false",
            ]
        )
        self.run(
            [
                "lxc",
                *self._project_args(),
                "network",
                "set",
                network,
                "ipv6.dhcp",
                "false",
            ]
        )
        self.run(
            [
                "lxc",
                *self._project_args(),
                "network",
                "set",
                network,
                "ipv6.address",
                "none",
            ]
        )

        gateway, prefixlen = self._network_subnet(network)
        log.info(
            "[net] %s subnet %s/%d (gateway %s), DHCP off",
            network,
            gateway,
            prefixlen,
            gateway,
        )
        return NetworkInfo(name=network, gateway=gateway, prefixlen=prefixlen)

    def _network_subnet(self, network: str) -> tuple[str, int]:
        """Read a managed network's IPv4 gateway address and prefix length."""
        if self.dry_run:
            # No live network to query; return a representative subnet so the
            # rest of the dry-run (static IP assignment) has something to show.
            return "10.0.0.1", 24
        result = self.run(
            ["lxc", *self._project_args(), "network", "get", network, "ipv4.address"]
        )
        cidr = result.stdout.strip()
        if not cidr:
            log.error("could not read ipv4.address for network %s", network)
            sys.exit(1)
        iface = ipaddress.ip_interface(cidr)
        return str(iface.ip), iface.network.prefixlen

    def delete_network(self, name: str) -> None:
        """Delete an LXD managed network."""
        network = f"{name}-net"
        log.info("[net] Deleting LXD network %s", network)
        result = self.run(
            ["lxc", *self._project_args(), "network", "delete", network], check=False
        )
        if result.returncode != 0:
            log.warning("  (network may not exist, skipping)")

    def create_containers(
        self,
        containers: list[str],
        ubuntu: str,
        profile: str,
        network: NetworkInfo | None,
    ) -> None:
        """Launch and start LXD containers.

        When a `network` is given its DHCP is disabled, so each container is
        assigned a static IP via cloud-init before it first boots.
        """
        image = f"ubuntu:{ubuntu}"
        for index, container in enumerate(containers):
            self._init_container(container, image, profile)
            self._set_idmap(container)
            if network:
                self._attach_network(container, network.name)
                self._set_network_config(container, network, index)

        for container in containers:
            log.info("[container] Starting %s", container)
            self.run(["lxc", *self._project_args(), "start", container])

    def _init_container(self, container: str, image: str, profile: str) -> None:
        log.info("[container] Initializing %s from %s", container, image)
        cmd = ["lxc", *self._project_args(), "init", image, container]
        if self.dry_run:
            self._log_dry(cmd + ["<", profile])
            return
        log.info("  $ %s < %s", " ".join(cmd), profile)
        with open(profile, "rb") as profile_file:
            result = subprocess.run(
                cmd, stdin=profile_file, text=True, capture_output=True
            )
        self._fail_on_error(result)

    def _set_idmap(self, container: str) -> None:
        """Map the host user onto the container's ubuntu user for bind mounts."""
        idmap = f"uid {os.getuid()} {CONTAINER_UID}\ngid {os.getgid()} {CONTAINER_GID}\n"
        cmd = [
            "lxc",
            *self._project_args(),
            "config",
            "set",
            container,
            "raw.idmap",
            "-",
        ]
        if self.dry_run:
            self._log_dry(cmd)
            return
        result = subprocess.run(cmd, input=idmap, text=True, capture_output=True)
        self._fail_on_error(result)

    def _attach_network(self, container: str, network: str) -> None:
        self.run(
            [
                "lxc",
                *self._project_args(),
                "config",
                "device",
                "add",
                container,
                "eth0",
                "nic",
                f"network={network}",
            ]
        )

    def _set_network_config(
        self, container: str, network: NetworkInfo, index: int
    ) -> None:
        """Assign a static IP via cloud-init (LXD DHCP is disabled on the net).

        Must run before the container's first boot so cloud-init applies it.
        """
        ip = network.host_ip(index)
        log.info("[container] Assigning static IP %s/%d to %s", ip, network.prefixlen, container)
        config = self._render_network_config(ip, network.prefixlen, network.gateway)
        cmd = [
            "lxc",
            *self._project_args(),
            "config",
            "set",
            container,
            "user.network-config",
            "-",
        ]
        if self.dry_run:
            self._log_dry(cmd)
            return
        result = subprocess.run(cmd, input=config, text=True, capture_output=True)
        self._fail_on_error(result)

    @staticmethod
    def _render_network_config(ip: str, prefixlen: int, gateway: str) -> str:
        """Render a cloud-init v2 network-config for a single static-IP NIC.

        DNS points at the gateway, where LXD's dnsmasq keeps forwarding queries
        (and NATs traffic to the internet) even with DHCP disabled.
        """
        return textwrap.dedent(
            f"""\
            version: 2
            ethernets:
              eth0:
                addresses:
                  - {ip}/{prefixlen}
                routes:
                  - to: default
                    via: {gateway}
                nameservers:
                  addresses:
                    - {gateway}
            """
        )

    def delete_containers(self, containers: list[str]) -> None:
        """Stop and delete LXD containers."""
        for container in containers:
            log.info("[container] Stopping %s", container)
            result = self.run(
                ["lxc", *self._project_args(), "stop", container, "--force"],
                check=False,
            )
            if result.returncode != 0:
                log.warning("  (container may not exist, skipping)")
        for container in containers:
            log.info("[container] Deleting %s", container)
            result = self.run(
                ["lxc", *self._project_args(), "delete", container], check=False
            )
            if result.returncode != 0:
                log.warning("  (container may not exist, skipping)")


class MaasEnv:
    _RESTART_COMMANDS: ClassVar = {
        InstallType.DEB: "sudo systemctl restart 'maas-*'",
        InstallType.SNAP: "sudo snap restart maas",
    }

    def __init__(self, lxd: Lxd, registry: Registry, dry_run: bool = False) -> None:
        self.lxd = lxd
        self.registry = registry
        self.dry_run = dry_run

    @staticmethod
    def _overlay_command(
        subcommand: str, config: str, method: InstallType, workdir: str = "/work"
    ) -> str:
        """Build the in-container shell command that runs the overlay tool.

        Runs from `workdir` (must be on the container rootfs) so the tool's
        relative `.overlayfs_workdir` shares a filesystem with the
        `/work/src/...` upperdirs. `method` selects the package section:
        '--snap' or '--deb'.
        """
        return (
            f"cd {workdir} && python3 {OVERLAY_SCRIPT} {subcommand} "
            f"--config {config} --{method}"
        )

    @classmethod
    def _restart_command(cls, method: InstallType) -> str:
        """Command to restart MAAS after (un)overlaying, per install method."""
        return cls._RESTART_COMMANDS[method]

    @staticmethod
    def _rsync_command(source: str, container: str, dest: str, rsh: str) -> list[str]:
        """Build the host-side rsync argv that syncs into a container via the shim."""
        cmd = ["rsync", "-a", "--no-owner", "--no-group", "--delete"]
        for pattern in EXCLUDES:
            cmd += ["--exclude", pattern]
        cmd += ["-e", rsh]
        cmd += [source, f"{container}:{dest.rstrip('/')}/"]
        return cmd

    def setup_postgres(self, container: str) -> str:
        """Install and configure PostgreSQL on a container. Return its IP."""
        log.info("[postgres] Installing PostgreSQL on %s", container)
        self.lxd.exec(container, "/scripts/postgres-setup.sh")
        db_ip = self.lxd.node_ip(container)
        log.info("[postgres] DB IP: %s", db_ip)
        return db_ip

    def install_maas(
        self,
        containers: list[str],
        install: Install,
        *,
        db_ip: str | None = None,
    ) -> None:
        """Install MAAS on all nodes via maas-install.sh for the given method."""
        log.info(
            "[%s] Installing and initializing MAAS on all nodes", install.install_type
        )
        cmd = install.invocation(db_ip)
        for container in containers:
            self.lxd.exec(container, cmd)

    def create_admin(self, container: str) -> None:
        """Create admin user and login on the first container."""
        log.info("[maas] Creating admin user on %s", container)
        self.lxd.exec(
            container,
            "sudo maas createadmin --username maas --password maas --email maas@admin",
        )
        self.lxd.exec(
            container,
            "maas login admin http://$(hostname -I | cut -d' ' -f1):5240/MAAS/api/2.0 "
            "$(sudo maas apikey --username maas)",
        )

    @staticmethod
    def _resolve_targets(
        script: ScriptTarget, containers: list[str], label: str
    ) -> list[str] | None:
        """Return the containers a script targets, or None if unavailable.

        `containers` is ordered, so nodeN maps to index N-1.
        """
        if script.target is NodeTarget.ALL:
            return list(containers)
        index = int(script.target.removeprefix("node")) - 1
        if index < len(containers):
            return [containers[index]]
        log.warning(
            "  [%s] skipping %s:%s (container does not exist)",
            label,
            script.path,
            script.target,
        )
        return None

    def run_scripts(
        self,
        containers: list[str],
        scripts: list[ScriptTarget],
        label: str,
        *,
        abort_on_failure: bool,
    ) -> None:
        """Run user-provided scripts on their target containers.

        Args:
            containers: Ordered container names (e.g. ['t-1', 't-2', 't-3']).
            scripts: Scripts and the nodes they target.
            label: Human label for log output (e.g. "pre-install").
            abort_on_failure: If True, exit on first failure; else warn and continue.
        """
        for script in scripts:
            targets = self._resolve_targets(script, containers, label)
            if targets is None:
                continue
            for container in targets:
                log.info("  [%s] running %s on %s", label, script.path, container)
                result = self.lxd.exec(
                    container, script.path, check=abort_on_failure
                )
                if result.returncode != 0:
                    log.warning(
                        "  [%s] WARNING: %s on %s failed (exit code %d)",
                        label,
                        script.path,
                        container,
                        result.returncode,
                    )

    def create(self, spec: EnvironmentSpec, install: Install) -> None:
        containers = containers_for(spec.name, spec.mode)
        log.info(
            "=== Creating MAAS environment: %s (%s, %d nodes) ===",
            spec.name,
            spec.mode,
            len(containers),
        )

        profile_path = Path(spec.profile)
        if not profile_path.exists():
            log.error("ERROR: profile not found: %s", spec.profile)
            sys.exit(1)

        self.lxd.ensure_project()
        network = self.lxd.create_network(spec.name)
        self.lxd.create_containers(
            containers, spec.ubuntu, str(profile_path), network
        )

        self.registry.add(
            Env(
                name=spec.name,
                mode=spec.mode,
                install_type=install.install_type,
                maas_channel=install.registry_channel,
            )
        )

        log.info("[init] Waiting for cloud-init to finish on all nodes...")
        for container in containers:
            self.lxd.exec(
                container, "cloud-init status --wait > /dev/null 2>&1 || true"
            )

        self.run_scripts(
            containers, spec.pre_scripts, "pre-install", abort_on_failure=True
        )

        if install.provisions_own_db:
            self.install_maas(containers, install)
            maas_ip = self.lxd.node_ip(containers[0])
        else:
            db_ip = self.setup_postgres(containers[0])
            self.install_maas(containers, install, db_ip=db_ip)
            maas_ip = db_ip

        self.create_admin(containers[0])

        self.run_scripts(
            containers, spec.post_scripts, "post-install", abort_on_failure=False
        )

        log.info("=== MAAS environment '%s' ready ===", spec.name)
        log.info("  Containers: %s", ", ".join(containers))
        log.info("  MAAS URL:   http://%s:5240/MAAS", maas_ip)
        log.info("  Admin:      maas / maas")

    def destroy(self, name: str, containers: list[str]) -> None:
        log.info("=== Destroying MAAS environment: %s ===", name)

        self.lxd.delete_containers(containers)
        self.lxd.delete_network(name)
        self.registry.delete(name)

        log.info("=== MAAS environment '%s' destroyed ===", name)

    def list_environments(self) -> None:
        """Print the environments recorded in the local registry."""
        try:
            envs = self.registry.list_envs()
        except sqlite3.Error as exc:
            log.error("ERROR: failed to read registry: %s", exc)
            sys.exit(1)

        if not envs:
            log.info("No environments recorded (%s)", self.registry.path())
            return

        header = f"{'NAME':<20} {'MODE':<8} {'TYPE':<6} {'CHANNEL':<16} CREATED"
        log.info(header)
        log.info("-" * len(header))
        for env in envs:
            log.info(
                "%-20s %-8s %-6s %-16s %s",
                env.name,
                env.mode,
                env.install_type,
                env.maas_channel or "-",
                env.created_at,
            )

    def status(self, name: str, mode: Mode, containers: list[str]) -> None:
        """Print container state, MAAS URL, and overlay state for an environment."""
        log.info("Environment : %s", name)
        log.info("Mode        : %s", mode)
        log.info("")
        for container in containers:
            running = self.lxd.is_container_running(container)
            log.info(
                "  Container : %s  [%s]",
                container,
                "RUNNING" if running else "STOPPED",
            )
            if running:
                log.info(
                    "  MAAS URL  : http://%s:5240/MAAS", self.lxd.node_ip(container)
                )
                log.info("  Overlay   : %s", self._overlay_state(container))
            log.info("")

    def _overlay_state(self, container: str) -> str:
        """Report whether any overlay mount is currently applied in a container."""
        return self.lxd.exec_capture(
            container,
            "mount 2>/dev/null | grep -q overlay && echo applied || echo not-applied",
        )

    def sync(self, containers: list[str], source: str, dest: str) -> None:
        """Rsync a host source dir into each container's dest dir via the shim."""
        log.info(
            "=== Syncing %s -> %d container(s) at %s ===",
            source,
            len(containers),
            dest,
        )
        # rsync --rsh transport that re-invokes this script as the shim.
        rsh = f"{sys.executable} {Path(__file__).resolve()} --rsh-shim"
        failures: list[str] = []

        for container in containers:
            if not self.dry_run and not self.lxd.is_container_running(container):
                log.warning(
                    "  [sync] %s is missing or not running - skipping", container
                )
                failures.append(container)
                continue

            cmd = self._rsync_command(source, container, dest, rsh)
            result = self.lxd.run(cmd, check=False)
            if result.returncode != 0:
                detail = result.stderr.strip()
                log.warning(
                    "  [sync] rsync to %s failed (exit code %d)%s",
                    container,
                    result.returncode,
                    f": {detail}" if detail else "",
                )
                failures.append(container)
            else:
                log.info("  [sync] %s done", container)

        if failures:
            log.error("=== Sync finished with failures: %s ===", ", ".join(failures))
            sys.exit(1)
        log.info("=== Sync complete ===")

    def overlay(
        self,
        containers: list[str],
        subcommand: str,
        config: str,
        method: InstallType = InstallType.SNAP,
    ) -> None:
        """Run the overlay tool (sync/unsync) in each container, then restart MAAS."""
        verb = "Overlaying" if subcommand == "sync" else "Removing overlays from"
        log.info(
            "=== %s %d container(s) (config %s) ===", verb, len(containers), config
        )
        cmd = self._overlay_command(subcommand, config, method)
        failures: list[str] = []

        for container in containers:
            if not self.dry_run and not self.lxd.is_container_running(container):
                log.warning(
                    "  [overlay] %s is missing or not running - skipping", container
                )
                failures.append(container)
                continue

            result = self.lxd.exec(container, cmd, check=False)
            if result.returncode != 0:
                log.warning(
                    "  [overlay] %s failed on %s (exit code %d)",
                    subcommand,
                    container,
                    result.returncode,
                )
                failures.append(container)
                continue

            restart = self.lxd.exec(
                container, self._restart_command(method), check=False
            )
            if restart.returncode != 0:
                log.warning(
                    "  [overlay] MAAS restart failed on %s (exit code %d)",
                    container,
                    restart.returncode,
                )
                failures.append(container)
                continue

            log.info("  [overlay] %s done", container)

        if failures:
            log.error("=== Overlay finished with failures: %s ===", ", ".join(failures))
            sys.exit(1)
        log.info("=== Overlay complete ===")


class Command(ABC):
    name: str
    help: str

    @abstractmethod
    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Declare this subcommand's argparse arguments."""

    @abstractmethod
    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        """Run the subcommand against a wired-up MaasEnv."""

    @staticmethod
    def add_env_args(parser: argparse.ArgumentParser) -> None:
        """Add the `name` positional and --dry-run, shared by env subcommands.

        Everything else about an environment (mode, install type, channel) is
        recorded in the registry at create time and looked up by name, so the
        other subcommands never re-specify it.
        """
        parser.add_argument(
            "name",
            help="Base name for the environment's containers",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be done without doing it",
        )

    @staticmethod
    def add_container_arg(parser: argparse.ArgumentParser) -> None:
        """Add --container, used by subcommands that default to the primary node."""
        parser.add_argument(
            "--container",
            default=None,
            metavar="NAME",
            help="Target a specific container instead of the primary node",
        )

    @staticmethod
    def lookup(registry: Registry, name: str) -> tuple[Env | None, list[str]]:
        """Fetch a tracked env's record and container list, logging the outcome.

        Falls back to a single 'snap' environment when the name is untracked.
        """
        record = registry.get(name)
        if record is not None:
            log.info(
                "[registry] using recorded settings for '%s' (mode=%s, type=%s)",
                name,
                record.mode,
                record.install_type,
            )
            mode = record.mode
        else:
            log.warning(
                "[registry] '%s' is not tracked; assuming mode=single, type=snap",
                name,
            )
            mode = Mode.SINGLE
        return record, containers_for(name, mode)


class CreateCommand(Command):
    name = "create"
    help = "Create and initialise a MAAS environment"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        installs = parser.add_subparsers(dest="install_type", metavar="INSTALL_TYPE")
        installs.required = True

        p_snap = installs.add_parser(InstallType.SNAP, help="Install MAAS from the snap")
        self._add_create_args(p_snap)
        p_snap.add_argument(
            "--mode",
            type=Mode,
            choices=list(Mode),
            default=Mode.SINGLE,
            help="Deployment mode (default: single). Recorded in the registry "
            "and reused by later commands for this name.",
        )
        p_snap.add_argument(
            "--channel",
            default="latest/edge",
            help="MAAS snap channel to install (default: latest/edge)",
        )

        p_deb = installs.add_parser(
            InstallType.DEB,
            # MAAS dropped deb/PPA packaging after 3.8; only relevant for
            # ppa:maas/3.7 and ppa:maas/3.8 style channels.
            help="Install MAAS from a deb/PPA (single-node only, MAAS <= 3.8)",
        )
        self._add_create_args(p_deb)
        p_deb.add_argument(
            "--ppa",
            required=True,
            help="PPA to install the MAAS deb from (e.g. ppa:maas/3.7)",
        )
        p_deb.add_argument(
            "--branch",
            required=True,
            help="MAAS git branch cloned into /work for the deb path",
        )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        spec = EnvironmentSpec(
            name=args.name,
            # deb is single-node only, so its subparser omits --mode.
            mode=getattr(args, "mode", Mode.SINGLE),
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
        )
        env.create(spec, self._install_from_args(args))

    def _add_create_args(self, parser: argparse.ArgumentParser) -> None:
        """Add the arguments common to every install type."""
        self.add_env_args(parser)
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
            type=self._parse_script_arg,
            metavar="PATH:TARGET",
            help="Script to run before MAAS init. Repeatable. "
            "Format: path:all|node1|node2|node3",
        )
        parser.add_argument(
            "--post",
            action="append",
            default=[],
            type=self._parse_script_arg,
            metavar="PATH:TARGET",
            help="Script to run after MAAS init. Repeatable. "
            "Format: path:all|node1|node2|node3",
        )

    @staticmethod
    def _install_from_args(args: argparse.Namespace) -> Install:
        if args.install_type == InstallType.DEB:
            return DebInstall(ppa=args.ppa, branch=args.branch)
        return SnapInstall(channel=args.channel)

    @staticmethod
    def _parse_script_arg(raw: str) -> ScriptTarget:
        """Parse 'path:target' into a ScriptTarget. Used as an argparse type."""
        targets = ", ".join(f"'{t}'" for t in NodeTarget)
        if ":" not in raw:
            raise argparse.ArgumentTypeError(
                f"invalid script spec '{raw}': expected 'path:target' "
                f"(target is one of {targets})"
            )
        path, target = raw.rsplit(":", 1)
        try:
            return ScriptTarget(path=path, target=NodeTarget(target))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid target '{target}': must be one of {targets}"
            ) from None


class DestroyCommand(Command):
    name = "destroy"
    help = "Stop and delete containers and the LXD network"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the destroy confirmation prompt (for scripted use)",
        )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        _, containers = self.lookup(env.registry, args.name)
        if not env.dry_run and not args.yes and not self._confirm(args.name):
            log.info("Aborted.")
            return
        env.destroy(args.name, containers)

    @staticmethod
    def _confirm(name: str) -> bool:
        answer = input(
            f"Destroy environment '{name}'? "
            "This will delete all containers and the LXD network. [y/N] "
        )
        return answer.strip().lower() == "y"


class ListCommand(Command):
    name = "list"
    help = "List environments recorded in the local registry"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        # No arguments: always lists everything in the registry.
        pass

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        env.list_environments()


class SyncCommand(Command):
    name = "sync"
    help = "Rsync a host MAAS repo into each container"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        parser.add_argument(
            "source",
            metavar="PATH",
            help="Host directory to sync (e.g. ~/work/maas)",
        )
        parser.add_argument(
            "--dest",
            default="/work",
            metavar="DIR",
            help="Destination dir inside each container (default: /work)",
        )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        source, dest = args.source, args.dest
        if not source.strip() or not dest.strip():
            log.error("ERROR: source PATH and --dest must be non-empty")
            sys.exit(1)
        if self._resolves_to_root(source):
            log.error("ERROR: refusing to sync from filesystem root '/'")
            sys.exit(1)
        if os.path.normpath(dest) == "/":
            log.error("ERROR: refusing to sync into container root '/'")
            sys.exit(1)
        source = self._normalize_source(source)
        if not os.path.isdir(source):
            log.error(
                "ERROR: sync source not found or not a directory: %s", args.source
            )
            sys.exit(1)
        _, containers = self.lookup(env.registry, args.name)
        env.sync(containers, source, dest)

    @staticmethod
    def _normalize_source(path: str) -> str:
        """Expand ~ and ensure exactly one trailing slash (rsync 'contents of')."""
        expanded = os.path.expanduser(path)
        return expanded.rstrip("/") + "/"

    @staticmethod
    def _resolves_to_root(path: str) -> bool:
        """True if path (after ~ expansion) resolves to filesystem root '/'.

        Catches '/', '/.', '/..', '/home/..', and symlinks to '/', any of which
        would make `rsync -a --delete` mirror the entire host root filesystem.
        """
        return os.path.realpath(os.path.expanduser(path)) == "/"


class OverlayCommand(Command):
    name = "overlay"
    help = "Manage overlay mounts inside containers"

    _ACTIONS = (
        ("apply", "Overlay-mount /work/src/* onto MAAS paths, then restart MAAS"),
        ("remove", "Remove overlay mounts, then restart MAAS"),
    )

    # Channel prefix -> overlay config file, used when --config is omitted.
    #
    # "latest" tracks the tip of development, which moves past whatever the
    # newest numbered config is (currently 3.8) as new releases branch off.
    # overlay-config-master.yaml has no deb: section because MAAS dropped deb
    # packaging after 3.8 (snap-only from then on).
    _CONFIG_BY_CHANNEL_PREFIX = {
        "3.7": "overlay-config-37.yaml",
        "3.8": "overlay-config-38.yaml",
        "latest": "overlay-config-master.yaml",
    }

    def configure(self, parser: argparse.ArgumentParser) -> None:
        actions = parser.add_subparsers(dest="overlay_action", metavar="ACTION")
        actions.required = True
        for action_name, action_help in self._ACTIONS:
            p = actions.add_parser(action_name, help=action_help)
            self.add_env_args(p)
            p.add_argument(
                "--config",
                default=None,
                metavar="PATH",
                help="Path to the overlay config YAML (must live inside this repo). "
                "Auto-selected from the environment's MAAS channel when omitted.",
            )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        method = record.install_type if record is not None else InstallType.SNAP
        host_config = self._host_config(args.config, record)
        subcommand = "sync" if args.overlay_action == "apply" else "unsync"
        env.overlay(containers, subcommand, self._in_container(host_config), method)

    @classmethod
    def _host_config(cls, config_arg: str | None, record: Env | None) -> str:
        """Resolve the host-side overlay config, explicit or auto-selected."""
        if config_arg and config_arg.strip():
            host_config = os.path.expanduser(config_arg)
            if not os.path.isfile(host_config):
                log.error(
                    "ERROR: overlay config not found or not a file: %s", config_arg
                )
                sys.exit(1)
            return host_config

        channel = record.maas_channel if record is not None else None
        if not channel:
            log.error(
                "ERROR: --config is required (environment has no recorded channel)"
            )
            sys.exit(1)
        try:
            host_config = cls._config_for_channel(channel, cls._repo_root())
        except ValueError as exc:
            log.error("ERROR: %s", exc)
            sys.exit(1)
        log.info("[overlay] Auto-selected config: %s", host_config)
        return host_config

    @classmethod
    def _config_for_channel(cls, channel: str, repo_root: str) -> str:
        """Map a MAAS channel to its overlay config path inside the repo."""
        prefix = channel.split("/")[0].strip()
        name = cls._CONFIG_BY_CHANNEL_PREFIX.get(prefix)
        if name is None:
            raise ValueError(
                f"cannot auto-select overlay config for channel '{channel}'. "
                f"Pass --config explicitly."
            )
        path = os.path.join(repo_root, name)
        if not os.path.isfile(path):
            raise ValueError(
                f"auto-selected overlay config '{name}' for channel '{channel}' "
                f"does not exist at {path}. Pass --config explicitly."
            )
        return path

    @classmethod
    def _in_container(cls, host_config: str) -> str:
        """Map the host overlay config to its in-container path, or exit."""
        try:
            return cls._map_into_repo(host_config, cls._repo_root())
        except ValueError as exc:
            log.error("ERROR: %s", exc)
            sys.exit(1)

    @staticmethod
    def _repo_root() -> str:
        return str(Path(__file__).resolve().parent)

    @staticmethod
    def _map_into_repo(host_config: str, repo_root: str, mount: str = "/scripts") -> str:
        """Map a host overlay-config path to its path inside a container.

        The repo is bind-mounted at `mount` (default /scripts) in every
        container, so the config must live inside `repo_root`. Returns
        `<mount>/<relpath>`. Raises ValueError if the config is outside the repo.
        """
        abs_config = os.path.abspath(os.path.expanduser(host_config))
        abs_root = os.path.abspath(os.path.expanduser(repo_root))
        rel = os.path.relpath(abs_config, abs_root)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            raise ValueError(
                f"overlay config must live inside the repo ({abs_root}): {host_config}"
            )
        return f"{mount}/{rel}"


class StatusCommand(Command):
    name = "status"
    help = "Show containers, MAAS URL, and overlay state for an environment"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        mode = record.mode if record is not None else Mode.SINGLE
        env.status(args.name, mode, containers)


class LogsCommand(Command):
    name = "logs"
    help = "Tail MAAS service logs from a container"

    # The systemd unit that carries the MAAS logs, per install method.
    _UNITS = {
        InstallType.SNAP: "snap.maas.supervisor",
        InstallType.DEB: "maas-regiond",
    }

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        self.add_container_arg(parser)
        parser.add_argument(
            "--lines",
            type=int,
            default=100,
            metavar="N",
            help="Number of history lines to show before following (default: 100)",
        )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        container = args.container or containers[0]
        install_type = record.install_type if record is not None else InstallType.SNAP
        unit = self._UNITS[install_type]
        log.info("[logs] Tailing %s on %s (Ctrl-C to stop)", unit, container)
        env.lxd.exec_replace(
            container, ["journalctl", "-u", unit, "-f", "-n", str(args.lines)]
        )


class ExecCommand(Command):
    name = "exec"
    help = "Run a command in an environment's containers"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        self.add_container_arg(parser)
        parser.add_argument(
            "--all",
            action="store_true",
            help="Run in every container of the environment",
        )
        # nargs="*" (not REMAINDER, which would swallow this subcommand's own
        # options). dest is not "command": that name is taken by the top-level
        # subparser. Separate the command with `--` so its flags reach it.
        parser.add_argument(
            "argv",
            nargs="*",
            metavar="COMMAND",
            help="Command to run, e.g. `-- systemctl status maas-regiond`",
        )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        if not args.argv:
            log.error("ERROR: COMMAND is required")
            sys.exit(1)
        if args.all and args.container:
            log.error("ERROR: --all and --container are mutually exclusive")
            sys.exit(1)

        _, containers = self.lookup(env.registry, args.name)
        targets = containers if args.all else [args.container or containers[0]]
        failures: list[str] = []
        for container in targets:
            log.info("[exec] %s: %s", container, " ".join(args.argv))
            if env.lxd.exec_passthrough(container, args.argv) != 0:
                failures.append(container)
        if failures:
            log.error("[exec] failed on: %s", ", ".join(failures))
            sys.exit(1)


class ShellCommand(Command):
    name = "shell"
    help = "Open an interactive bash shell in a container"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        self.add_container_arg(parser)

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        _, containers = self.lookup(env.registry, args.name)
        container = args.container or containers[0]
        log.info("[shell] Opening shell in %s", container)
        env.lxd.exec_replace(container, ["bash"])


class CLI:
    COMMANDS: tuple[type[Command], ...] = (
        CreateCommand,
        DestroyCommand,
        ListCommand,
        SyncCommand,
        OverlayCommand,
        StatusCommand,
        LogsCommand,
        ExecCommand,
        ShellCommand,
    )

    def __init__(self) -> None:
        self.commands = {cmd.name: cmd() for cmd in self.COMMANDS}

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Create and destroy MAAS environments in LXD, and drive a "
            "fast edit/sync/overlay inner loop.",
        )
        sub = parser.add_subparsers(dest="command", metavar="COMMAND")
        sub.required = True
        for command in self.commands.values():
            command.configure(sub.add_parser(command.name, help=command.help))
        return parser

    def run(self, argv: list[str] | None = None) -> None:
        argv = list(sys.argv[1:] if argv is None else argv)

        # rsync transport shim: when invoked as the rsync --rsh, re-exec into
        # `lxc exec`. This must run before argparse, which would reject these args.
        if argv and argv[0] == "--rsh-shim":
            exec_argv = self._shim_exec_argv(argv[1:])
            os.execvp(exec_argv[0], exec_argv)
            return  # unreachable: execvp replaces the process image

        parser = self.build_parser()
        if argcomplete is not None:
            argcomplete.autocomplete(parser)
        args = parser.parse_args(argv)

        dry_run = getattr(args, "dry_run", False)
        env = MaasEnv(
            Lxd(dry_run=dry_run),
            SqliteRegistry(dry_run=dry_run),
            dry_run=dry_run,
        )
        self.commands[args.command].execute(args, env)

    @staticmethod
    def _shim_exec_argv(shim_args: list[str]) -> list[str]:
        """Translate rsync's '<container> <remote-cmd...>' into an lxc exec argv.

        Runs the container-side command as the container's ubuntu user so synced
        files keep the ownership maas-install.sh sets on /work.
        """
        container, *remote_cmd = shim_args
        return [
            "lxc",
            "--project",
            LXD_PROJECT,
            "exec",
            "--user",
            str(CONTAINER_UID),
            "--group",
            str(CONTAINER_GID),
            container,
            "--",
            *remote_cmd,
        ]


def main() -> None:
    CLI().run()


if __name__ == "__main__":
    main()

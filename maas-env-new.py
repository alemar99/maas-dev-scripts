#!/usr/bin/env python3

from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Self

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

MAAS_USER_UID = 1000
MAAS_USER_GID = 1000


class Mode(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


class InstallType(str, Enum):
    SNAP = "snap"
    DEB = "deb"


class NodeTarget(str, Enum):
    ALL = "all"
    NODE1 = "node1"
    NODE2 = "node2"
    NODE3 = "node3"


class ScriptTarget(NamedTuple):
    path: str
    target: NodeTarget


@dataclass
class Env:
    name: str
    mode: Mode
    package_type: InstallType
    maas_channel: str | None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> Self | None:
        if row is None:
            return None
        return cls(
            name=row["name"],
            mode=Mode(row["mode"]),
            package_type=InstallType(row["type"]),
            maas_channel=row["maas_channel"],
            created_at=row["created_at"],
        )


@dataclass
class EnvironmentSpec:
    """All parameters needed to create a new MAAS environment."""

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
                        env.mode.value,
                        env.package_type.value,
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
        return [record for row in rows if (record := Env.from_row(row)) is not None]

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
    package_type: InstallType

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
    package_type = InstallType.SNAP
    channel: str

    def invocation(self, db_ip: str | None) -> str:
        return f"{INSTALL_SCRIPT} snap {db_ip} {self.channel}"

    @property
    def registry_channel(self) -> str | None:
        return self.channel


@dataclass
class DebInstall(Install):
    package_type = InstallType.DEB
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

    def _log_dry(self, cmd_args: list[str]) -> None:
        log.info("[dry-run] %s", " ".join(cmd_args))

    def _fail_on_error(self, result: subprocess.CompletedProcess) -> None:
        """Log stderr and exit if the process failed."""
        if result.returncode != 0:
            log.error(result.stderr.strip())
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
        """Run a command inside an LXD container."""
        argv = ["lxc", "exec", container, "--", "sh", "-c", cmd]
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
        """Run a command inside a container and return its stdout."""
        argv = ["lxc", "exec", container, "--", "sh", "-c", cmd]
        if self.dry_run:
            self._log_dry(argv)
            return ""
        result = subprocess.run(argv, text=True, capture_output=True)
        self._fail_on_error(result)
        return result.stdout.strip()

    def is_container_running(self, container: str) -> bool:
        """Check whether a container exists and is running (via `lxc ls`)."""
        result = subprocess.run(
            ["lxc", "ls", "--columns", "s", "--format", "csv", container],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "RUNNING"

    def node_ip(self, container: str) -> str:
        """Return the container's primary IP address."""
        return self.exec_capture(container, "hostname -I | cut -d' ' -f1")

    def create_network(self, name: str) -> None:
        """Create an LXD managed network."""
        network = f"{name}-net"
        log.info("[net] Creating LXD network %s", network)
        result = self.run(["lxc", "network", "create", network], check=True)
        if result.returncode != 0:
            log.info("  (network already exists, skipping)")

    def delete_network(self, name: str) -> None:
        """Delete an LXD managed network."""
        network = f"{name}-net"
        log.info("[net] Deleting LXD network %s", network)
        result = self.run(["lxc", "network", "delete", network], check=False)
        if result.returncode != 0:
            log.warning("  (network may not exist, skipping)")

    def create_containers(
        self,
        containers: list[str],
        ubuntu: str,
        profile: str,
        network: str | None,
    ) -> None:
        """Launch and start LXD containers."""
        image = f"ubuntu:{ubuntu}"
        for container in containers:
            self._init_container(container, image, profile)
            self._set_idmap(container)
            if network:
                self._attach_network(container, network)

        for container in containers:
            log.info("[container] Starting %s", container)
            self.run(["lxc", "start", container])

    def _init_container(self, container: str, image: str, profile: str) -> None:
        log.info("[container] Initializing %s from %s", container, image)
        run_cmd = ["lxc", "init", image, container]
        if self.dry_run:
            self._log_dry(run_cmd + ["<", profile])
            return
        with open(profile, "rb") as profile_file:
            log.info("  $ %s < %s", " ".join(run_cmd), profile)
            result = subprocess.run(
                run_cmd,
                stdin=profile_file,
                text=True,
                capture_output=True,
            )
            self._fail_on_error(result)

    def _set_idmap(self, container: str) -> None:
        if self.dry_run:
            return
        uid = os.getuid()
        gid = os.getgid()
        idmap_input = f"uid {uid} {MAAS_USER_UID}\ngid {gid} {MAAS_USER_GID}\n"
        result = subprocess.run(
            ["lxc", "config", "set", container, "raw.idmap", "-"],
            input=idmap_input,
            text=True,
            capture_output=True,
        )
        self._fail_on_error(result)

    def _attach_network(self, container: str, network: str) -> None:
        self.run(
            ["lxc", "config", "device", "add", container, "eth0", "nic",
             f"network={network}"]
        )

    def delete_containers(self, containers: list[str]) -> None:
        """Stop and delete LXD containers."""
        for container in containers:
            log.info("[container] Stopping %s", container)
            result = self.run(["lxc", "stop", container, "--force"], check=False)
            if result.returncode != 0:
                log.warning("  (container may not exist, skipping)")
        for container in containers:
            log.info("[container] Deleting %s", container)
            result = self.run(["lxc", "delete", container], check=False)
            if result.returncode != 0:
                log.warning("  (container may not exist, skipping)")


class MaasEnv:
    def __init__(self, lxd: Lxd, registry: Registry, dry_run: bool = False) -> None:
        self.lxd = lxd
        self.registry = registry
        self.dry_run = dry_run

    _RESTART_COMMANDS: dict[InstallType, str] = {
        InstallType.DEB: "sudo systemctl restart 'maas-*'",
        InstallType.SNAP: "sudo snap restart maas",
    }

    @staticmethod
    def _overlay_command(
        subcommand: str, config: str, method: InstallType, workdir: str = "/work"
    ) -> str:
        """Build the in-container shell command that runs the overlay tool.

        Runs from `workdir` (must be on the container rootfs) so the tool's
        relative `.overlayfs_workdir` shares a filesystem with the
        `/work/src/...` upperdirs. `method` selects the package section:
        '--snap' (default) or '--deb'.
        """
        return (
            f"cd {workdir} && python3 {OVERLAY_SCRIPT} {subcommand} "
            f"--config {config} --{method.value}"
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
        dest_spec = f"{container}:{dest.rstrip('/')}/"
        cmd += [source, dest_spec]
        return cmd

    def setup_postgres(self, container: str) -> str:
        """Install and configure PostgreSQL on a container. Return its IP."""
        log.info("[postgres] Installing PostgreSQL on %s", container)
        self.lxd.exec(container, "/scripts/postgres-setup.sh")
        db_ip = self.lxd.exec_capture(container, "hostname -I | cut -d' ' -f1")
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
        log.info("[%s] Installing and initializing MAAS on all nodes", install.package_type.value)
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

    def _resolve_target_containers(
        self,
        script: ScriptTarget,
        containers: list[str],
        label: str,
    ) -> list[str] | None:
        """Return the containers for a script target, or None if the target is unavailable."""
        match script.target:
            case NodeTarget.ALL:
                return list(containers)
            case NodeTarget.NODE1:
                return [containers[0]]
            case NodeTarget.NODE2:
                if len(containers) >= 2:
                    return [containers[1]]
            case NodeTarget.NODE3:
                if len(containers) >= 3:
                    return [containers[2]]
            case _:
                log.error(
                    "  [%s] unknown target '%s' — skipping", label, script.target
                )
                return None

        log.warning(
            "  [%s] skipping %s:%s (container does not exist)",
            label,
            script.path,
            script.target.value,
        )
        return None

    def run_scripts_strict(
        self,
        containers: list[str],
        scripts: list[ScriptTarget],
        label: str,
    ) -> None:
        """Run scripts on specified containers, aborting on first failure."""
        self._run_scripts(containers, scripts, label, abort_on_failure=True)

    def run_scripts_lenient(
        self,
        containers: list[str],
        scripts: list[ScriptTarget],
        label: str,
    ) -> None:
        """Run scripts on specified containers, logging failures and continuing."""
        self._run_scripts(containers, scripts, label, abort_on_failure=False)

    def _run_scripts(
        self,
        containers: list[str],
        scripts: list[ScriptTarget],
        label: str,
        *,
        abort_on_failure: bool,
    ) -> None:
        if not scripts:
            return

        for script in scripts:
            target_containers = self._resolve_target_containers(script, containers, label)
            if target_containers is None:
                continue

            for container in target_containers:
                log.info("  [%s] running %s on %s", label, script.path, container)
                try:
                    result = self.lxd.exec(container, script.path, check=abort_on_failure)
                    if not abort_on_failure and result.returncode != 0:
                        log.warning(
                            "  [%s] WARNING: %s on %s failed (exit code %d)",
                            label,
                            script.path,
                            container,
                            result.returncode,
                        )
                except SystemExit:
                    raise

    def create(self, spec: EnvironmentSpec, install: Install) -> None:
        network = f"{spec.name}-net"

        log.info(
            "=== Creating MAAS environment: %s (%s, %d nodes) ===",
            spec.name,
            spec.mode.value,
            len(containers := self._containers_for(spec.name, spec.mode)),
        )

        self.lxd.create_network(spec.name)

        profile_path = Path(spec.profile)
        if not profile_path.exists():
            log.error("ERROR: profile not found: %s", spec.profile)
            sys.exit(1)

        self.lxd.create_containers(containers, spec.ubuntu, str(profile_path), network)

        self.registry.add(
            Env(
                name=spec.name,
                mode=spec.mode,
                package_type=install.package_type,
                maas_channel=install.registry_channel,
            )
        )

        log.info("[init] Waiting for cloud-init to finish on all nodes...")
        for container in containers:
            self.lxd.exec(container, "cloud-init status --wait > /dev/null 2>&1 || true")

        self.run_scripts_strict(containers, spec.pre_scripts, "pre-install")

        if install.provisions_own_db:
            self.install_maas(containers, install)
            maas_ip = self.lxd.node_ip(containers[0])
        else:
            db_ip = self.setup_postgres(containers[0])
            self.install_maas(containers, install, db_ip=db_ip)
            maas_ip = db_ip

        self.create_admin(containers[0])

        self.run_scripts_lenient(containers, spec.post_scripts, "post-install")

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
        for record in envs:
            log.info(
                "%-20s %-8s %-6s %-16s %s",
                record.name,
                record.mode.value,
                record.package_type.value,
                record.maas_channel or "-",
                record.created_at,
            )

    def sync(self, containers: list[str], source: str, dest: str) -> None:
        """Rsync a host source dir into each container's dest dir via the shim."""
        log.info(
            "=== Syncing %s -> %d container(s) at %s ===",
            source,
            len(containers),
            dest,
        )
        rsh = f"{sys.executable} {Path(__file__).resolve()} --rsh-shim"
        failures: list[str] = []

        for container in containers:
            if not self.dry_run and not self.lxd.is_container_running(container):
                log.warning("  [sync] %s is missing or not running — skipping", container)
                failures.append(container)
                continue

            cmd = self._rsync_command(source, container, dest, rsh)
            result = self.lxd.run(cmd, check=False)
            if not self.dry_run and result.returncode != 0:
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
            "=== %s %d container(s) (config %s) ===",
            verb,
            len(containers),
            config,
        )
        cmd = self._overlay_command(subcommand, config, method)
        failures: list[str] = []

        for container in containers:
            if not self.dry_run and not self.lxd.is_container_running(container):
                log.warning("  [overlay] %s is missing or not running — skipping", container)
                failures.append(container)
                continue

            result = self.lxd.exec(container, cmd, check=False)
            if not self.dry_run and result.returncode != 0:
                log.warning(
                    "  [overlay] %s failed on %s (exit code %d)",
                    subcommand,
                    container,
                    result.returncode,
                )
                failures.append(container)
                continue

            restart = self.lxd.exec(container, self._restart_command(method), check=False)
            if not self.dry_run and restart.returncode != 0:
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

    @staticmethod
    def _containers_for(name: str, mode: Mode) -> list[str]:
        if mode == Mode.SINGLE:
            return [name]
        return [f"{name}-{i}" for i in (1, 2, 3)]


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

    @classmethod
    def lookup(cls, registry: Registry, name: str) -> tuple[Env | None, list[str]]:
        """Fetch a tracked env's record and container list, logging the outcome.

        Falls back to a single 'snap' environment when the name is untracked.
        """
        record = registry.get(name)
        if record is not None:
            log.info(
                "[registry] using recorded settings for '%s' (mode=%s, type=%s)",
                name,
                record.mode.value,
                record.package_type.value,
            )
            mode = record.mode
        else:
            log.warning(
                "[registry] '%s' is not tracked; assuming mode=single, type=snap",
                name,
            )
            mode = Mode.SINGLE
        return record, MaasEnv._containers_for(name, mode)


class CreateCommand(Command):
    name = "create"
    help = "Create and initialise a MAAS environment"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        installs = parser.add_subparsers(dest="install_type", metavar="INSTALL_TYPE")
        installs.required = True

        p_snap = installs.add_parser("snap", help="Install MAAS from the snap")
        self._add_create_args(p_snap)
        p_snap.add_argument(
            "--mode",
            choices=[m.value for m in Mode],
            default=Mode.SINGLE.value,
            help="Deployment mode (default: single). Recorded in the registry "
            "and reused by later commands for this name.",
        )
        p_snap.add_argument(
            "--channel",
            default="latest/edge",
            help="MAAS snap channel to install (default: latest/edge)",
        )

        p_deb = installs.add_parser(
            "deb",
            help="Install MAAS from a deb/PPA (single-node only)",
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
        install = self._install_from_args(args)
        mode = Mode(getattr(args, "mode", Mode.SINGLE.value))
        spec = EnvironmentSpec(
            name=args.name,
            mode=mode,
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
        )
        env.create(spec, install)

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
        if args.install_type == InstallType.DEB.value:
            return DebInstall(ppa=args.ppa, branch=args.branch)
        return SnapInstall(channel=args.channel)

    @staticmethod
    def _parse_script_arg(raw: str) -> ScriptTarget:
        """Parse 'path:target' into a ScriptTarget. Used as an argparse type."""
        if ":" not in raw:
            raise argparse.ArgumentTypeError(
                f"invalid script spec '{raw}': expected 'path:target' "
                f"(target is 'all', 'node1', 'node2', or 'node3')"
            )
        path, target_str = raw.rsplit(":", 1)
        valid_targets = {t.value for t in NodeTarget}
        if target_str not in valid_targets:
            raise argparse.ArgumentTypeError(
                f"invalid target '{target_str}': must be one of {sorted(valid_targets)}"
            )
        return ScriptTarget(path=path, target=NodeTarget(target_str))


class DestroyCommand(Command):
    name = "destroy"
    help = "Stop and delete containers and the LXD network"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        _, containers = self.lookup(env.registry, args.name)
        env.destroy(args.name, containers)


class ListCommand(Command):
    name = "list"
    help = "List environments recorded in the local registry"

    def configure(self, parser: argparse.ArgumentParser) -> None:
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

    def configure(self, parser: argparse.ArgumentParser) -> None:
        actions = parser.add_subparsers(dest="overlay_action", metavar="ACTION")
        actions.required = True
        for action_name, action_help in self._ACTIONS:
            p = actions.add_parser(action_name, help=action_help)
            self.add_env_args(p)
            p.add_argument(
                "--config",
                required=True,
                metavar="PATH",
                help="Path to the overlay config YAML (must live inside this repo)",
            )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        config = self._config_in_container(args.config)
        record, containers = self.lookup(env.registry, args.name)
        method = record.package_type if record is not None else InstallType.SNAP
        subcommand = "sync" if args.overlay_action == "apply" else "unsync"
        env.overlay(containers, subcommand, config, method)

    @classmethod
    def _config_in_container(cls, config_arg: str) -> str:
        """Validate the host overlay config and map it to its in-container path."""
        host_config = os.path.expanduser(config_arg)
        if not os.path.isfile(host_config):
            log.error("ERROR: overlay config not found or not a file: %s", config_arg)
            sys.exit(1)
        repo_root = str(Path(__file__).resolve().parent)
        try:
            return cls._map_into_repo(host_config, repo_root)
        except ValueError as exc:
            log.error("ERROR: %s", exc)
            sys.exit(1)

    @staticmethod
    def _map_into_repo(
        host_config: str, repo_root: str, mount: str = "/scripts"
    ) -> str:
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


class CLI:
    COMMANDS: tuple[type[Command], ...] = (
        CreateCommand,
        DestroyCommand,
        ListCommand,
        SyncCommand,
        OverlayCommand,
    )

    def __init__(self) -> None:
        self.commands = {cmd.name: cmd() for cmd in self.COMMANDS}

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Create/destroy MAAS test environments in LXD",
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

        args = self.build_parser().parse_args(argv)

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

        Runs the container-side command as uid/gid 1000 (ubuntu) so synced files
        keep the ownership maas-install.sh sets on /work.
        """
        container = shim_args[0]
        remote_cmd = shim_args[1:]
        return [
            "lxc",
            "exec",
            "--user",
            str(MAAS_USER_UID),
            "--group",
            str(MAAS_USER_GID),
            container,
            "--",
            *remote_cmd,
        ]


def main() -> None:
    CLI().run()


if __name__ == "__main__":
    main()

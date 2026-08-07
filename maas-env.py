#!/usr/bin/env python3

from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
from collections.abc import Generator
from contextlib import contextmanager
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NamedTuple, Self

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


class ScriptTarget(NamedTuple):
    path: str
    target: str  # "all", "node1", "node2", "node3"


@dataclass
class Env:
    name: str
    mode: Literal["single", "multi"]
    type_: Literal["snap", "deb"]
    maas_channel: str | None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> Self | None:
        if row is None:
            return None
        return cls(
            name=row["name"],
            mode=row["mode"],
            type_=row["type"],
            maas_channel=row["maas_channel"],
            created_at=row["created_at"],
        )


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
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
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
        """Open (creating if needed) the registry DB and ensure the schema exists."""
        conn = sqlite3.connect(self.path())
        conn.row_factory = sqlite3.Row
        yield conn.cursor()
        conn.commit()
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
                        env.mode,
                        env.type_,
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
    type_: Literal["snap", "deb"]

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
    type_ = "snap"
    channel: str

    def invocation(self, db_ip: str | None) -> str:
        return f"{INSTALL_SCRIPT} snap {db_ip} {self.channel}"

    @property
    def registry_channel(self) -> str | None:
        return self.channel


@dataclass
class DebInstall(Install):
    type_ = "deb"
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
    def _echo_dry(cmd_args: list[str]) -> None:
        log.info("[dry-run] %s", " ".join(cmd_args))

    def run(self, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        """Run a host command. If dry_run, print it instead."""
        if self.dry_run:
            self._echo_dry(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        log.info("  $ %s", " ".join(cmd))
        result = subprocess.run(cmd, text=True, capture_output=True)
        if check and result.returncode != 0:
            log.error(result.stderr.strip())
            sys.exit(1)
        return result

    def exec(
        self, container: str, cmd: str, *, check: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a command inside an LXD container."""
        argv = ["lxc", "exec", container, "--", "sh", "-c", cmd]
        if self.dry_run:
            self._echo_dry(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        log.info("  [%s] $ %s", container, cmd)
        result = subprocess.run(argv, text=True, capture_output=True)
        if check and result.returncode != 0:
            log.error(result.stderr.strip())
            sys.exit(1)
        if result.stdout.strip():
            log.info(result.stdout.strip())
        return result

    def exec_capture(self, container: str, cmd: str) -> str:
        """Run a command inside a container and return its stdout."""
        argv = ["lxc", "exec", container, "--", "sh", "-c", cmd]
        if self.dry_run:
            self._echo_dry(argv)
            return ""
        result = subprocess.run(argv, text=True, capture_output=True)
        if result.returncode != 0:
            log.error(result.stderr.strip())
            sys.exit(1)
        return result.stdout.strip()

    def is_container_running(self, container: str) -> bool:
        """Check whether a container exists and is running (via `lxc info`)."""
        result = subprocess.run(
            ["lxc", "ls", "--columns", "s", "--format", "csv", container],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            return False
        return True if result.stdout.strip() == "RUNNING" else False

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

        for c in containers:
            log.info("[container] Initializing %s from %s", c, image)
            with open(profile, "rb") as f:
                run_cmd = ["lxc", "init", image, c]
                if self.dry_run:
                    self._echo_dry(run_cmd + ["<", profile])
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
            if not self.dry_run:
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
                self.run(
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
            self.run(["lxc", "start", c])

    def delete_containers(self, containers: list[str]) -> None:
        """Stop and delete LXD containers."""
        for c in containers:
            log.info("[container] Stopping %s", c)
            result = self.run(["lxc", "stop", c, "--force"], check=False)
            if result.returncode != 0:
                log.warning("  (container may not exist, skipping)")
        for c in containers:
            log.info("[container] Deleting %s", c)
            result = self.run(["lxc", "delete", c], check=False)
            if result.returncode != 0:
                log.warning("  (container may not exist, skipping)")


class MaasEnv:
    def __init__(self, lxd: Lxd, registry: Registry, dry_run: bool = False) -> None:
        self.lxd = lxd
        self.registry = registry
        self.dry_run = dry_run

    @staticmethod
    def _overlay_command(
        subcommand: str, config: str, method: str, workdir: str = "/work"
    ) -> str:
        """Build the in-container shell command that runs the overlay tool.

        Runs from `workdir` (must be on the container rootfs) so the tool's
        relative `.overlayfs_workdir` shares a filesystem with the
        `/work/src/...` upperdirs. `method` selects the package section:
        '--snap' (default) or '--deb'.
        """
        return (
            f"cd {workdir} && python3 {OVERLAY_SCRIPT} {subcommand} "
            f"--config {config} --{method}"
        )

    @staticmethod
    def _restart_command(method: str) -> str:
        """Command to restart MAAS after (un)overlaying, per install method."""
        if method == "deb":
            return "sudo systemctl restart 'maas-*'"
        return "sudo snap restart maas"

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
        log.info("[%s] Installing and initializing MAAS on all nodes", install.type_)
        cmd = install.invocation(db_ip)
        for c in containers:
            self.lxd.exec(c, cmd)

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

    def run_scripts(
        self,
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
                    result = self.lxd.exec(c, st.path, check=abort_on_failure)
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

    def create(
        self,
        name: str,
        mode: str,
        containers: list[str],
        ubuntu: str,
        profile: str,
        pre_scripts: list[ScriptTarget],
        post_scripts: list[ScriptTarget],
        install: Install,
    ) -> None:
        network = f"{name}-net"

        log.info(
            "=== Creating MAAS environment: %s (%s, %d nodes) ===",
            name,
            mode,
            len(containers),
        )

        self.lxd.create_network(name)

        profile_path = Path(profile)
        if not profile_path.exists():
            log.error("ERROR: profile not found: %s", profile)
            sys.exit(1)

        self.lxd.create_containers(containers, ubuntu, str(profile_path), network)

        self.registry.add(
            Env(
                name=name,
                mode=mode,
                type_=install.type_,
                maas_channel=install.registry_channel,
            )
        )

        log.info("[init] Waiting for cloud-init to finish on all nodes...")
        for c in containers:
            self.lxd.exec(c, "cloud-init status --wait > /dev/null 2>&1 || true")

        self.run_scripts(containers, pre_scripts, "pre-install", abort_on_failure=True)

        if install.provisions_own_db:
            self.install_maas(containers, install)
            maas_ip = self.lxd.node_ip(containers[0])
        else:
            db_ip = self.setup_postgres(containers[0])
            self.install_maas(containers, install, db_ip=db_ip)
            maas_ip = db_ip

        self.create_admin(containers[0])

        self.run_scripts(
            containers, post_scripts, "post-install", abort_on_failure=False
        )

        log.info("=== MAAS environment '%s' ready ===", name)
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
        for e in envs:
            log.info(
                "%-20s %-8s %-6s %-16s %s",
                e.name,
                e.mode,
                e.type_,
                e.maas_channel or "-",
                e.created_at,
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

        for c in containers:
            if not self.dry_run and not self.lxd.is_container_running(c):
                log.warning("  [sync] %s is missing or not running — skipping", c)
                failures.append(c)
                continue

            cmd = self._rsync_command(source, c, dest, rsh)
            result = self.lxd.run(cmd, check=False)
            if not self.dry_run and result.returncode != 0:
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
        self,
        containers: list[str],
        subcommand: str,
        config: str,
        method: str = "snap",
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

        for c in containers:
            if not self.dry_run and not self.lxd.is_container_running(c):
                log.warning("  [overlay] %s is missing or not running — skipping", c)
                failures.append(c)
                continue

            result = self.lxd.exec(c, cmd, check=False)
            if not self.dry_run and result.returncode != 0:
                log.warning(
                    "  [overlay] %s failed on %s (exit code %d)",
                    subcommand,
                    c,
                    result.returncode,
                )
                failures.append(c)
                continue

            restart = self.lxd.exec(c, self._restart_command(method), check=False)
            if not self.dry_run and restart.returncode != 0:
                log.warning(
                    "  [overlay] MAAS restart failed on %s (exit code %d)",
                    c,
                    restart.returncode,
                )
                failures.append(c)
                continue

            log.info("  [overlay] %s done", c)

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
    def containers_for(name: str, mode: str) -> list[str]:
        """Return the container names for a given base name and mode."""
        if mode == "single":
            return [name]
        return [f"{name}-{i}" for i in (1, 2, 3)]

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
                record.mode,
                record.type_,
            )
            mode = record.mode
        else:
            log.warning(
                "[registry] '%s' is not tracked; assuming mode=single, type=snap",
                name,
            )
            mode = "single"
        return record, cls.containers_for(name, mode)


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
            choices=["single", "multi"],
            default="single",
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
        # deb is single-node only, so its subparser omits --mode.
        mode = getattr(args, "mode", "single")
        env.create(
            name=args.name,
            mode=mode,
            containers=self.containers_for(args.name, mode),
            ubuntu=args.ubuntu,
            profile=args.profile,
            pre_scripts=args.pre,
            post_scripts=args.post,
            install=install,
        )

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
        if args.install_type == "deb":
            return DebInstall(ppa=args.ppa, branch=args.branch)
        return SnapInstall(channel=args.channel)

    @staticmethod
    def _parse_script_arg(raw: str) -> ScriptTarget:
        """Parse 'path:target' into (path, target). Used as an argparse type."""
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
        if not env.dry_run and not args.yes:
            answer = input(
                f"Destroy environment '{args.name}'? "
                "This will delete all containers and the LXD network. [y/N] "
            )
            if answer.strip().lower() != "y":
                log.info("Aborted.")
                return
        env.destroy(args.name, containers)


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

    def configure(self, parser: argparse.ArgumentParser) -> None:
        actions = parser.add_subparsers(dest="overlay_action", metavar="ACTION")
        actions.required = True
        for action_name, action_help in self._ACTIONS:
            p = actions.add_parser(action_name, help=action_help)
            self.add_env_args(p)
            p.add_argument(
                "--config",
                required=False,
                default=None,
                metavar="PATH",
                help="Path to the overlay config YAML (must live inside this repo). "
                "Auto-selected from the environment's MAAS channel when omitted.",
            )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        method = record.type_ if record is not None else "snap"
        repo_root = str(Path(__file__).resolve().parent)
        if args.config and args.config.strip():
            config = self._config_in_container(args.config)
        else:
            channel = record.maas_channel if record is not None else None
            if not channel:
                log.error(
                    "ERROR: --config is required (environment has no recorded channel)"
                )
                sys.exit(1)
            try:
                host_config = self._channel_to_overlay_config(channel, repo_root)
                log.info("[overlay] Auto-selected config: %s", host_config)
            except ValueError as exc:
                log.error("ERROR: %s", exc)
                sys.exit(1)
            config = self._map_into_repo(host_config, repo_root)
        subcommand = "sync" if args.overlay_action == "apply" else "unsync"
        env.overlay(containers, subcommand, config, method)

    @staticmethod
    def _channel_to_overlay_config(channel: str, repo_root: str) -> str:
        """Map a MAAS channel string to the appropriate overlay config file path.

        Rules:
        - Channel starting with '3.7' → overlay-config-37.yaml
        - Channel starting with 'master', 'main', or 'latest' → overlay-config-master.yaml
        - Otherwise raise ValueError with a helpful message.
        """
        prefix = channel.split("/")[0].strip()
        if prefix == "3.7":
            name = "overlay-config-37.yaml"
        elif prefix in ("master", "main", "latest"):
            name = "overlay-config-master.yaml"
        else:
            raise ValueError(
                f"Cannot auto-select overlay config for channel '{channel}'. "
                f"Pass --config explicitly."
            )
        path = os.path.join(repo_root, name)
        if not os.path.isfile(path):
            raise ValueError(
                f"Auto-selected overlay config '{name}' for channel '{channel}' "
                f"does not exist at {path}. Pass --config explicitly."
            )
        return path

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


class StatusCommand(Command):
    name = "status"
    help = "Show containers, MAAS URL, and overlay state for an environment"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        log.info("Environment : %s", args.name)
        log.info("Mode        : %s", record.mode if record else "single")
        log.info("")
        for c in containers:
            running = env.lxd.is_container_running(c)
            status_str = "RUNNING" if running else "STOPPED"
            log.info("  Container : %s  [%s]", c, status_str)
            if running:
                result = subprocess.run(
                    ["lxc", "exec", c, "--", "hostname", "-I"],
                    text=True, capture_output=True,
                )
                ip = result.stdout.strip().split()[0] if result.stdout.strip() else "unknown"
                log.info("  MAAS URL  : http://%s:5240/MAAS", ip)
                ov = subprocess.run(
                    ["lxc", "exec", c, "--", "sh", "-c",
                     "mount 2>/dev/null | grep -q overlay && echo applied || echo not-applied"],
                    text=True, capture_output=True,
                )
                overlay_state = ov.stdout.strip() if ov.returncode == 0 else "unknown"
                log.info("  Overlay   : %s", overlay_state)
            log.info("")


class LogsCommand(Command):
    name = "logs"
    help = "Tail MAAS service logs from the primary container"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        parser.add_argument("--container", default=None, help="Target a specific container")
        parser.add_argument(
            "--deb", action="store_true",
            help="Use deb service name (maas-regiond) instead of snap",
        )

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        container = args.container or containers[0]
        unit = "maas-regiond" if args.deb else "snap.maas.supervisor"
        log.info("[logs] Tailing %s on %s (Ctrl-C to stop)", unit, container)
        os.execvp(
            "lxc",
            ["lxc", "exec", container, "--",
             "journalctl", "-u", unit, "-f", "-n", "100"],
        )


class ExecCommand(Command):
    name = "exec"
    help = "Run a command in the primary container of an environment"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        parser.add_argument("--container", default=None, help="Target a specific container")
        parser.add_argument(
            "--all", action="store_true",
            help="Run in all containers (multi mode)",
        )
        parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run")

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        if not args.command:
            log.error("ERROR: COMMAND is required")
            sys.exit(1)
        targets = containers if args.all else [args.container or containers[0]]
        failed = False
        for c in targets:
            log.info("[exec] %s: %s", c, " ".join(args.command))
            result = subprocess.run(["lxc", "exec", c, "--"] + args.command)
            if result.returncode != 0:
                failed = True
        if failed:
            sys.exit(1)


class ShellCommand(Command):
    name = "shell"
    help = "Open an interactive bash shell in the primary container"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        self.add_env_args(parser)
        parser.add_argument("--container", default=None, help="Target a specific container")

    def execute(self, args: argparse.Namespace, env: MaasEnv) -> None:
        record, containers = self.lookup(env.registry, args.name)
        container = args.container or containers[0]
        log.info("[shell] Opening shell in %s", container)
        os.execvp("lxc", ["lxc", "exec", container, "--", "bash"])


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
            description="Create and destroy MAAS environments in LXD, and drive a fast edit→sync→overlay inner loop.",
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

        Runs the container-side command as uid/gid 1000 (ubuntu) so synced files
        keep the ownership maas-install.sh sets on /work.
        """
        container = shim_args[0]
        remote_cmd = shim_args[1:]
        return [
            "lxc",
            "exec",
            "--user",
            "1000",
            "--group",
            "1000",
            container,
            "--",
            *remote_cmd,
        ]


def main() -> None:
    CLI().run()


if __name__ == "__main__":
    main()

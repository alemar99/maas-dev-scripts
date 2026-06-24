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

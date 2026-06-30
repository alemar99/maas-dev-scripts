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


class TestNormalizeSource(unittest.TestCase):
    def test_adds_single_trailing_slash(self):
        self.assertEqual(maas_env.normalize_source("/tmp/foo"), "/tmp/foo/")

    def test_collapses_existing_trailing_slash(self):
        self.assertEqual(maas_env.normalize_source("/tmp/foo/"), "/tmp/foo/")

    def test_expands_user(self):
        result = maas_env.normalize_source("~/foo")
        self.assertTrue(result.startswith(os.path.expanduser("~")))
        self.assertTrue(result.endswith("/foo/"))


class TestBuildRshValue(unittest.TestCase):
    def test_builds_rsh_string(self):
        self.assertEqual(
            maas_env.build_rsh_value("/usr/bin/python3", "/x/maas-env.py"),
            "/usr/bin/python3 /x/maas-env.py --rsh-shim",
        )


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


class TestBuildShimExecArgv(unittest.TestCase):
    def test_builds_lxc_exec_argv(self):
        self.assertEqual(
            maas_env.build_shim_exec_argv(["c1", "rsync", "--server", "x"]),
            [
                "lxc", "exec", "--user", "1000", "--group", "1000",
                "c1", "--", "rsync", "--server", "x",
            ],
        )


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


class TestSourceResolvesToRoot(unittest.TestCase):
    def test_literal_root(self):
        self.assertTrue(maas_env.source_resolves_to_root("/"))

    def test_dot_and_dotdot_paths_resolve_to_root(self):
        for raw in ("/.", "/..", "/home/.."):
            self.assertTrue(maas_env.source_resolves_to_root(raw), msg=raw)

    def test_normal_directory_is_not_root(self):
        self.assertFalse(maas_env.source_resolves_to_root("/tmp"))
        self.assertFalse(maas_env.source_resolves_to_root("."))


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


class TestExcludes(unittest.TestCase):
    def test_excludes_overlayfs_workdir(self):
        self.assertIn(".overlayfs_workdir", maas_env.EXCLUDES)

    def test_keeps_sync_excludes(self):
        for pattern in (".git", "__pycache__", "*.pyc"):
            self.assertIn(pattern, maas_env.EXCLUDES)


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


if __name__ == "__main__":
    unittest.main()

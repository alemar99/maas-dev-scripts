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


if __name__ == "__main__":
    unittest.main()

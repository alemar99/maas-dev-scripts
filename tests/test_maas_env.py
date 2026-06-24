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

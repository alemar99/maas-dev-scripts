import argparse
import importlib.util
import os
import sys
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "maas-env.py"
_spec = importlib.util.spec_from_file_location("maas_env", _MODULE_PATH)
maas_env = importlib.util.module_from_spec(_spec)
# Register before executing: dataclasses resolves annotations via sys.modules.
sys.modules["maas_env"] = maas_env
_spec.loader.exec_module(maas_env)

Mode = maas_env.Mode
InstallType = maas_env.InstallType
NodeTarget = maas_env.NodeTarget


class TestContainersFor(unittest.TestCase):
    def test_single_mode_is_the_bare_name(self):
        self.assertEqual(maas_env.containers_for("foo", Mode.SINGLE), ["foo"])

    def test_multi_mode_is_three_numbered_nodes(self):
        self.assertEqual(
            maas_env.containers_for("foo", Mode.MULTI),
            ["foo-1", "foo-2", "foo-3"],
        )


class TestInstallInvocation(unittest.TestCase):
    def test_snap_form(self):
        install = maas_env.SnapInstall(channel="3.7/edge")
        self.assertEqual(
            install.invocation("10.0.0.5"),
            "/scripts/maas-install.sh snap 10.0.0.5 3.7/edge",
        )

    def test_snap_needs_a_separate_db_and_records_its_channel(self):
        install = maas_env.SnapInstall(channel="3.7/edge")
        self.assertFalse(install.provisions_own_db)
        self.assertEqual(install.registry_channel, "3.7/edge")

    def test_deb_form(self):
        install = maas_env.DebInstall(ppa="ppa:maas/3.7", branch="3.7")
        self.assertEqual(
            install.invocation(None),
            "/scripts/maas-install.sh deb ppa:maas/3.7 3.7",
        )

    def test_deb_brings_its_own_db_and_has_no_channel(self):
        install = maas_env.DebInstall(ppa="ppa:maas/3.7", branch="3.7")
        self.assertTrue(install.provisions_own_db)
        self.assertIsNone(install.registry_channel)


class TestNetworkInfo(unittest.TestCase):
    def test_host_ip_places_containers_above_the_gateway(self):
        info = maas_env.NetworkInfo(name="t-net", gateway="10.20.30.1", prefixlen=24)
        self.assertEqual(info.host_ip(0), "10.20.30.10")
        self.assertEqual(info.host_ip(1), "10.20.30.11")
        self.assertEqual(info.host_ip(2), "10.20.30.12")

    def test_host_ip_is_derived_from_the_network_base_not_the_gateway(self):
        # Gateway happens to be .1, but host IPs are offset from the network
        # base address (.0 for a /24), so index 0 lands on .10.
        info = maas_env.NetworkInfo(name="t-net", gateway="192.168.5.1", prefixlen=24)
        self.assertEqual(info.host_ip(0), "192.168.5.10")


class TestRenderNetworkConfig(unittest.TestCase):
    def _render(self, ip="10.0.0.10", prefixlen=24, gateway="10.0.0.1"):
        return maas_env.Lxd._render_network_config(ip, prefixlen, gateway)

    def test_declares_the_static_address_with_prefix(self):
        self.assertIn("- 10.0.0.10/24", self._render())

    def test_routes_default_via_the_gateway(self):
        rendered = self._render()
        self.assertIn("to: default", rendered)
        self.assertIn("via: 10.0.0.1", rendered)

    def test_nameserver_points_at_the_gateway(self):
        # LXD's dnsmasq keeps forwarding DNS on the gateway with DHCP disabled.
        self.assertIn("- 10.0.0.1", self._render().split("nameservers")[1])

    def test_is_cloud_init_v2_format(self):
        self.assertTrue(self._render().startswith("version: 2\n"))


class TestRsyncCommand(unittest.TestCase):
    def _command(self, source="/src/", container="c1", dest="/work"):
        return maas_env.MaasEnv._rsync_command(source, container, dest, "RSH")

    def test_archive_and_delete_flags(self):
        cmd = self._command()
        self.assertEqual(cmd[0], "rsync")
        for flag in ("-a", "--no-owner", "--no-group", "--delete"):
            self.assertIn(flag, cmd)

    def test_uses_the_shim_as_transport(self):
        cmd = self._command()
        self.assertEqual(cmd[cmd.index("-e") + 1], "RSH")

    def test_excludes_are_passed_through(self):
        cmd = self._command()
        for pattern in maas_env.EXCLUDES:
            self.assertIn(pattern, cmd)

    def test_source_and_destination_are_the_final_operands(self):
        cmd = self._command(source="/src/", container="c1", dest="/work")
        self.assertEqual(cmd[-2:], ["/src/", "c1:/work/"])

    def test_destination_trailing_slash_is_normalised(self):
        cmd = self._command(dest="/work/")
        self.assertEqual(cmd[-1], "c1:/work/")


class TestShimExecArgv(unittest.TestCase):
    def test_runs_the_remote_command_as_the_container_user(self):
        self.assertEqual(
            maas_env.CLI._shim_exec_argv(["c1", "rsync", "--server", "x"]),
            [
                "lxc",
                "--project",
                maas_env.LXD_PROJECT,
                "exec",
                "--user",
                str(maas_env.CONTAINER_UID),
                "--group",
                str(maas_env.CONTAINER_GID),
                "c1",
                "--",
                "rsync",
                "--server",
                "x",
            ],
        )


class TestSyncSourceValidation(unittest.TestCase):
    def test_adds_a_single_trailing_slash(self):
        for raw in ("/tmp/foo", "/tmp/foo/", "/tmp/foo//"):
            self.assertEqual(
                maas_env.SyncCommand._normalize_source(raw), "/tmp/foo/", msg=raw
            )

    def test_expands_tilde(self):
        result = maas_env.SyncCommand._normalize_source("~/foo")
        self.assertFalse(result.startswith("~"))
        self.assertEqual(result, os.path.expanduser("~/foo") + "/")

    def test_detects_paths_resolving_to_root(self):
        for raw in ("/", "/.", "/..", "/home/.."):
            self.assertTrue(maas_env.SyncCommand._resolves_to_root(raw), msg=raw)

    def test_ordinary_paths_are_not_root(self):
        for raw in ("/tmp", "."):
            self.assertFalse(maas_env.SyncCommand._resolves_to_root(raw), msg=raw)


class TestOverlayConfigPath(unittest.TestCase):
    def test_maps_a_repo_relative_config_into_the_bind_mount(self):
        self.assertEqual(
            maas_env.OverlayCommand._map_into_repo(
                "/repo/overlay-config-37.yaml", "/repo"
            ),
            "/scripts/overlay-config-37.yaml",
        )

    def test_preserves_subdirectories(self):
        self.assertEqual(
            maas_env.OverlayCommand._map_into_repo("/repo/configs/x.yaml", "/repo"),
            "/scripts/configs/x.yaml",
        )

    def test_rejects_configs_outside_the_repo(self):
        with self.assertRaises(ValueError):
            maas_env.OverlayCommand._map_into_repo("/etc/x.yaml", "/repo")

    def test_rejects_traversal_out_of_the_repo(self):
        with self.assertRaises(ValueError):
            maas_env.OverlayCommand._map_into_repo("/repo/../x.yaml", "/repo")


class TestOverlayConfigForChannel(unittest.TestCase):
    _REPO_ROOT = str(_MODULE_PATH.parent)

    def _config_for(self, channel):
        return maas_env.OverlayCommand._config_for_channel(channel, self._REPO_ROOT)

    def test_37_channels_select_the_37_config(self):
        for channel in ("3.7", "3.7/edge", "3.7/stable"):
            self.assertTrue(
                self._config_for(channel).endswith("overlay-config-37.yaml"),
                msg=channel,
            )

    def test_38_channels_select_the_38_config(self):
        for channel in ("3.8", "3.8/edge", "3.8/stable"):
            self.assertTrue(
                self._config_for(channel).endswith("overlay-config-38.yaml"),
                msg=channel,
            )

    def test_tip_channels_select_the_master_config(self):
        for channel in ("latest/edge", "latest", "latest/stable"):
            self.assertTrue(
                self._config_for(channel).endswith("overlay-config-master.yaml"),
                msg=channel,
            )

    def test_unknown_channel_raises(self):
        with self.assertRaises(ValueError):
            self._config_for("2.9/stable")

    def test_missing_config_file_raises(self):
        with self.assertRaises(ValueError):
            maas_env.OverlayCommand._config_for_channel("3.7/edge", "/nonexistent")


class TestOverlayCommandBuilders(unittest.TestCase):
    def test_runs_from_the_container_rootfs_workdir(self):
        cmd = maas_env.MaasEnv._overlay_command(
            "sync", "/scripts/c.yaml", InstallType.SNAP
        )
        self.assertTrue(cmd.startswith("cd /work && "))

    def test_snap_and_deb_select_their_config_section(self):
        snap = maas_env.MaasEnv._overlay_command(
            "sync", "/scripts/c.yaml", InstallType.SNAP
        )
        deb = maas_env.MaasEnv._overlay_command(
            "sync", "/scripts/c.yaml", InstallType.DEB
        )
        self.assertTrue(snap.endswith("--snap"))
        self.assertTrue(deb.endswith("--deb"))

    def test_unsync_uses_the_unsync_subcommand(self):
        cmd = maas_env.MaasEnv._overlay_command(
            "unsync", "/scripts/c.yaml", InstallType.SNAP
        )
        self.assertIn(f"{maas_env.OVERLAY_SCRIPT} unsync", cmd)

    def test_restart_command_per_install_type(self):
        self.assertEqual(
            maas_env.MaasEnv._restart_command(InstallType.SNAP),
            "sudo snap restart maas",
        )
        self.assertEqual(
            maas_env.MaasEnv._restart_command(InstallType.DEB),
            "sudo systemctl restart 'maas-*'",
        )


class TestResolveScriptTargets(unittest.TestCase):
    _MULTI = ["e-1", "e-2", "e-3"]

    def _resolve(self, target, containers):
        script = maas_env.ScriptTarget(path="s.sh", target=target)
        return maas_env.MaasEnv._resolve_targets(script, containers, "pre-install")

    def test_all_targets_every_container(self):
        self.assertEqual(self._resolve(NodeTarget.ALL, self._MULTI), self._MULTI)

    def test_node_targets_map_to_their_index(self):
        for target, expected in (
            (NodeTarget.NODE1, "e-1"),
            (NodeTarget.NODE2, "e-2"),
            (NodeTarget.NODE3, "e-3"),
        ):
            self.assertEqual(self._resolve(target, self._MULTI), [expected])

    def test_missing_node_is_skipped(self):
        self.assertIsNone(self._resolve(NodeTarget.NODE2, ["only"]))


class TestExcludes(unittest.TestCase):
    def test_excludes_overlayfs_workdir(self):
        self.assertIn(".overlayfs_workdir", maas_env.EXCLUDES)

    def test_keeps_sync_excludes(self):
        for pattern in (".git", "__pycache__", "*.pyc"):
            self.assertIn(pattern, maas_env.EXCLUDES)


class TestParseScriptArg(unittest.TestCase):
    def test_parses_path_and_target(self):
        self.assertEqual(
            maas_env.CreateCommand._parse_script_arg("./setup.sh:node2"),
            maas_env.ScriptTarget("./setup.sh", NodeTarget.NODE2),
        )

    def test_splits_on_the_last_colon(self):
        self.assertEqual(
            maas_env.CreateCommand._parse_script_arg("a:b/setup.sh:all").path,
            "a:b/setup.sh",
        )

    def test_rejects_a_missing_target(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            maas_env.CreateCommand._parse_script_arg("./setup.sh")

    def test_rejects_an_unknown_target(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            maas_env.CreateCommand._parse_script_arg("./setup.sh:node9")


class TestParser(unittest.TestCase):
    def setUp(self):
        self.parser = maas_env.CLI().build_parser()

    def test_create_snap_defaults(self):
        args = self.parser.parse_args(["create", "snap", "dev"])
        self.assertEqual(args.name, "dev")
        self.assertEqual(args.mode, Mode.SINGLE)
        self.assertEqual(args.channel, "latest/edge")

    def test_create_deb_requires_ppa_and_branch(self):
        args = self.parser.parse_args(
            ["create", "deb", "dev", "--ppa", "ppa:maas/3.7", "--branch", "3.7"]
        )
        self.assertEqual(args.ppa, "ppa:maas/3.7")
        self.assertEqual(args.branch, "3.7")
        # deb is single-node only, so it has no --mode.
        self.assertFalse(hasattr(args, "mode"))

    def test_create_deb_without_ppa_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["create", "deb", "dev", "--branch", "3.7"])

    def test_install_type_is_required_for_create(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["create", "dev"])

    def test_overlay_config_is_optional(self):
        args = self.parser.parse_args(["overlay", "apply", "dev"])
        self.assertIsNone(args.config)

    def test_overlay_action_is_required(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["overlay", "dev"])

    def test_exec_keeps_its_own_options_out_of_the_command(self):
        args = self.parser.parse_args(["exec", "dev", "--all", "--", "ls", "-la"])
        self.assertTrue(args.all)
        self.assertEqual(args.argv, ["ls", "-la"])

    def test_sync_dest_defaults_to_work(self):
        args = self.parser.parse_args(["sync", "dev", "~/work/maas"])
        self.assertEqual(args.dest, "/work")


class TestInstallFromArgs(unittest.TestCase):
    def _install(self, argv):
        args = maas_env.CLI().build_parser().parse_args(argv)
        return maas_env.CreateCommand._install_from_args(args)

    def test_snap_args_build_a_snap_install(self):
        install = self._install(["create", "snap", "dev", "--channel", "3.7/edge"])
        self.assertIsInstance(install, maas_env.SnapInstall)
        self.assertEqual(install.install_type, InstallType.SNAP)
        self.assertEqual(install.channel, "3.7/edge")

    def test_deb_args_build_a_deb_install(self):
        install = self._install(
            ["create", "deb", "dev", "--ppa", "ppa:maas/3.7", "--branch", "3.7"]
        )
        self.assertIsInstance(install, maas_env.DebInstall)
        self.assertEqual(install.install_type, InstallType.DEB)


class FakeRegistry(maas_env.Registry):
    def __init__(self, envs=None):
        self.envs = dict(envs or {})

    def path(self):
        return Path("/tmp/fake.db")

    def add(self, env):
        self.envs[env.name] = env

    def list_envs(self):
        return list(self.envs.values())

    def get(self, name):
        return self.envs.get(name)

    def delete(self, name):
        self.envs.pop(name, None)


class TestCommandLookup(unittest.TestCase):
    def test_uses_the_recorded_mode(self):
        env = maas_env.Env(
            name="dev",
            mode=Mode.MULTI,
            install_type=InstallType.SNAP,
            maas_channel="3.7/edge",
        )
        record, containers = maas_env.Command.lookup(FakeRegistry({"dev": env}), "dev")
        self.assertIs(record, env)
        self.assertEqual(containers, ["dev-1", "dev-2", "dev-3"])

    def test_untracked_names_fall_back_to_single(self):
        record, containers = maas_env.Command.lookup(FakeRegistry(), "dev")
        self.assertIsNone(record)
        self.assertEqual(containers, ["dev"])


if __name__ == "__main__":
    unittest.main()

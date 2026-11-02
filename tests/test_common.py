import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from backend import auth, common, plugin_manager, wine_tools


class CommonTest(unittest.TestCase):
    def test_strip_ansi(self):
        self.assertEqual(common.strip_ansi("\x1b[1;32msynced\x1b[0m"), "synced")

    def test_deep_merge(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        merged = common._deep_merge(base, {"nested": {"y": 3}, "b": 2})
        self.assertEqual(merged, {"a": 1, "b": 2, "nested": {"x": 1, "y": 3}})
        self.assertEqual(base["nested"]["y"], 2)

    def test_bad_user_config_is_reported_not_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        self.addCleanup(os.unlink, f.name)
        config = common.load_config(Path(f.name))
        self.assertEqual(config["configError"]["code"], "bad_config")

    def test_user_config_overrides_defaults(self):
        defaults = json.loads(common.DEFAULT_CONFIG.read_text())
        key = next(iter(defaults))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({key: "override"}, f)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(common.load_config(Path(f.name))[key], "override")

    def test_run_command_missing_binary(self):
        with self.assertRaises(common.CommandError) as ctx:
            common.run_command(["omavst-no-such-command"])
        self.assertEqual(ctx.exception.err["code"], "not_installed")

    def test_write_private_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "secret"
            common.write_private(path, "x")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), "x")


class AuthTest(unittest.TestCase):
    def test_check(self):
        self.assertTrue(auth.check("abc", "abc"))
        self.assertTrue(auth.check("abc", " abc\n"))
        self.assertFalse(auth.check("abc", "abd"))
        self.assertFalse(auth.check("abc", None))


class PluginPathsTest(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(plugin_manager._inside("/a/b/c.vst3", "/a/b"))
        self.assertFalse(plugin_manager._inside("/a/b", "/a/b"))
        self.assertFalse(plugin_manager._inside("/a/bc/d.vst3", "/a/b"))
        self.assertFalse(plugin_manager._inside("/a/b/../x.vst3", "/a/b"))

    def test_bridged_path(self):
        dirs = {"VST2": "/home/u/.vst/yabridge", "VST3": "/home/u/.vst3/yabridge"}
        vst2 = {"format": "VST2", "relativePath": "Vendor/Synth.dll"}
        vst3 = {"format": "VST3", "relativePath": "Foo.vst3"}
        self.assertEqual(plugin_manager._bridged_path(vst2, dirs), "/home/u/.vst/yabridge/Vendor/Synth.so")
        self.assertEqual(plugin_manager._bridged_path(vst3, dirs), "/home/u/.vst3/yabridge/Foo.vst3")
        self.assertIsNone(plugin_manager._bridged_path({"format": "CLAP", "relativePath": "x"}, dirs))


class InstallerCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        os.chmod(self.dir, 0o700)

    def _installer(self, name="setup.exe", mode=0o644):
        path = self.dir / name
        path.write_bytes(b"MZ")
        os.chmod(path, mode)
        return path

    def test_accepts_private_installer(self):
        path = self._installer()
        self.assertEqual(wine_tools.check_installer(str(path)), path.resolve())

    def test_rejects_other_file_types(self):
        with self.assertRaises(wine_tools.ToolError) as ctx:
            wine_tools.check_installer(str(self._installer("setup.sh")))
        self.assertEqual(ctx.exception.code, "bad_installer")

    def test_rejects_group_writable_installer(self):
        with self.assertRaises(wine_tools.ToolError) as ctx:
            wine_tools.check_installer(str(self._installer(mode=0o664)))
        self.assertEqual(ctx.exception.code, "untrusted_installer")

    def test_rejects_group_writable_folder(self):
        path = self._installer()
        os.chmod(self.dir, 0o770)
        with self.assertRaises(wine_tools.ToolError) as ctx:
            wine_tools.check_installer(str(path))
        self.assertEqual(ctx.exception.code, "untrusted_installer")


if __name__ == "__main__":
    unittest.main()

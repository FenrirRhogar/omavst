import unittest

from backend import vst_scanner

STATUS = """\
yabridge path: <auto>
VST2 location: '/home/u/.vst/yabridge'
VST3 location: '/home/u/.vst3/yabridge'

libyabridge-chainloader-vst3.so: '/home/u/.local/share/yabridge/libyabridge-chainloader-vst3.so' (64-bit)
libyabridge-chainloader-clap.so: <not found>

yabridge-host.exe: '/home/u/.local/share/yabridge/yabridge-host.exe'

/home/u/.wine/drive_c/Program Files/Common Files/VST3/
  Foo.vst3 :: VST3, bundle, 64-bit, synced
  Vendor/Bar.vst3 :: VST3, legacy, 64-bit, not yet synced
/home/u/plugins/
  Baz.dll :: VST2, 32-bit, weird, synced
"""


class ParseStatusTest(unittest.TestCase):
    def setUp(self):
        self.status = vst_scanner.parse_status(STATUS)

    def test_headers(self):
        self.assertEqual(self.status["settings"], {"yabridge path": "<auto>"})
        self.assertEqual(self.status["installDirs"], {
            "VST2": "/home/u/.vst/yabridge",
            "VST3": "/home/u/.vst3/yabridge",
        })
        self.assertEqual(self.status["hosts"], {
            "yabridge-host.exe": "/home/u/.local/share/yabridge/yabridge-host.exe",
        })

    def test_chainloaders(self):
        vst3 = self.status["chainloaders"]["libyabridge-chainloader-vst3.so"]
        self.assertEqual(vst3["arch"], "64-bit")
        self.assertTrue(vst3["found"])
        self.assertFalse(self.status["chainloaders"]["libyabridge-chainloader-clap.so"]["found"])

    def test_directories_and_prefixes(self):
        dirs = self.status["watchedDirectories"]
        self.assertEqual([d["path"] for d in dirs], [
            "/home/u/.wine/drive_c/Program Files/Common Files/VST3",
            "/home/u/plugins",
        ])
        self.assertEqual(dirs[0]["prefix"], "/home/u/.wine")
        self.assertIsNone(dirs[1]["prefix"])
        self.assertEqual(self.status["prefixes"], ["/home/u/.wine"])
        self.assertEqual((dirs[0]["pluginCount"], dirs[0]["syncedCount"]), (2, 1))

    def test_plugins(self):
        foo, bar = self.status["watchedDirectories"][0]["plugins"]
        self.assertEqual(foo["name"], "Foo")
        self.assertEqual((foo["format"], foo["kind"], foo["arch"]), ("VST3", "bundle", "64-bit"))
        self.assertTrue(foo["synced"])
        self.assertEqual(bar["path"], "/home/u/.wine/drive_c/Program Files/Common Files/VST3/Vendor/Bar.vst3")
        self.assertEqual(bar["status"], "not yet synced")
        self.assertFalse(bar["synced"])

    def test_unknown_attributes_are_kept(self):
        baz = self.status["watchedDirectories"][1]["plugins"][0]
        self.assertEqual(baz["extra"], ["weird"])
        self.assertIsNone(baz["kind"])

    def test_sync_summary(self):
        self.assertEqual(self.status["sync"], {
            "total": 3,
            "synced": 2,
            "unsynced": ["Vendor/Bar.vst3"],
            "inSync": False,
        })

    def test_empty_output(self):
        status = vst_scanner.parse_status("")
        self.assertEqual(status["sync"]["total"], 0)
        self.assertTrue(status["sync"]["inSync"])

    def test_unparsed_lines_are_kept(self):
        status = vst_scanner.parse_status("something odd\n  stray indented line\n")
        self.assertEqual(status["settings"]["unparsed"], ["something odd"])


if __name__ == "__main__":
    unittest.main()

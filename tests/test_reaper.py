import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import reaper

CACHE = """\
[vstcache]
ABPL.vst3=00A1,1234{ABCD,Ample Bass P Lite (Ample Sound)!!!VSTi
PolyMAX_Synth.vst3=00A2,5678{EF01,PolyMAX Synth (Universal Audio (UADx))
Broken.vst3=00A3,0
"""


class SplitVendorTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(reaper.split_vendor("Pro-Q 3 (FabFilter)"), ("Pro-Q 3", "FabFilter"))

    def test_nested_brackets(self):
        self.assertEqual(reaper.split_vendor("PolyMAX Synth (Universal Audio (UADx))"),
                         ("PolyMAX Synth", "Universal Audio (UADx)"))

    def test_no_vendor(self):
        self.assertEqual(reaper.split_vendor("ReaEQ"), ("ReaEQ", None))
        self.assertEqual(reaper.split_vendor("(Only Brackets)"), ("(Only Brackets)", None))


class CacheTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / reaper.CACHE_FILES[0]).write_text(CACHE)

    def test_cache_key(self):
        self.assertEqual(reaper.cache_key("/x/PolyMAX Synth.vst3"), "PolyMAX_Synth.vst3")

    def test_read_cache(self):
        entries = reaper.read_cache(self.dir)
        self.assertEqual(entries["ABPL.vst3"], {
            "displayName": "Ample Bass P Lite",
            "vendor": "Ample Sound",
            "instrument": True,
        })
        self.assertEqual(entries["PolyMAX_Synth.vst3"]["vendor"], "Universal Audio (UADx)")
        self.assertNotIn("Broken.vst3", entries)

    def test_forget_while_closed(self):
        with mock.patch.object(reaper, "is_running", return_value=False):
            result = reaper.forget(["/x/PolyMAX Synth.vst3"], self.dir)
        self.assertEqual(result, {"reaperForgot": 1})
        self.assertNotIn("PolyMAX_Synth.vst3", reaper.read_cache(self.dir))
        self.assertIn("ABPL.vst3", reaper.read_cache(self.dir))

    def test_forget_while_open_leaves_cache(self):
        with mock.patch.object(reaper, "is_running", return_value=True):
            result = reaper.forget(["/x/ABPL.vst3"], self.dir)
        self.assertIn("reaperNote", result)
        self.assertIn("ABPL.vst3", reaper.read_cache(self.dir))


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest

from config import ConfigFile


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, ".env")

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_and_new_keys(self):
        cfg = ConfigFile(self.path)
        cfg.set_many({"A": "1", "B": "hello world"})
        cfg.save()

        cfg2 = ConfigFile(self.path)
        self.assertEqual(cfg2.get("A"), "1")
        self.assertEqual(cfg2.get_all(), {"A": "1", "B": "hello world"})

        cfg2.set_many({"B": "2", "C": "3"})
        cfg2.save()
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("B=2", text)
        self.assertIn("C=3", text)

    def test_preserves_comments_and_inline_comments(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# header\nA=1 # keep me\n\nB=2\n")
        cfg = ConfigFile(self.path)
        cfg.set_many({"A": "9"})
        cfg.save()
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("# header", text)
        self.assertIn("A=9 # keep me", text)
        self.assertIn("\n\nB=2", text)


if __name__ == "__main__":
    unittest.main()

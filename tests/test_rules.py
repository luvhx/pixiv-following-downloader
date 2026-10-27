import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

from database import DownloadDB  # noqa: E402
from rules import DEFAULTS, Rules, coerce  # noqa: E402


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DownloadDB(os.path.join(self.tmp.name, "t.db"))
        self.rules = Rules(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_defaults(self):
        snap = self.rules.snapshot()
        for key, value in DEFAULTS.items():
            self.assertEqual(snap[key], value, key)
        self.assertEqual(snap["include_tags"], [])
        self.assertEqual(snap["exclude_tags"], [])

    def test_update_and_refresh(self):
        errors = self.rules.update({
            "download_kind": "large",
            "illust_type": "all",
            "first_page_only": "true",
            "max_per_artist": "8",
            "min_bookmark_count": "100",
            "run_cron": "0 */6 * * *",
            "run_cron_immediate": "false",
            "poll_interval": "3600",
        })
        self.assertEqual(errors, {})
        snap = self.rules.snapshot(refresh=True)
        self.assertEqual(snap["download_kind"], "large")
        self.assertEqual(snap["illust_type"], "all")
        self.assertIs(snap["first_page_only"], True)
        self.assertEqual(snap["max_per_artist"], 8)
        self.assertEqual(snap["min_bookmark_count"], 100)
        self.assertEqual(snap["run_cron"], "0 */6 * * *")
        self.assertIs(snap["run_cron_immediate"], False)
        self.assertEqual(snap["poll_interval"], 3600)

        # 另一个实例（模拟重启）也能读到
        other = Rules(self.db)
        self.assertEqual(other.snapshot()["max_per_artist"], 8)

    def test_validate_rejects_bad_values(self):
        errors = self.rules.update({
            "download_kind": "huge",
            "max_per_artist": "-1",
            "run_cron": "not a cron",
            "poll_interval": "-5",
            "illust_type": "illust",
            "first_page_only": "false",
            "min_bookmark_count": "0",
        })
        self.assertIn("download_kind", errors)
        self.assertIn("max_per_artist", errors)
        self.assertIn("run_cron", errors)
        self.assertIn("poll_interval", errors)
        # 校验失败不写库
        self.assertEqual(self.rules.snapshot(refresh=True)["download_kind"], "original")

    def test_coerce_clamps(self):
        self.assertEqual(coerce("max_per_artist", "999999"), 100000)
        self.assertEqual(coerce("poll_interval", "-1"), 0)
        self.assertEqual(coerce("poll_interval", "99999999"), 2_592_000)
        self.assertEqual(coerce("download_kind", "LARGE"), "large")
        self.assertIs(coerce("first_page_only", "on"), True)
        self.assertIs(coerce("run_cron_immediate", "no"), False)
        self.assertEqual(coerce("run_cron", " 0 3 * * * "), "0 3 * * *")

    def test_tag_include_and_exclude(self):
        self.assertTrue(self.rules.add_tag("风景", "include")[0])
        self.assertTrue(self.rules.add_tag("R-18", "exclude")[0])
        snap = self.rules.snapshot(refresh=True)
        self.assertEqual(snap["include_tags"], ["风景"])
        self.assertEqual(snap["exclude_tags"], ["r-18"])

        # 同一个 tag 改模式应就地更新
        self.rules.add_tag("风景", "exclude")
        snap = self.rules.snapshot(refresh=True)
        self.assertEqual(snap["include_tags"], [])
        self.assertEqual(set(snap["exclude_tags"]), {"风景", "r-18"})

        self.assertTrue(self.rules.remove_tag("R-18"))
        self.assertEqual(self.rules.snapshot(refresh=True)["exclude_tags"], ["风景"])
        self.assertFalse(self.rules.remove_tag("不存在"))

    def test_tag_validation(self):
        ok, error = self.rules.add_tag("", "include")
        self.assertFalse(ok)
        self.assertIn("不能为空", error)
        ok, error = self.rules.add_tag("abc", "bogus")
        self.assertFalse(ok)
        self.assertIn("include", error)
        ok, error = self.rules.add_tag("x" * 101, "include")
        self.assertFalse(ok)

    def test_migrate_legacy_env(self):
        with mock.patch.dict(os.environ, {
            "RUN_CRON": "0 3 * * *",
            "POLL_INTERVAL": "0",
            "RUN_CRON_IMMEDIATE": "false",
        }):
            self.assertTrue(self.rules.migrate_legacy_env())
        snap = self.rules.snapshot(refresh=True)
        self.assertEqual(snap["run_cron"], "0 3 * * *")
        self.assertIs(snap["run_cron_immediate"], False)
        # 已经迁移过就不再覆盖
        self.assertFalse(self.rules.migrate_legacy_env())

    def test_reset_watermarks(self):
        self.db.set_artist_watermark(1, "a", 100)
        self.db.set_artist_watermark(2, "b", 200)
        self.assertEqual(self.rules.reset_watermarks(), 2)
        self.assertEqual(self.db.artist_watermark(1), 0)
        self.assertEqual(self.db.artist_watermark(2), 0)


if __name__ == "__main__":
    unittest.main()

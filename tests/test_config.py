import sys
import tempfile
import types
import unittest
from pathlib import Path

# config.py 在导入时会调用 dotenv.load_dotenv；测试环境无需读取真实 .env
_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

import config  # noqa: E402
from config import ConfigFile  # noqa: E402


class ConfigCoerceTest(unittest.TestCase):
    def test_enum_fallback(self):
        self.assertEqual(config._coerce("STORAGE_BACKEND", "WEBDAV"), "webdav")
        self.assertEqual(config._coerce("STORAGE_BACKEND", "ftp"), "local")
        self.assertEqual(config._coerce("IMAGE_DELIVERY", "REDIRECT"), "redirect")

    def test_numeric_guards(self):
        self.assertEqual(config._coerce("TOKEN_REFRESH_INTERVAL", "120"), 59)
        self.assertEqual(config._coerce("TOKEN_REFRESH_INTERVAL", "0"), 1)
        self.assertEqual(
            config._coerce("WEBUI_PORT", "70000"),
            int(config._DEFAULTS["WEBUI_PORT"]),
        )
        self.assertEqual(config._coerce("WEBUI_PORT", "9000"), 9000)
        self.assertEqual(config._coerce("WEBDAV_VERIFY_TIMEOUT", "-5"), 0)
        self.assertEqual(config._coerce("MAX_RETRIES", "0"), 1)
        self.assertEqual(config._coerce("MAX_RETRIES", "99"), 20)
        self.assertEqual(config._coerce("REQUEST_DELAY", "-1"), 0.0)
        self.assertEqual(config._coerce("REQUEST_DELAY", "700"), 600.0)
        self.assertEqual(config._coerce("REQUEST_JITTER", "abc"), 1.0)
        self.assertEqual(config._coerce("ARTIST_COOLDOWN_SECONDS", "99999"), 7200)

    def test_bool_and_paths(self):
        self.assertIs(config._coerce("WEBDAV_VERIFY_SSL", "no"), False)
        self.assertIs(config._coerce("WEBDAV_VERIFY_SSL", "YES"), True)
        self.assertEqual(config._coerce("WEBDAV_ROOT", "/pixiv/"), "pixiv")
        self.assertEqual(config._coerce("WEBDAV_PUBLIC_URL", " https://x/dav/ "), "https://x/dav")
        self.assertEqual(config._coerce("WEBUI_USERNAME", "  admin "), "admin")

    def test_download_rules_not_in_env(self):
        # 下载规则已迁移到数据库
        for key in ("DOWNLOAD_KIND", "ILLUST_TYPE", "EXCLUDE_KEYWORDS",
                    "MIN_BOOKMARK_COUNT", "THUMB_PATH",
                    "RUN_CRON", "RUN_CRON_IMMEDIATE", "POLL_INTERVAL"):
            self.assertNotIn(key, config._DEFAULTS)
        # 请求节流属于部署配置，仍在 .env
        for key in ("REQUEST_DELAY", "REQUEST_JITTER", "DOWNLOAD_DELAY",
                    "MAX_RETRIES", "RATE_LIMIT_WAIT",
                    "ARTIST_COOLDOWN_EVERY", "ARTIST_COOLDOWN_SECONDS"):
            self.assertIn(key, config._DEFAULTS)


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.path.write_text(
            "# 注释保留\n"
            "PIXIV_REFRESH_TOKEN=abc\n"
            "WEBUI_PORT=8080  # 端口\n"
            "TARGET_USER_ID=42\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_and_write_preserves_comments(self):
        cfg = ConfigFile(self.path)
        self.assertEqual(cfg.get("PIXIV_REFRESH_TOKEN"), "abc")
        self.assertEqual(cfg.get("WEBUI_PORT"), "8080")
        cfg.set_many({"WEBUI_PORT": "9000", "NEW_KEY": "v"})
        cfg.save()

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# 注释保留", text)
        self.assertIn("WEBUI_PORT=9000 # 端口", text)
        self.assertIn("NEW_KEY=v", text)

        again = ConfigFile(self.path)
        self.assertEqual(again.get("WEBUI_PORT"), "9000")
        self.assertEqual(again.get("NEW_KEY"), "v")

    def test_unknown_keys_kept(self):
        cfg = ConfigFile(self.path)
        self.assertEqual(cfg.get_all()["TARGET_USER_ID"], "42")


if __name__ == "__main__":
    unittest.main()

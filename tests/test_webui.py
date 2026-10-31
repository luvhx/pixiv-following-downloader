import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from jinja2 import StrictUndefined

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

import config  # noqa: E402
import webui  # noqa: E402
from database import DownloadDB  # noqa: E402
from rules import Rules  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from storage import LocalStorage  # noqa: E402


class StubQueue:
    def __init__(self, *args, **kwargs):
        self.submitted = []

    def submit(self, illust_id):
        self.submitted.append(illust_id)
        return True

    def pending_ids(self):
        return set()


class FakeAPI:
    access_token = "token"
    user_id = 123

    def __init__(self):
        self.following_calls = 0
        self.user_illusts_calls = 0

    def user_following(self, user_id, restrict="public", **kwargs):
        self.following_calls += 1
        return {
            "user_previews": [{
                "user": {
                    "id": 55, "name": "Artist",
                    "profile_image_urls": {"medium": "https://i.pximg.net/a.jpg"},
                },
                "illusts": [{
                    "id": 2001, "title": "预览作品", "total_bookmarks": 99,
                    "page_count": 1,
                    "image_urls": {"medium": "https://i.pximg.net/p1.jpg"},
                }],
            }],
            "next_url": None,
        }

    def user_illusts(self, user_id, type="illust", offset=None, **kwargs):
        self.user_illusts_calls += 1
        return {
            "illusts": [{
                "id": 1001, "title": "新作品", "page_count": 2,
                "user": {"id": 55, "name": "Artist"},
                "tags": [{"name": "tagA"}],
                "total_bookmarks": 42,
                "meta_pages": [{
                    "page_index": 0,
                    "image_urls": {"medium": "https://i.pximg.net/m.jpg"},
                }],
                "type": type,
            }],
            "next_url": None,
        }

    def illust_detail(self, illust_id):
        return {"illust": None}

    def user_detail(self, artist_id, **kwargs):
        return {"user": {
            "id": artist_id, "name": f"Artist {artist_id}",
            "profile_image_urls": {"medium": "https://i.pximg.net/u.jpg"},
        }}

    @staticmethod
    def parse_qs(url):
        return None


class WebUITest(unittest.TestCase):
    _KEYS = (
        "PIXIV_REFRESH_TOKEN", "PIXIV_DOWNLOAD_REFRESH_TOKEN", "STORAGE_BACKEND",
        "DOWNLOAD_PATH", "DB_PATH", "WEBDAV_URL", "WEBDAV_ROOT",
        "WEBDAV_PUBLIC_URL", "IMAGE_DELIVERY", "WEBUI_USERNAME", "WEBUI_PASSWORD",
        "REQUEST_DELAY", "REQUEST_JITTER", "DOWNLOAD_DELAY", "MAX_RETRIES",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = {k: getattr(config, k) for k in self._KEYS}

        config.PIXIV_REFRESH_TOKEN = "test-token"
        config.PIXIV_DOWNLOAD_REFRESH_TOKEN = ""
        config.STORAGE_BACKEND = "local"
        config.DOWNLOAD_PATH = str(self.root / "downloads")
        config.DB_PATH = str(self.root / "test.db")
        config.WEBDAV_URL = ""
        config.WEBDAV_ROOT = "pixiv"
        config.WEBDAV_PUBLIC_URL = ""
        config.IMAGE_DELIVERY = "proxy"
        config.WEBUI_USERNAME = ""
        config.WEBUI_PASSWORD = ""
        config.REQUEST_DELAY = 0.0
        config.REQUEST_JITTER = 0.0
        config.DOWNLOAD_DELAY = 0.0
        config.MAX_RETRIES = 3

        self.storage = LocalStorage(config.DOWNLOAD_PATH)
        artist_dir = self.root / "downloads" / "Artist"
        artist_dir.mkdir(parents=True)
        (artist_dir / "1_test.jpg").write_bytes(b"\xff\xd8jpegdata")

        self.db = DownloadDB(config.DB_PATH)
        self.db.mark_downloaded(
            1, "Test work", 10, "Artist", ["tag1", "tag2"], 2,
            files=["Artist/1_test.jpg"],
            thumb_url="https://i.pximg.net/thumb.jpg", illust_type="illust",
        )
        self.rules = Rules(self.db)
        webui._LOGIN_FAILS.clear()

    def tearDown(self):
        self.db.close()
        for key, value in self._orig.items():
            setattr(config, key, value)
        webui._LOGIN_FAILS.clear()
        self._tmp.cleanup()

    def make_client(self, api=None, queue=None):
        queue_patch = mock.patch.object(webui, "DownloadQueue", queue or StubQueue)
        queue_patch.start()
        self.addCleanup(queue_patch.stop)
        app = webui.create_app(api, self.db, self.rules, None)
        app.jinja_env.undefined = StrictUndefined
        return app.test_client()

    def csrf(self, client):
        with client.session_transaction() as sess:
            return sess.get("_csrf_token")

    def storage_patch(self):
        patcher = mock.patch.object(webui, "get_storage", return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------- 页面 ----------
    def test_pages_render(self):
        self.storage_patch()
        client = self.make_client()
        for path in ("/", "/browse", "/downloaded", "/downloaded?tag=tag1",
                     "/downloaded?q=Test", "/downloaded/1", "/image/1",
                     "/rules", "/config", "/logs", "/healthz"):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            resp.close()
        self.assertIn(b"Test work", client.get("/downloaded").data)
        self.assertIn(b"tag1", client.get("/downloaded/1").data)

    def test_thumbnails_go_through_proxy(self):
        self.storage_patch()
        client = self.make_client()
        html = client.get("/downloaded").data.decode()
        # 浏览器直连 i.pximg.net 会 403，必须走服务端转发
        self.assertIn('src="/thumb/1"', html)
        self.assertNotIn("i.pximg.net", html)
        self.assertNotIn("referrerpolicy", html)
        self.assertNotIn("/gallery/thumb/", html)

    def test_thumb_proxy_passes_referer(self):
        self.storage_patch()
        client = self.make_client()
        upstream = mock.Mock()
        upstream.status_code = 200
        upstream.headers = {"Content-Type": "image/jpeg", "Content-Length": "3"}
        upstream.iter_content = lambda chunk_size=65536: iter([b"abc"])
        upstream.close = mock.Mock()
        url = "https://i.pximg.net/c/540x540_70/img-master/img/a.jpg"
        with mock.patch.object(webui._thumb_session, "get", return_value=upstream) as get:
            resp = client.get("/thumb", query_string={"u": url})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"abc")
        self.assertEqual(resp.headers["Cache-Control"], "private, max-age=604800")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], url)
        self.assertEqual(get.call_args.kwargs["stream"], True)

    def test_thumb_proxy_rejects_other_hosts(self):
        self.storage_patch()
        client = self.make_client()
        for bad in ("https://evil.example.com/a.jpg",
                    "http://i.pximg.net/a.jpg",
                    "https://i.pximg.net:8443/a.jpg",
                    "https://pximg.net.evil.com/a.jpg",
                    ""):
            resp = client.get("/thumb", query_string={"u": bad})
            self.assertEqual(resp.status_code, 400, bad)

    def test_thumb_proxy_upstream_error(self):
        self.storage_patch()
        client = self.make_client()
        upstream = mock.Mock()
        upstream.status_code = 403
        upstream.headers = {}
        upstream.close = mock.Mock()
        with mock.patch.object(webui._thumb_session, "get", return_value=upstream):
            resp = client.get("/thumb", query_string={
                "u": "https://i.pximg.net/c/540x540_70/img-master/img/a.jpg",
            })
        self.assertEqual(resp.status_code, 404)

    def test_thumb_by_illust_id(self):
        self.storage_patch()
        client = self.make_client()
        upstream = mock.Mock()
        upstream.status_code = 200
        upstream.headers = {"Content-Type": "image/jpeg"}
        upstream.iter_content = lambda chunk_size=65536: iter([b"x"])
        upstream.close = mock.Mock()
        with mock.patch.object(webui._thumb_session, "get", return_value=upstream):
            resp = client.get("/thumb/1")
            missing = client.get("/thumb/999")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(missing.status_code, 404)

    def test_legacy_gallery_redirects(self):
        self.storage_patch()
        client = self.make_client()
        resp = client.get("/gallery?tag=tag1")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/downloaded", resp.headers["Location"])
        resp = client.get("/gallery/1")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/downloaded/1", resp.headers["Location"])
        resp = client.get("/downloads")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/downloaded", resp.headers["Location"])

    def test_unknown_work_404(self):
        self.storage_patch()
        client = self.make_client()
        self.assertEqual(client.get("/downloaded/999").status_code, 404)
        self.assertEqual(client.get("/image/999").status_code, 404)

    def test_setup_guard(self):
        self.storage_patch()
        client = self.make_client()
        config.PIXIV_REFRESH_TOKEN = ""
        resp = client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/setup", resp.headers["Location"])
        self.assertEqual(client.get("/healthz").status_code, 200)
        setup_page = client.get("/setup")
        self.assertEqual(setup_page.status_code, 200)
        self.assertNotIn(b'class="topbar"', setup_page.data)

    # ---------- 登录 ----------
    def test_login_required_and_success(self):
        config.WEBUI_USERNAME = "admin"
        config.WEBUI_PASSWORD = "secret"
        client = self.make_client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

        login_page = client.get("/login").data.decode()
        self.assertNotIn('class="topbar"', login_page)
        self.assertIn("<title>登录 · Pixiv Following Downloader</title>", login_page)
        token = self.csrf(client)
        resp = client.post("/login", data={
            "username": "admin", "password": "secret", "_csrf": token,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)

    def test_login_rate_limit(self):
        config.WEBUI_USERNAME = "admin"
        config.WEBUI_PASSWORD = "secret"
        client = self.make_client()
        client.get("/login")
        token = self.csrf(client)
        for _ in range(5):
            resp = client.post("/login", data={
                "username": "admin", "password": "wrong", "_csrf": token,
            })
            self.assertEqual(resp.status_code, 200)
        self.assertGreater(webui._login_retry_after("127.0.0.1"), 0)
        self.assertEqual(client.get("/login").status_code, 429)
        # 正确密码在封禁期内也不能登录
        self.assertEqual(client.post("/login", data={
            "username": "admin", "password": "secret", "_csrf": token,
        }).status_code, 429)

    def test_config_save_throttle(self):
        env_path = self.root / ".env"
        env_path.write_text("PIXIV_REFRESH_TOKEN=old-token\n", encoding="utf-8")
        original_env = config.ENV_FILE
        config.ENV_FILE = env_path
        self.addCleanup(setattr, config, "ENV_FILE", original_env)
        self.addCleanup(config.reload, True)

        self.storage_patch()
        client = self.make_client()
        client.get("/config")
        token = self.csrf(client)
        data = {key: webui._current_str(key) for key, *_ in webui.CONFIG_FIELDS}
        data["REQUEST_DELAY"] = "2.5"
        data["_csrf"] = token
        resp = client.post("/config", data=data)
        self.assertEqual(resp.status_code, 302)
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("REQUEST_DELAY=2.5", text)
        self.assertIn("MAX_RETRIES=3", text)
        # 测试环境里 dotenv 被替换成 no-op，因此只校验写回内容
        self.assertEqual(config._coerce("REQUEST_DELAY", "2.5"), 2.5)

    # ---------- CSRF ----------
    def test_csrf_required(self):
        self.storage_patch()
        client = self.make_client()
        resp = client.post("/rules", data={"download_kind": "large"})
        self.assertEqual(resp.status_code, 400)

    # ---------- 规则 ----------
    def test_rules_save_and_validate(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/rules")
        token = self.csrf(client)

        resp = client.post("/rules", data={
            "download_kind": "large", "illust_type": "all",
            "first_page_only": "true", "max_per_artist": "5",
            "min_bookmark_count": "0",
            "ugoira_format": "webm", "folder_template": "", "filename_template": "",
            "run_cron": "0 */6 * * *", "run_cron_immediate": "true",
            "poll_interval": "0",
            "_csrf": token,
        })
        self.assertEqual(resp.status_code, 302)
        snap = self.rules.snapshot(refresh=True)
        self.assertEqual(snap["download_kind"], "large")
        self.assertEqual(snap["max_per_artist"], 5)
        self.assertIs(snap["first_page_only"], True)
        self.assertEqual(snap["run_cron"], "0 */6 * * *")

        resp = client.post("/rules", data={
            "download_kind": "nope", "illust_type": "all",
            "first_page_only": "false", "max_per_artist": "5",
            "min_bookmark_count": "0",
            "ugoira_format": "webm", "folder_template": "", "filename_template": "",
            "run_cron": "0 */6 * * *", "run_cron_immediate": "false",
            "poll_interval": "0",
            "_csrf": token,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"\xe5\x8f\x96\xe5\x80\xbc\xe6\x97\xa0\xe6\x95\x88", resp.data)  # 取值无效
        self.assertEqual(self.rules.snapshot(refresh=True)["download_kind"], "large")
        self.assertEqual(self.rules.snapshot(refresh=True)["run_cron"], "0 */6 * * *")

    def test_tag_add_and_remove(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/rules")
        token = self.csrf(client)
        client.post("/rules/tag/add", data={
            "tag": "风景", "mode": "include", "next": "/rules", "_csrf": token,
        })
        self.assertEqual([t["tag"] for t in self.rules.tags("include")], ["风景"])
        self.assertIn("风景", client.get("/rules").data.decode())

        client.post("/rules/tag/remove", data={
            "tag": "风景", "next": "/rules", "_csrf": token,
        })
        self.assertEqual(self.rules.tags("include"), [])

    def test_tag_purge_deletes_downloads(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/rules")
        token = self.csrf(client)
        client.post("/rules/tag/add", data={
            "tag": "tag1", "mode": "exclude", "purge": "1",
            "next": "/rules", "_csrf": token,
        })
        self.assertIsNone(self.db.get(1))
        self.assertFalse((self.root / "downloads" / "Artist" / "1_test.jpg").exists())

    def test_reset_watermarks(self):
        self.storage_patch()
        self.db.set_artist_watermark(55, "Artist", 900)
        client = self.make_client()
        client.get("/rules")
        token = self.csrf(client)
        client.post("/rules/reset-watermarks", data={"_csrf": token})
        self.assertEqual(self.db.artist_watermark(55), 0)

    # ---------- 已下载操作 ----------
    def test_multipage_viewer(self):
        artist = self.root / "downloads" / "Artist"
        (artist / "2_a.jpg").write_bytes(b"AAA")
        (artist / "2_b.jpg").write_bytes(b"BBB")
        self.db.mark_downloaded(
            2, "Multi page", 10, "Artist", ["tag1"], 2,
            files=["Artist/2_a.jpg", "Artist/2_b.jpg"],
            thumb_url="https://i.pximg.net/m.jpg", bookmarks=999,
        )
        self.storage_patch()
        client = self.make_client()
        detail = client.get("/downloaded/2?p=2&tag=tag1&sort=bookmarks&page=1").data.decode()
        self.assertIn("第 2 / 2 页", detail)
        self.assertIn("p=2", detail)
        img2 = client.get("/image/2?p=2")
        self.assertEqual(img2.data, b"BBB")
        img2.close()
        self.assertEqual(client.get("/image/2?p=3").status_code, 404)
        img1 = client.get("/image/2")
        self.assertEqual(img1.data, b"AAA")
        img1.close()

    def test_ugoira_video_detail(self):
        artist = self.root / "downloads" / "Artist"
        (artist / "3_anim.webm").write_bytes(b"webmdata")
        self.db.mark_downloaded(
            3, "Anim", 10, "Artist", ["动图"], 1,
            files=["Artist/3_anim.webm"], thumb_url="https://i.pximg.net/a.jpg",
            illust_type="ugoira",
        )
        self.storage_patch()
        client = self.make_client()
        detail = client.get("/downloaded/3").data.decode()
        self.assertIn("<video", detail)
        self.assertEqual(client.get("/image/3").content_type, "video/webm")

    def test_gallery_sort_by_bookmarks(self):
        self.db.mark_downloaded(
            2, "High marks", 10, "Artist", ["tag1"], 1,
            files=["Artist/2_a.jpg"], bookmarks=999,
        )
        self.storage_patch()
        client = self.make_client()
        html = client.get("/downloaded?sort=bookmarks").data.decode()
        self.assertLess(html.index("High marks"), html.index("Test work"))
        self.assertIn('<option value="bookmarks" selected>', html)
        self.assertIn("999 收藏", html)

    def test_browse_batch_download(self):
        api = FakeAPI()
        queue = StubQueue()
        with mock.patch.object(webui, "DownloadQueue", return_value=queue):
            app = webui.create_app(api, self.db, self.rules, None)
        app.jinja_env.undefined = StrictUndefined
        client = app.test_client()
        client.get("/browse/55")
        token = self.csrf(client)
        resp = client.post("/browse/download-batch", data={
            "illust_id": ["1001", "1"], "_csrf": token, "next": "/browse/55",
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(queue.submitted, [1001])
        self.assertIn("跳过 1", resp.data.decode())

    def test_favorite_and_delete(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/downloaded")
        token = self.csrf(client)

        client.post("/downloaded/1/favorite", data={"_csrf": token, "next": "/downloaded"})
        self.assertEqual(self.db.get(1)["favorite"], 1)

        resp = client.post("/downloaded/delete", data={
            "illust_id": "1", "_csrf": token,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(self.db.get(1))
        self.assertFalse((self.root / "downloads" / "Artist" / "1_test.jpg").exists())

    def test_delete_keeps_record_on_storage_error(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/downloaded")
        token = self.csrf(client)
        with mock.patch.object(self.storage, "delete", side_effect=RuntimeError("boom")):
            resp = client.post("/downloaded/delete", data={
                "illust_id": "1", "_csrf": token,
            }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(self.db.get(1))
        self.assertIn("记录已保留", resp.data.decode())

    def test_favorite_tag(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/downloaded")
        token = self.csrf(client)
        client.post("/tag/favorite", data={
            "tag": "tag1", "next": "/downloaded", "_csrf": token,
        })
        self.assertEqual(self.db.favorite_tags(), ["tag1"])

    def test_redirect_safety(self):
        self.storage_patch()
        client = self.make_client()
        client.get("/downloaded")
        token = self.csrf(client)
        resp = client.post("/tag/favorite", data={
            "tag": "tag2", "next": "//evil.example.com/x", "_csrf": token,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example.com", resp.headers["Location"])
        self.assertTrue(resp.headers["Location"].endswith("/downloaded"))

    # ---------- 浏览 ----------
    def test_browse_refresh_and_artist(self):
        api = FakeAPI()
        queue = StubQueue()
        with mock.patch.object(webui, "DownloadQueue", return_value=queue):
            app = webui.create_app(api, self.db, self.rules, None)
        app.jinja_env.undefined = StrictUndefined
        client = app.test_client()
        client.get("/browse")
        token = self.csrf(client)
        resp = client.post("/browse/refresh", data={"_csrf": token})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.db.following()), 1)
        self.assertEqual(api.following_calls, 2)  # 公开 + 隐藏

        page = client.get("/browse/55")
        self.assertEqual(page.status_code, 200)
        html = page.data.decode()
        self.assertIn("新作品", html)
        self.assertIn("https://i.pximg.net/m.jpg", html)
        self.assertIn("下载", html)

        resp = client.post("/browse/download/1001", data={
            "_csrf": token, "next": "/browse/55",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(queue.submitted, [1001])

    def test_browse_preview_toggle(self):
        api = FakeAPI()
        with mock.patch.object(webui, "DownloadQueue", return_value=StubQueue()):
            app = webui.create_app(api, self.db, self.rules, None)
        app.jinja_env.undefined = StrictUndefined
        client = app.test_client()
        client.get("/browse")
        token = self.csrf(client)
        client.post("/browse/refresh", data={"_csrf": token})

        off = client.get("/browse").data.decode()
        self.assertNotIn("预览作品", off)
        self.assertIn("预览代表作", off)

        on = client.get("/browse?preview=1").data.decode()
        self.assertIn("预览作品", on)
        self.assertNotIn("每个画师展示", on)
        self.assertIn("关闭预览", on)
        self.assertIn("https://i.pximg.net/p1.jpg", on)

    def test_browse_artist_tag_actions(self):
        api = FakeAPI()
        with mock.patch.object(webui, "DownloadQueue", return_value=StubQueue()):
            app = webui.create_app(api, self.db, self.rules, None)
        app.jinja_env.undefined = StrictUndefined
        client = app.test_client()
        client.get("/browse/55")
        token = self.csrf(client)

        resp = client.post("/tag/favorite", data={
            "tag": "tagA", "next": "/browse/55", "_csrf": token,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.favorite_tags(), ["tagA"])
        self.assertIn("★", client.get("/browse/55").data.decode())

        resp = client.post("/rules/tag/add", data={
            "tag": "tagA", "mode": "exclude", "next": "/browse/55", "_csrf": token,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual([t["tag"] for t in self.rules.tags("exclude")], ["tagA"])
        self.assertIn("已在排除规则中", client.get("/browse/55").data.decode())

    def test_browse_ignored_artist(self):
        self.db.replace_following([
            {"user_id": 55, "user_name": "Artist"},
            {"user_id": 66, "user_name": "Muted"},
        ])
        self.db.set_artist_ignored(66, True)
        self.storage_patch()
        client = self.make_client()

        hidden = client.get("/browse").data.decode()
        self.assertNotIn("Muted", hidden)
        self.assertIn("显示已暂停", hidden)

        shown = client.get("/browse?ignored=1").data.decode()
        self.assertIn("Muted", shown)
        self.assertIn("已暂停", shown)

        token = self.csrf(client)
        client.post("/browse/ignore/66", data={
            "ignored": "0", "next": "/browse", "_csrf": token,
        })
        self.assertFalse(self.db.following()[1]["ignored"])
        self.assertIn("Muted", client.get("/browse").data.decode())

    def test_api_progress(self):
        sched = Scheduler(None, None, self.db, self.rules)
        sched._set_progress(index=1, total=2, artist="A", work="W")
        with mock.patch.object(webui, "DownloadQueue", StubQueue):
            app = webui.create_app(None, self.db, self.rules, sched)
        app.jinja_env.undefined = StrictUndefined
        data = app.test_client().get("/api/progress").get_json()
        self.assertFalse(data["running"])
        self.assertEqual(data["progress"]["artist"], "A")
        self.assertEqual(data["progress"]["total"], 2)

    def test_maintenance_cleanup(self):
        self.storage_patch()
        (self.root / "downloads" / "Artist" / "1_test.jpg").unlink()
        client = self.make_client()
        client.get("/rules")
        token = self.csrf(client)
        resp = client.post("/maintenance/cleanup", data={"_csrf": token},
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.db.get(1))
        self.assertIn("清理完成", resp.data.decode())

    def test_nav_labels_and_page_title(self):
        html = self.make_client().get("/").data.decode()
        self.assertIn("<title>概览 · Pixiv Following Downloader</title>", html)
        self.assertIn(">概览<", html)
        self.assertIn(">画师<", html)
        self.assertIn(">本地画廊<", html)
        self.assertIn(">下载规则<", html)
        self.assertIn('class="nav-sep"', html)

    def test_dashboard_progress_render(self):
        sched = Scheduler(None, None, self.db, self.rules)
        sched._running = True
        sched._set_progress(index=1, total=2, artist="Artist", work="Test work")
        with mock.patch.object(webui, "DownloadQueue", StubQueue):
            app = webui.create_app(None, self.db, self.rules, sched)
        app.jinja_env.undefined = StrictUndefined
        html = app.test_client().get("/").data.decode()
        self.assertIn("当前进度", html)
        self.assertIn("正在处理 1/2 位画师：Artist", html)
        self.assertIn("Test work", html)

    def test_config_and_setup_have_no_target(self):
        self.storage_patch()
        client = self.make_client()
        cfg = client.get("/config").data.decode()
        self.assertNotIn('name="TARGET_USER_ID"', cfg)
        self.assertIn('name="PIXIV_DOWNLOAD_REFRESH_TOKEN"', cfg)

        config.PIXIV_REFRESH_TOKEN = ""
        setup = client.get("/setup").data.decode()
        self.assertNotIn('name="TARGET_USER_ID"', setup)
        self.assertIn('name="PIXIV_DOWNLOAD_REFRESH_TOKEN"', setup)

    def test_plan_toggle(self):
        self.db.replace_following([
            {"user_id": 55, "user_name": "Artist"},
            {"user_id": 66, "user_name": "Other"},
        ])
        self.storage_patch()
        client = self.make_client()
        client.get("/browse")
        token = self.csrf(client)

        client.post("/plan/toggle/55", data={
            "action": "add", "next": "/browse", "_csrf": token,
        })
        self.assertEqual(self.db.plan_ids(), {55})
        plan = client.get("/browse?view=plan").data.decode()
        self.assertIn("Artist", plan)
        self.assertIn("移出计划", plan)

        client.post("/plan/toggle/55", data={
            "action": "remove", "next": "/browse?view=plan", "_csrf": token,
        })
        self.assertEqual(self.db.plan_ids(), set())

    def test_plan_add_non_followed(self):
        api = FakeAPI()
        with mock.patch.object(webui, "DownloadQueue", return_value=StubQueue()):
            app = webui.create_app(api, self.db, self.rules, None)
        app.jinja_env.undefined = StrictUndefined
        client = app.test_client()
        client.get("/browse?view=plan")
        token = self.csrf(client)
        resp = client.post("/plan/add", data={
            "artist": "https://www.pixiv.net/users/999", "_csrf": token,
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db.plan_ids(), {999})
        self.assertIn("Artist 999", resp.data.decode())

    def test_privacy_filter(self):
        self.db.replace_following([
            {"user_id": 55, "user_name": "PublicArtist", "is_private": 0},
            {"user_id": 66, "user_name": "PrivateArtist", "is_private": 1},
        ])
        self.storage_patch()
        client = self.make_client()
        public = client.get("/browse?privacy=public").data.decode()
        self.assertIn("PublicArtist", public)
        self.assertNotIn("PrivateArtist", public)
        private = client.get("/browse?privacy=private").data.decode()
        self.assertIn("PrivateArtist", private)
        self.assertNotIn("PublicArtist", private)

    def test_download_account_used_for_works(self):
        main = FakeAPI()
        download = FakeAPI()
        with mock.patch.object(webui, "DownloadQueue", return_value=StubQueue()):
            app = webui.create_app(
                main, self.db, self.rules, None, download_api=download,
            )
        app.jinja_env.undefined = StrictUndefined
        html = app.test_client().get("/browse/55").data.decode()
        self.assertIn("新作品", html)
        self.assertEqual(download.user_illusts_calls, 1)
        self.assertEqual(main.user_illusts_calls, 0)

    def test_tag_export_import(self):
        self.storage_patch()
        self.rules.add_tag("风景", "include")
        self.rules.add_tag("R-18", "exclude")
        client = self.make_client()
        exported = client.get("/rules/tags/export")
        self.assertEqual(exported.status_code, 200)
        data = json.loads(exported.data)
        self.assertEqual(data["include"], ["风景"])
        self.assertEqual(data["exclude"], ["R-18"])

        client.get("/rules")
        token = self.csrf(client)
        payload = json.dumps({"include": ["人物"], "exclude": ["R-18"]}).encode()
        resp = client.post("/rules/tags/import", data={
            "file": (io.BytesIO(payload), "tags.json"),
            "mode": "replace",
            "_csrf": token,
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["tag"] for t in self.rules.tags("include")], ["人物"])
        self.assertEqual([t["tag"] for t in self.rules.tags("exclude")], ["R-18"])

    def test_api_status(self):
        client = self.make_client()
        resp = client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["authed"])
        self.assertEqual(data["db"]["downloaded"], 1)


if __name__ == "__main__":
    unittest.main()

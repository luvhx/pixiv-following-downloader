import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

import config  # noqa: E402

import pixiv  # noqa: E402

_THROTTLE_KEYS = ("REQUEST_DELAY", "REQUEST_JITTER", "DOWNLOAD_DELAY", "MAX_RETRIES")
_THROTTLE_ORIG = {}


def setUpModule():
    # 请求节流现在来自配置；测试里不要真的 sleep，结束后恢复
    _THROTTLE_ORIG.update({k: getattr(config, k) for k in _THROTTLE_KEYS})
    config.REQUEST_DELAY = 0.0
    config.REQUEST_JITTER = 0.0
    config.DOWNLOAD_DELAY = 0.0
    config.MAX_RETRIES = 1


def tearDownModule():
    for key, value in _THROTTLE_ORIG.items():
        setattr(config, key, value)
from database import DownloadDB  # noqa: E402


class FilterTest(unittest.TestCase):
    def _illust(self, title="Title", bookmarks=0, tags=("tagA", "tagB")):
        return {
            "id": 1,
            "title": title,
            "total_bookmarks": bookmarks,
            "tags": [
                {"name": name, "translated_name": f"{name}-en"} for name in tags
            ],
        }

    def test_should_exclude_by_tag_and_title(self):
        illust = self._illust(title="夏日海边")
        self.assertTrue(pixiv.should_exclude(illust, ["taga"]))
        self.assertTrue(pixiv.should_exclude(illust, ["tagb-en"]))
        self.assertTrue(pixiv.should_exclude(illust, ["海边"]))
        self.assertFalse(pixiv.should_exclude(illust, ["tagc"]))
        self.assertFalse(pixiv.should_exclude(illust, []))

    def test_should_exclude_is_case_insensitive(self):
        illust = self._illust(tags=("Landscape",))
        self.assertTrue(pixiv.should_exclude(illust, ["landscape"]))
        self.assertTrue(pixiv.should_exclude(illust, ["LANDSCAPE"]))

    def test_matches_include(self):
        illust = self._illust(tags=("风景", "天空"))
        self.assertTrue(pixiv.matches_include(illust, ["风景"]))
        # include 只做精确 tag 匹配，不做标题模糊匹配
        self.assertFalse(pixiv.matches_include(illust, ["风"]))
        self.assertTrue(pixiv.matches_include(illust, []))

    def test_bookmark_requirement(self):
        illust = self._illust(bookmarks=50)
        self.assertTrue(pixiv.meets_bookmark_requirement(illust, 0))
        self.assertTrue(pixiv.meets_bookmark_requirement(illust, 50))
        self.assertFalse(pixiv.meets_bookmark_requirement(illust, 51))


class ThumbAndUrlTest(unittest.TestCase):
    def test_thumb_from_meta_pages(self):
        illust = {
            "meta_pages": [{"image_urls": {"medium": "m0", "large": "l0"}}],
            "image_urls": {"medium": "top"},
        }
        self.assertEqual(pixiv.select_thumb_url(illust), "m0")

    def test_thumb_from_top_level(self):
        illust = {"image_urls": {"medium": "top", "large": "l"}}
        self.assertEqual(pixiv.select_thumb_url(illust), "top")

    def test_thumb_from_single_page(self):
        illust = {"meta_single_page": {"medium_image_url": "single"}}
        self.assertEqual(pixiv.select_thumb_url(illust), "single")

    def test_thumb_from_original_url(self):
        illust = {"meta_single_page": {"original_image_url":
            "https://i.pximg.net/img-original/img/2024/01/01/00/00/00/1_p0.jpg"}}
        self.assertEqual(
            pixiv.select_thumb_url(illust),
            "https://i.pximg.net/c/540x540_70/img-master/img/2024/01/01/00/00/00/1_p0_master1200.jpg",
        )

    def test_thumb_missing(self):
        self.assertEqual(pixiv.select_thumb_url({}), "")

    def test_collect_urls_multi_page(self):
        illust = {
            "meta_pages": [
                {"page_index": 0, "image_urls": {"original": "o0", "large": "l0"}},
                {"page_index": 1, "image_urls": {"original": "o1", "large": "l1"}},
            ]
        }
        self.assertEqual(
            pixiv.collect_image_urls(illust, kind="original"),
            [(0, "o0"), (1, "o1")],
        )
        self.assertEqual(
            pixiv.collect_image_urls(illust, kind="original", first_page_only=True),
            [(0, "o0")],
        )
        # 缺 original 时回落到 large
        self.assertEqual(
            pixiv.collect_image_urls(illust, kind="medium"),
            [(0, "l0"), (1, "l1")],
        )

    def test_collect_urls_single(self):
        illust = {"meta_single_page": {
            "original_image_url": "o", "large_image_url": "l", "medium_image_url": "m",
        }}
        self.assertEqual(pixiv.collect_image_urls(illust, "original"), [(0, "o")])
        self.assertEqual(pixiv.collect_image_urls(illust, "large"), [(0, "l")])
        self.assertEqual(pixiv.collect_image_urls(illust, "medium"), [(0, "m")])

    def test_sanitize_filename(self):
        self.assertEqual(pixiv.sanitize_filename('a/b:c*d?e"f'), "a_b_c_d_e_f")
        self.assertEqual(pixiv.sanitize_filename("CON"), "_CON")
        self.assertEqual(pixiv.sanitize_filename("  "), "untitled")
        long_name = "あ" * 200
        self.assertLessEqual(len(pixiv.sanitize_filename(long_name).encode()), 180)


class FakeApi:
    def __init__(self, pages=None):
        self.pages = pages or []
        self.calls = 0

    def user_illusts(self, user_id, type="illust", offset=None, **kwargs):
        self.calls += 1
        self.user_id, self.type = user_id, type
        if self.pages:
            return self.pages.pop(0)
        return {"illusts": [], "next_url": None}

    def parse_qs(self, url):
        if not url:
            return None
        return {"user_id": self.user_id, "type": self.type, "offset": 1}

    def download(self, url, path, name):
        with open(os.path.join(path, name), "wb") as f:
            f.write(b"\xff\xd8data")


class FakeStorage:
    def __init__(self, fail_on=None):
        self.saved = []
        self.deleted = []
        self.fail_on = fail_on

    def exists(self, path):
        return False

    def save(self, tmp, relative):
        if self.fail_on is not None and len(self.saved) == self.fail_on:
            raise RuntimeError("boom")
        self.saved.append(relative)
        return relative

    def delete(self, path):
        self.deleted.append(path)
        return True


class FollowingFetchTest(unittest.TestCase):
    def test_error_raises(self):
        class Api:
            def user_following(self, user_id, **kwargs):
                return {"error": {"message": "invalid token"}}

            @staticmethod
            def parse_qs(url):
                return None

        with self.assertRaises(RuntimeError):
            pixiv.get_following_users(Api(), "1")

    def test_collects_previews(self):
        class Api:
            def user_following(self, user_id, **kwargs):
                return {
                    "user_previews": [{
                        "user": {"id": 5, "name": "A"},
                        "illusts": [
                            {"id": 1, "title": "one", "total_bookmarks": 10,
                             "page_count": 1,
                             "image_urls": {"medium": "https://i.pximg.net/1.jpg"}},
                            {"id": 2, "title": "two", "total_bookmarks": 50,
                             "page_count": 2,
                             "image_urls": {"medium": "https://i.pximg.net/2.jpg"}},
                            {"id": 3, "title": "three", "total_bookmarks": 80,
                             "page_count": 1,
                             "image_urls": {"medium": "https://i.pximg.net/3.jpg"}},
                            {"id": 4, "title": "four", "total_bookmarks": 1,
                             "page_count": 1,
                             "image_urls": {"medium": "https://i.pximg.net/4.jpg"}},
                        ],
                    }],
                    "next_url": None,
                }

            @staticmethod
            def parse_qs(url):
                return None

        users = pixiv.get_following_users(Api(), "1")
        previews = users[0]["previews"]
        self.assertEqual([p["illust_id"] for p in previews], [3, 2, 1])
        self.assertEqual(previews[0]["thumb_url"], "https://i.pximg.net/3.jpg")
        self.assertEqual(previews[0]["bookmarks"], 80)

    def test_collects_avatar(self):
        class Api:
            def user_following(self, user_id, **kwargs):
                return {
                    "user_previews": [{
                        "user": {
                            "id": 5, "name": "A",
                            "profile_image_urls": {"medium": "http://a"},
                        },
                    }],
                    "next_url": None,
                }

            @staticmethod
            def parse_qs(url):
                return None

        users = pixiv.get_following_users(Api(), "1")
        self.assertEqual(users, [{
            "user_id": 5, "user_name": "A", "avatar_url": "http://a", "previews": [],
        }])


class IncrementalFetchTest(unittest.TestCase):
    def _page(self, ids, next_url=None):
        return {
            "illusts": [{"id": i, "title": str(i)} for i in ids],
            "next_url": next_url,
        }

    def test_stops_at_watermark(self):
        api = FakeApi([
            self._page([100, 99], "next"),
            self._page([98, 97]),
        ])
        illusts, complete = pixiv.get_user_illusts(api, 1, stop_at_id=98)
        self.assertEqual([i["id"] for i in illusts], [100, 99])
        self.assertTrue(complete)

    def test_stops_when_all_seen(self):
        api = FakeApi([self._page([50, 49])])
        illusts, complete = pixiv.get_user_illusts(api, 1, stop_at_id=60)
        self.assertEqual(illusts, [])
        self.assertTrue(complete)

    def test_page_limit_marks_incomplete(self):
        api = FakeApi([
            self._page([100, 99], "next"),
            self._page([98, 97], "next"),
        ])
        with mock.patch.object(pixiv, "MAX_ILLUST_PAGES", 1):
            illusts, complete = pixiv.get_user_illusts(api, 1, stop_at_id=0)
        self.assertEqual([i["id"] for i in illusts], [100, 99])
        self.assertFalse(complete)

    def test_api_error_marks_incomplete(self):
        api = FakeApi([
            self._page([100, 99], "next"),
            {"error": {"message": "rate limited"}},
        ])
        illusts, complete = pixiv.get_user_illusts(api, 1)
        self.assertEqual([i["id"] for i in illusts], [100, 99])
        self.assertFalse(complete)

    def test_all_types_merged_and_sorted(self):
        api = FakeApi([
            self._page([100]),   # illust
            self._page([101]),   # manga
        ])
        illusts, complete = pixiv.get_user_illusts(api, 1, illust_type="all")
        self.assertEqual([i["id"] for i in illusts], [101, 100])
        self.assertTrue(complete)


class NamingTest(unittest.TestCase):
    def _illust(self):
        return {
            "id": 12, "title": "Title", "type": "illust",
            "user": {"id": 34, "name": "Artist"},
            "create_date": "2024-01-02T03:04:05+09:00",
        }

    def test_default_paths(self):
        illust = self._illust()
        self.assertEqual(
            pixiv.build_relative_path(illust, 0, 1, ".jpg", {}),
            "Artist/12_Title.jpg",
        )
        self.assertEqual(
            pixiv.build_relative_path(illust, 1, 3, ".jpg", {}),
            "Artist/12_p1_Title.jpg",
        )

    def test_custom_templates(self):
        illust = self._illust()
        rules = {
            "folder_template": "{date}/{artist_id}",
            "filename_template": "{id}-{title}-{page}",
        }
        self.assertEqual(
            pixiv.build_relative_path(illust, 1, 3, ".png", rules),
            "2024-01-02/34/12-Title-1.png",
        )
        rules = {"filename_template": "{id}.{ext}"}
        self.assertEqual(
            pixiv.build_relative_path(illust, 0, 1, ".jpg", rules),
            "Artist/12.jpg",
        )

    def test_custom_template_without_page_gets_suffix(self):
        illust = self._illust()
        rules = {"filename_template": "{title}"}
        self.assertEqual(
            pixiv.build_relative_path(illust, 2, 3, ".jpg", rules),
            "Artist/Title_p2.jpg",
        )


class UgoiraTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DownloadDB(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ugoira_zip_fallback(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("000000.jpg", b"frame0")
            archive.writestr("000001.jpg", b"frame1")

        class Api:
            access_token = "x"
            zip_bytes = buffer.getvalue()

            def ugoira_metadata(self, illust_id):
                return {"ugoira_metadata": {
                    "zip_urls": {"medium": "https://i.pximg.net/x.zip"},
                    "frames": [
                        {"file": "000000.jpg", "delay": 40},
                        {"file": "000001.jpg", "delay": 60},
                    ],
                }}

            def download(self, url, path, name):
                with open(os.path.join(path, name), "wb") as f:
                    f.write(self.zip_bytes)

        storage = FakeStorage()
        illust = {
            "id": 77, "title": "Anim", "type": "ugoira", "page_count": 1,
            "user": {"id": 5, "name": "Artist"},
            "tags": [{"name": "动图"}],
            "image_urls": {"medium": "https://i.pximg.net/thumb.jpg"},
            "total_bookmarks": 12,
        }
        with mock.patch.object(pixiv, "get_storage", return_value=storage):
            ok = pixiv.download_illust(
                Api(), illust, self.db, {"ugoira_format": "zip"},
            )
        self.assertTrue(ok)
        row = self.db.get(77)
        self.assertEqual(row["illust_type"], "ugoira")
        self.assertEqual(row["bookmarks"], 12)
        self.assertIn(".zip", row["files"])
        self.assertEqual(len(storage.saved), 1)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DownloadDB(os.path.join(self.tmp.name, "t.db"))
        self.api = FakeApi()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _illust(self, pages=2):
        return {
            "id": 1001,
            "title": "Test",
            "page_count": pages,
            "user": {"id": 55, "name": "Artist"},
            "tags": [{"name": "tagA"}, {"name": "tagB", "translated_name": "tagB-en"}],
            "total_bookmarks": 42,
            "meta_pages": [
                {"page_index": i, "image_urls": {
                    "original": f"https://i.pximg.net/img-original/img/2024/01/01/00/00/00/1001_p{i}.jpg",
                }}
                for i in range(pages)
            ],
        }

    def test_download_success_records_metadata(self):
        storage = FakeStorage()
        with mock.patch.object(pixiv, "get_storage", return_value=storage):
            ok = pixiv.download_illust(self.api, self._illust(), self.db, {})
        self.assertTrue(ok)
        row = self.db.get(1001)
        self.assertEqual(row["author_name"], "Artist")
        self.assertEqual(row["page_count"], 2)
        self.assertEqual(len(storage.saved), 2)
        self.assertEqual(self.db.tags_for(1001), ["tagA", "tagB", "tagB-en"])
        self.assertEqual(
            row["thumb_url"],
            "https://i.pximg.net/c/540x540_70/img-master/img/2024/01/01/00/00/00/1001_p0_master1200.jpg",
        )
        self.assertEqual(row["illust_type"], None)

    def test_download_skips_existing(self):
        self.db.mark_downloaded(1001, "x", 1, "a", [], 1)
        storage = FakeStorage()
        with mock.patch.object(pixiv, "get_storage", return_value=storage):
            self.assertTrue(pixiv.download_illust(self.api, self._illust(), self.db, {}))
        self.assertEqual(storage.saved, [])

    def test_download_rolls_back_on_upload_failure(self):
        storage = FakeStorage(fail_on=1)
        with mock.patch.object(pixiv, "get_storage", return_value=storage):
            ok = pixiv.download_illust(self.api, self._illust(), self.db, {})
        self.assertFalse(ok)
        # 第一个已写入的文件被回滚
        self.assertEqual(storage.deleted, ["Artist/1001_p0_Test.jpg"])
        self.assertIsNone(self.db.get(1001))

    def test_download_no_urls(self):
        illust = self._illust()
        illust["meta_pages"] = []
        illust["meta_single_page"] = {}
        illust["image_urls"] = {}
        with mock.patch.object(pixiv, "get_storage", return_value=FakeStorage()):
            self.assertFalse(pixiv.download_illust(self.api, illust, self.db, {}))

    def test_first_page_only(self):
        storage = FakeStorage()
        with mock.patch.object(pixiv, "get_storage", return_value=storage):
            ok = pixiv.download_illust(
                self.api, self._illust(pages=3), self.db,
                {"first_page_only": True},
            )
        self.assertTrue(ok)
        self.assertEqual(len(storage.saved), 1)


if __name__ == "__main__":
    unittest.main()

import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

from database import DownloadDB, normalize_tag  # noqa: E402


class DownloadDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.db = DownloadDB(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _add(self, illust_id=1, tags=("tagA", "tagB"), title="作品",
             author_id=10, author_name="画师", favorite=False, **kw):
        self.db.mark_downloaded(
            illust_id, title, author_id, author_name, list(tags), 2,
            files=[f"{author_name}/{illust_id}_x.jpg"], **kw,
        )
        if favorite:
            self.db.set_favorite(illust_id, True)

    def test_mark_and_tags(self):
        self._add(thumb_url="https://i.pximg.net/1.jpg", illust_type="manga")
        row = self.db.get(1)
        self.assertEqual(row["title"], "作品")
        self.assertEqual(self.db.tags_for(1), ["tagA", "tagB"])
        self.assertTrue(self.db.is_downloaded(1))
        self.assertEqual(self.db.downloaded_ids([1, 2]), {1})
        self.assertEqual(row["thumb_url"], "https://i.pximg.net/1.jpg")
        self.assertEqual(row["illust_type"], "manga")
        self.assertNotIn("tags", row)

    def test_search_and_tag_filter(self):
        self._add(1, tags=("风景", "tagA"), title="海边")
        self._add(2, tags=("人物",), title="室内")

        by_tag = self.db.list_downloaded(tag="风景")
        self.assertEqual([r["illust_id"] for r in by_tag["rows"]], [1])

        by_q_tag = self.db.list_downloaded(q="风景")
        self.assertEqual([r["illust_id"] for r in by_q_tag["rows"]], [1])

        by_q_title = self.db.list_downloaded(q="海边")
        self.assertEqual([r["illust_id"] for r in by_q_title["rows"]], [1])

        by_q_author = self.db.list_downloaded(q="画师")
        self.assertEqual(by_q_author["total"], 2)

    def test_tag_with_comma_is_exact(self):
        self._add(1, tags=("a,b", "c"))
        self.assertEqual([r["illust_id"] for r in self.db.list_by_tag("a,b")], [1])
        self.assertEqual(self.db.list_by_tag("a"), [])
        self.assertEqual([r["illust_id"] for r in self.db.list_downloaded(tag="a,b")["rows"]], [1])

    def test_tag_filter_is_case_insensitive(self):
        self._add(1, tags=("Landscape",))
        self.assertEqual(self.db.list_downloaded(tag="landscape")["total"], 1)
        self.assertEqual(normalize_tag("  Landscape "), "landscape")

    def test_bookmarks_and_sort(self):
        self._add(1, title="A")
        self._add(2, title="B")
        self.db.mark_downloaded(3, "C", 10, "画师", ["x"], 5, files=["a.jpg"],
                                bookmarks=500)
        self.assertEqual(self.db.get(3)["bookmarks"], 500)

        ids = lambda sort: [r["illust_id"] for r in self.db.list_downloaded(sort=sort)["rows"]]
        self.assertEqual(ids("bookmarks")[:1], [3])
        self.assertEqual(ids("pages")[:1], [3])
        self.assertEqual(ids("title"), [1, 2, 3])
        # 未知排序回落到最新
        self.assertEqual(ids("bogus"), ids("new"))

    def test_favorites(self):
        self._add(1)
        self._add(2, favorite=True)
        self.assertEqual(self.db.list_downloaded(favorites_only=True)["total"], 1)
        self.assertTrue(self.db.set_favorite(1, True))
        self.assertEqual(self.db.list_downloaded(favorites_only=True)["total"], 2)
        self.assertFalse(self.db.set_favorite(99, True))

    def test_favorite_tags(self):
        self.assertTrue(self.db.set_tag_favorite("风景"))
        self.assertTrue(self.db.is_tag_favorite(" 风景 "))
        self.assertEqual(self.db.favorite_tags(), ["风景"])
        self.db.set_tag_favorite("风景", False)
        self.assertFalse(self.db.is_tag_favorite("风景"))

    def test_delete_cascades_tags(self):
        self._add(1)
        self.assertTrue(self.db.delete(1))
        self.assertFalse(self.db.is_downloaded(1))
        rows = self.db._conn().execute(
            "SELECT COUNT(*) AS c FROM illust_tags"
        ).fetchone()
        self.assertEqual(rows["c"], 0)

    def test_stats_and_options_cache_invalidated(self):
        self._add(1, tags=("tagA",))
        self.assertEqual(self.db.stats()["downloaded"], 1)
        self.assertIn("tagA", self.db.tag_options())
        self.assertEqual(self.db.artist_options()[0]["count"], 1)

        self._add(2, tags=("tag3",), author_id=11, author_name="画师二")
        self.assertEqual(self.db.stats()["downloaded"], 2)
        self.assertIn("tag3", self.db.tag_options())
        self.assertEqual(len(self.db.artist_options()), 2)

    def test_artist_tag_stats(self):
        self._add(1, tags=("风景", "天空"))
        self._add(2, tags=("风景",))
        stats = self.db.artist_tag_stats(10)
        self.assertEqual(stats[0]["tag"], "风景")
        self.assertEqual(stats[0]["count"], 2)

    def test_watermarks(self):
        self.assertEqual(self.db.artist_watermark(7), 0)
        self.db.set_artist_watermark(7, "画师", 100)
        self.assertEqual(self.db.artist_watermark(7), 100)
        # 不回退
        self.db.set_artist_watermark(7, "画师", 50)
        self.assertEqual(self.db.artist_watermark(7), 100)
        self.db.set_artist_watermark(7, "画师", 120)
        self.assertEqual(self.db.watermarks()[7], 120)

    def test_following_previews(self):
        previews = [{
            "illust_id": 11, "title": "preview", "thumb_url": "https://i.pximg.net/x.jpg",
            "page_count": 2, "bookmarks": 30, "type": "illust",
        }]
        self.db.replace_following([
            {"user_id": 1, "user_name": "A", "previews": previews},
            {"user_id": 2, "user_name": "B"},
        ])
        rows = {r["artist_id"]: r for r in self.db.following()}
        self.assertEqual(rows[1]["previews"], previews)
        self.assertEqual(rows[2]["previews"], [])

    def test_following_cache(self):
        self.db.replace_following([
            {"user_id": 1, "user_name": "A", "avatar_url": "http://a"},
            {"user_id": 2, "user_name": "B"},
        ])
        self.assertEqual(len(self.db.following()), 2)
        self.assertIsNotNone(self.db.following_updated_at())

        # 全量替换会删除已取关的画师
        self.db.replace_following([{"user_id": 2, "user_name": "B2"}])
        names = [r["artist_name"] for r in self.db.following()]
        self.assertEqual(names, ["B2"])

    def test_ignored_artists_survive_refresh(self):
        self.db.replace_following([{"user_id": 1, "user_name": "A"}])
        self.assertTrue(self.db.set_artist_ignored(1, True))
        self.assertEqual(self.db.ignored_artist_ids(), {1})
        # 刷新关注列表不会清掉暂停标记
        self.db.replace_following([{"user_id": 1, "user_name": "A2"}])
        self.assertEqual(self.db.following()[0]["ignored"], 1)

    def test_settings(self):
        self.assertEqual(self.db.get_settings(), {})
        self.db.set_settings({"download_kind": "large"})
        self.db.set_settings({"download_kind": "medium", "max_per_artist": "3"})
        self.assertEqual(
            self.db.get_settings(),
            {"download_kind": "medium", "max_per_artist": "3"},
        )

    def test_related_works(self):
        self._add(1, tags=("风景", "海"))
        self._add(2, tags=("风景",), author_id=11, author_name="别人")
        self._add(3, tags=("人物",), author_id=12, author_name="第三")
        related = self.db.related(1, author_id=10, tags=["风景"])
        ids = [r["illust_id"] for r in related]
        self.assertIn(2, ids)
        self.assertNotIn(1, ids)
        self.assertNotIn(3, ids)

    def test_concurrent_writes(self):
        errors = []

        def worker(base):
            try:
                for i in range(10):
                    self._add(base + i, tags=(f"t{base + i}",))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.db.stats()["downloaded"], 40)


class BookmarksMigrationTest(unittest.TestCase):
    def test_old_table_gets_bookmarks_column(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE downloaded (
                illust_id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER,
                author_name TEXT, page_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                files TEXT, favorite INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO downloaded (illust_id, title, files) VALUES (1, 'old', '[]')"
        )
        conn.commit()
        conn.close()

        db = DownloadDB(path)
        try:
            self.assertEqual(db.get(1)["bookmarks"], 0)
            cols = [r[1] for r in db._conn().execute("PRAGMA table_info(downloaded)")]
            self.assertIn("bookmarks", cols)
        finally:
            db.close()
            tmp.cleanup()


class FollowingMigrationTest(unittest.TestCase):
    def test_old_following_table_gets_previews_column(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE following (
                artist_id INTEGER PRIMARY KEY, artist_name TEXT NOT NULL DEFAULT '',
                avatar_url TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO following (artist_id, artist_name) VALUES (1, 'A')")
        conn.commit()
        conn.close()

        db = DownloadDB(path)
        try:
            self.assertEqual(db.following()[0]["previews"], [])
            self.assertEqual(db.following()[0]["ignored"], 0)
            cols = [r[1] for r in db._conn().execute("PRAGMA table_info(following)")]
            self.assertIn("previews", cols)
            self.assertIn("ignored", cols)
        finally:
            db.close()
            tmp.cleanup()


class LegacyMigrationTest(unittest.TestCase):
    def test_old_schema_migrated(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE downloaded (
                illust_id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER,
                author_name TEXT, tags TEXT, page_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                files TEXT, favorite INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO downloaded (illust_id, title, author_id, author_name, "
            "tags, page_count, files, favorite) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "旧作品", 10, "画师", "tagA,tagB,含,逗号", 3, None, 1),
        )
        conn.execute(
            "CREATE TABLE favorite_tags (tag TEXT PRIMARY KEY, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO favorite_tags (tag) VALUES ('风景')")
        conn.commit()
        conn.close()

        db = DownloadDB(path)
        try:
            row = db.get(1)
            self.assertEqual(row["favorite"], 1)
            self.assertEqual(row["page_count"], 3)
            self.assertEqual(db.tags_for(1), ["tagA", "tagB", "含", "逗号"])
            self.assertEqual(db.favorite_tags(), ["风景"])
            cols = [r[1] for r in db._conn().execute("PRAGMA table_info(downloaded)")]
            self.assertNotIn("tags", cols)
            self.assertIn("thumb_url", cols)
        finally:
            db.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

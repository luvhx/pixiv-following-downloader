import os
import sys
import tempfile
import types
import unittest

_dotenv = types.ModuleType("dotenv")
_dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", _dotenv)

import gallery  # noqa: E402
from database import DownloadDB  # noqa: E402


class FakeStorage:
    def __init__(self, files=None, delete_error=None):
        self.files = files or []
        self.delete_error = delete_error
        self.deleted = []

    def list_files(self, relative_dir):
        return list(self.files)

    def delete(self, path):
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        if path in self.files:
            self.files.remove(path)
            self.deleted.append(path)
            return True
        return False


class GalleryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DownloadDB(os.path.join(self.tmp.name, "t.db"))
        self.db.mark_downloaded(
            1, "作品", 10, "Artist", ["tag"], 2,
            files=["Artist/1_a.jpg", "Artist/1_b.jpg"],
        )
        self.row = self.db.get(1)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_safe_rel(self):
        self.assertEqual(gallery._safe_rel("Artist/1.jpg"), "Artist/1.jpg")
        for bad in ("/etc/passwd", "../x", "Artist/../../x", "C:/x", ""):
            with self.assertRaises(ValueError):
                gallery._safe_rel(bad)

    def test_resolve_from_row(self):
        storage = FakeStorage()
        files = gallery.resolve_files(storage, self.db, self.row)
        self.assertEqual(files, ["Artist/1_a.jpg", "Artist/1_b.jpg"])

    def test_resolve_legacy_scan_and_persist(self):
        self.db.mark_downloaded(2, "旧作品", 10, "Artist", [], 1, files=[])
        row = self.db.get(2)
        storage = FakeStorage(files=[
            "Artist/2_a.jpg", "Artist/2_b.jpg", "Artist/1_a.jpg",
        ])
        files = gallery.resolve_files(storage, self.db, row)
        self.assertEqual(files, ["Artist/2_a.jpg", "Artist/2_b.jpg"])
        self.assertEqual(
            self.db.get(2)["files"], '["Artist/2_a.jpg", "Artist/2_b.jpg"]'
        )

    def test_delete_removes_record_when_all_files_gone(self):
        storage = FakeStorage(files=["Artist/1_a.jpg", "Artist/1_b.jpg"])
        result = gallery.delete_illust(self.db, storage, self.row)
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted"], 2)
        self.assertIsNone(self.db.get(1))
        self.assertEqual(self.db.tags_for(1), [])

    def test_delete_keeps_record_when_file_delete_fails(self):
        storage = FakeStorage(delete_error="网络错误")
        result = gallery.delete_illust(self.db, storage, self.row)
        self.assertFalse(result["removed"])
        self.assertEqual(result["errors"], 2)
        self.assertIsNotNone(self.db.get(1))

    def test_delete_missing_file_still_removes_record(self):
        storage = FakeStorage(files=[])  # 文件已不存在
        result = gallery.delete_illust(self.db, storage, self.row)
        self.assertTrue(result["removed"])
        self.assertEqual(result["missing"], 2)
        self.assertIsNone(self.db.get(1))

    def test_image_mime(self):
        self.assertEqual(gallery.image_mime("a.JPG"), "image/jpeg")
        self.assertEqual(gallery.image_mime("a.webp"), "image/webp")
        self.assertEqual(gallery.image_mime("a.bin"), "application/octet-stream")


if __name__ == "__main__":
    unittest.main()

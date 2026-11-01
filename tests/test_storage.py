import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import storage
from storage import LocalStorage, WebDAVStorage


class FakeResponse:
    def __init__(self, status=200, chunks=(b"abc", b"def"), headers=None):
        self.status_code = status
        self.headers = headers if headers is not None else {
            "Content-Length": str(sum(len(c) for c in chunks))
        }
        self.text = ""
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=64 * 1024):
        yield from self._chunks

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeSession:
    def __init__(self, *responses, head_responses=None):
        self.responses = list(responses)
        self.head_responses = list(head_responses or [])
        self.calls = []

    def _next(self, method, url):
        self.calls.append((method, url))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def request(self, method, url, **kwargs):
        if method == "HEAD":
            self.calls.append((method, url))
            if self.head_responses:
                return self.head_responses.pop(0)
            return FakeResponse(
                status=200, chunks=(),
                headers={"Content-Length": "5"},
            )
        return self._next(method, url)

    def put(self, url, data=None, timeout=None):
        return self._next("PUT", url)

    def close(self):
        pass


class WebDAVStreamTest(unittest.TestCase):
    def setUp(self):
        self.ws = WebDAVStorage(
            "https://dav.example.com/dav", "u", "p", root="pixiv",
        )

    def tearDown(self):
        self.ws._session.close()

    def test_download_to_file_object(self):
        self.ws._session = FakeSession(FakeResponse())
        buf = io.BytesIO()
        self.ws.download_to("a/图 1.jpg", buf)
        self.assertEqual(buf.getvalue(), b"abcdef")

    def test_download_to_path_and_cleanup_on_error(self):
        self.ws._session = FakeSession(FakeResponse(status=500))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.jpg"
            with self.assertRaises(RuntimeError):
                self.ws.download_to("a.jpg", target)
            self.assertFalse(target.exists())

    def test_open_stream(self):
        resp = FakeResponse()
        self.ws._session = FakeSession(resp)
        upstream = self.ws.open_stream("a.jpg")
        self.assertEqual(b"".join(upstream.iter_content()), b"abcdef")
        upstream.close()
        self.assertTrue(resp.closed)

    def test_open_stream_error_closes_response(self):
        resp = FakeResponse(status=404)
        self.ws._session = FakeSession(resp)
        with self.assertRaises(RuntimeError):
            self.ws.open_stream("a.jpg")
        self.assertTrue(resp.closed)

    def test_save_creates_full_root_directory(self):
        self.ws = WebDAVStorage(
            "https://dav.example.com/dav", "u", "p",
            root="115/pixiv/thumbs",
        )
        session = FakeSession(FakeResponse(status=201))
        self.ws._session = session
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "1.jpg"
            source.write_bytes(b"thumb")
            self.ws.save(str(source), "1.jpg")
        self.assertIn("https://dav.example.com/dav/115", self.ws._dir_cache)
        self.assertIn("https://dav.example.com/dav/115/pixiv", self.ws._dir_cache)
        self.assertIn(
            "https://dav.example.com/dav/115/pixiv/thumbs",
            self.ws._dir_cache,
        )
        puts = [c for c in session.calls if c[0] == "PUT"]
        self.assertEqual(
            puts[-1],
            ("PUT", "https://dav.example.com/dav/115/pixiv/thumbs/1.jpg"),
        )
        self.assertTrue(any(c[0] == "HEAD" for c in session.calls))

    def test_save_retries_429(self):
        self.ws._session = FakeSession(
            FakeResponse(status=201),
            FakeResponse(status=429),
            FakeResponse(status=201),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "1.jpg"
            source.write_bytes(b"thumb")
            with mock.patch.object(storage.time, "sleep"):
                self.ws.save(str(source), "1.jpg")
        puts = [c for c in self.ws._session.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 2)

    def test_save_waits_until_remote_file_is_visible(self):
        self.ws._session = FakeSession(
            FakeResponse(status=201),
            head_responses=[
                FakeResponse(status=404),
                FakeResponse(
                    status=200, chunks=(), headers={"Content-Length": "5"},
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "1.jpg"
            source.write_bytes(b"thumb")
            with mock.patch.object(storage.time, "sleep"):
                self.ws.save(str(source), "1.jpg")
        heads = [c for c in self.ws._session.calls if c[0] == "HEAD"]
        self.assertEqual(len(heads), 2)


class LocalStorageTest(unittest.TestCase):
    def test_save_delete_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStorage(tmp)
            source = Path(tmp) / "temp.bin"
            source.write_bytes(b"data")
            dest = store.save(str(source), "Artist/1.jpg")
            self.assertTrue(Path(dest).exists())
            self.assertEqual(Path(dest).read_bytes(), b"data")
            self.assertTrue(store.exists("Artist/1.jpg"))
            self.assertTrue(store.delete("Artist/1.jpg"))
            self.assertFalse(store.exists("Artist/1.jpg"))
            with self.assertRaises(ValueError):
                store.abs_path("../outside.jpg")


if __name__ == "__main__":
    unittest.main()

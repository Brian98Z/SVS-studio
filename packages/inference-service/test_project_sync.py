from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch


@contextmanager
def dav_listing(xml):
    class Handler(BaseHTTPRequestHandler):
        def do_PROPFIND(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = xml.encode("utf-8")
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}/dav/"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("test_project_sync_module", HERE / "inference_service" / "project_sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class ProjectSyncValidationTests(unittest.TestCase):
    def test_webdav_collection_requires_tls_except_literal_loopback(self):
        self.assertEqual(sync._validate_url("https://dav.example/collection"), "https://dav.example/collection/")
        self.assertEqual(sync._validate_url("http://[::1]:8080/dav"), "http://[::1]:8080/dav/")
        encoded = sync._validate_url("https://dav.example/歌曲/")
        self.assertEqual(encoded, "https://dav.example/%E6%AD%8C%E6%9B%B2/")
        self.assertEqual(sync._validate_url(encoded), encoded)
        for url in (
            "http://dav.example/collection",
            "http://127.0.0.2/dav",
            "https://user:secret@dav.example/dav",
            "https://dav.example/a/../dav",
        ):
            with self.subTest(url=url), self.assertRaises(sync.SyncError):
                sync._validate_url(url)

    def test_remote_name_rejects_traversal_and_url_delimiters(self):
        base = "https://dav.example/collection/"
        for name in ("../x.svsbundle", "%2e%2e.svsbundle", "a/b.svsbundle", "a\\b.svsbundle", "a?x.svsbundle", "a#x.svsbundle", "a.svsbundle/"):
            with self.subTest(name=name), self.assertRaises(sync.SyncError):
                sync._target(base, name)
        self.assertEqual(sync._target(base, "歌曲.svsbundle").path, "/collection/%E6%AD%8C%E6%9B%B2.svsbundle")

    def test_only_strong_quoted_etags_are_accepted(self):
        self.assertEqual(sync._strong_etag({"etag": '"v1"'}), '"v1"')
        for value in (None, "W/\"weak\"", "unquoted", '"partial'):
            with self.subTest(value=value):
                self.assertIsNone(sync._strong_etag({"etag": value}))

    def test_listing_uses_only_successful_dav_properties(self):
        xml = '''<d:multistatus xmlns:d="DAV:" xmlns:x="urn:extension">
          <d:response><d:href>/dav/song.svsbundle</d:href>
            <d:propstat><d:prop><d:getetag>"valid"</d:getetag>
              <d:getcontentlength>42</d:getcontentlength><d:resourcetype/>
              <x:getetag>unrelated-extension</x:getetag></d:prop>
              <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
            <d:propstat><d:prop><d:getlastmodified>unavailable</d:getlastmodified></d:prop>
              <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
          </d:response>
          <d:response><d:href>/dav/folder.svsbundle</d:href><d:propstat>
            <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
            <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
          <d:response><d:href>/dav/denied.svsbundle</d:href><d:propstat>
            <d:prop><d:getetag>"denied"</d:getetag></d:prop>
            <d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat></d:response>
        </d:multistatus>'''
        with dav_listing(xml) as state, patch.object(sync, "_profile_credentials", return_value=("test", "test")):
            self.assertEqual(sync._list(state), [
                {"name": "song.svsbundle", "etag": '"valid"', "size": 42, "modified": None},
            ])

    def test_failed_download_does_not_open_destination_sink(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"permission denied"
                self.send_response(403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        destination = Path(__file__).resolve().parent / f"must-not-be-created-{threading.get_ident()}"
        try:
            status, _, _ = sync._request(f"http://127.0.0.1:{server.server_port}/dav/file.svsbundle", "GET", {}, "user", "password", sink=destination)
            self.assertEqual(status, 403)
            self.assertFalse(destination.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()



if __name__ == "__main__":
    unittest.main()

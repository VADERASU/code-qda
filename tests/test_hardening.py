"""Local mode: request hygiene, guards against proxies and other sites."""
import socket
import time
import unittest

from support import Client, appmod, fixture_project, local_server, temp_dir


class LocalHardening(unittest.TestCase):
    def setUp(self):
        self.project = fixture_project(temp_dir(self))
        self.server = local_server(self, self.project)
        self.client = Client(self.server)

    def raw(self, data, read_until_close=True, timeout=5):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=timeout)
        try:
            sock.sendall(data)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if not read_until_close:
                    break
            return b"".join(chunks)
        finally:
            sock.close()

    def test_serves_index_and_files(self):
        self.assertEqual(self.client.get("/").status, 200)
        resp = self.client.get("/api/files")
        self.assertEqual(resp.status, 200)
        self.assertEqual([f["name"] for f in resp.json()], ["alpha.md"])
        self.assertEqual(self.client.get("/auth/me").json(), {"mode": "local"})

    def test_rejects_foreign_host(self):
        # DNS rebinding: a page on evil.example resolving to 127.0.0.1.
        self.assertEqual(self.client.get("/api/files", headers={"Host": "evil.example"}).status, 421)
        self.assertEqual(self.client.get("/api/files", headers={"Host": "127.0.0.1:1"}).status, 200)

    def test_refuses_proxied_requests(self):
        for header in ("X-Forwarded-For", "Forwarded", "Via", "Tailscale-User-Login",
                       "Tailscale-Funnel-Request", "X-Real-IP"):
            with self.subTest(header=header):
                resp = self.client.get("/api/files", headers={header: "1.2.3.4"})
                self.assertEqual(resp.status, 403)
                self.assertEqual(resp.body, b"")

    def test_cross_site_writes_refused(self):
        body = {"codeTree": []}
        self.assertEqual(self.client.post("/api/save-codes", body,
                                          headers={"Origin": "https://evil.example"}).status, 403)
        self.assertEqual(self.client.post("/api/save-codes", body,
                                          headers={"Origin": "null"}).status, 403)
        # A form or no-cors fetch can only send "simple" content types.
        self.assertEqual(self.client.post("/api/save-codes", b"{}",
                                          headers={"Content-Type": "text/plain"}).status, 415)
        self.assertEqual(self.client.post("/api/save-codes", body).status, 200)
        # curl: no Origin at all is fine locally.
        self.assertEqual(self.client.post("/api/save-codes", body, origin=False).status, 200)

    def test_body_limits(self):
        # Refused from the headers alone: the server never reads the body.
        out = self.raw(b"POST /api/save-codes HTTP/1.1\r\nHost: localhost\r\n"
                       b"Content-Type: application/json\r\n"
                       b"Content-Length: %d\r\n\r\n{" % (appmod.MAX_BODY + 1))
        self.assertTrue(out.startswith(b"HTTP/1.1 413"), out[:40])
        self.assertIn(b"Connection: close", out)
        out = self.raw(b"POST /api/save-codes HTTP/1.1\r\nHost: localhost\r\n"
                       b"Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n"
                       b"2\r\n{}\r\n0\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 411"), out[:40])
        out = self.raw(b"POST /api/save-codes HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 411"), out[:40])

    def test_no_desync_after_early_rejection(self):
        smuggled = b"GET /api/files HTTP/1.1\r\nHost: localhost\r\n\r\n"
        request = (b"POST /api/nope HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                   b"Content-Length: %d\r\n\r\n" % len(smuggled)) + smuggled
        out = self.raw(request)
        self.assertEqual(out.count(b"HTTP/1.1 "), 1, out)
        self.assertTrue(out.startswith(b"HTTP/1.1 404"))
        self.assertIn(b"Connection: close", out)

    def test_slow_client_is_dropped(self):
        old = appmod.Handler.timeout
        appmod.Handler.timeout = 0.5
        self.addCleanup(setattr, appmod.Handler, "timeout", old)
        start = time.monotonic()
        out = self.raw(b"GET /api/files HTTP/1.1\r\nHost: loc", timeout=5)
        self.assertEqual(out, b"")
        self.assertLess(time.monotonic() - start, 4)

    def test_security_headers_everywhere(self):
        for path in ("/", "/api/files", "/does-not-exist", "/static/app.js"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertIn("default-src 'self'", resp.header("Content-Security-Policy"))
                self.assertEqual(resp.header("X-Content-Type-Options"), "nosniff")
                self.assertEqual(resp.header("Referrer-Policy"), "no-referrer")
                self.assertEqual(resp.header("Server"), "code-qda")
        self.assertEqual(self.client.get("/api/files").header("Cache-Control"), "no-store")
        # http.server's own error path (here: an over-long request line).
        out = self.raw(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 414"), out[:40])
        self.assertIn(b"Content-Security-Policy", out)

    def test_paths_cannot_escape(self):
        for path in ("/api/file?name=../app.py", "/api/file?name=..%2Fapp.py",
                     "/api/file?name=alpha.md%00", "/api/file?name=.hidden.md",
                     "/static/../app.py", "/static/%2e%2e/app.py", "/api/tx?name=../../etc/passwd",
                     "/api/audio?name=../alpha.md"):
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status, (400, 404))
        # Never read as a URL with a host part. Python 3.13 already collapses
        # the leading slashes; older versions get our own 400.
        self.assertIn(self.client.get("//evil.example/api/files").status, (400, 404))

    def test_audio_ranges(self):
        url = "/api/audio?name=alpha.16k.opus"
        full = self.client.get(url)
        self.assertEqual(full.status, 200)
        self.assertEqual(len(full.body), 200_000)
        self.assertEqual(full.header("Cache-Control"), "no-store")
        part = self.client.get(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(part.status, 206)
        self.assertEqual(part.body, full.body[10:20])
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=5-3"}).status, 416)
        self.assertEqual(self.client.get(url, headers={"Range": "bytes=999999-"}).status, 416)
        # HEAD sends no body, and the connection stays usable.
        conn = self.client.connection()
        head = self.client.request("HEAD", url, conn=conn)
        self.assertEqual((head.status, head.body), (200, b""))
        self.assertEqual(self.client.get("/api/files", conn=conn).status, 200)
        conn.close()

    def test_unsupported_method(self):
        out = self.raw(b"PUT /api/files HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 501"), out[:40])


if __name__ == "__main__":
    unittest.main()

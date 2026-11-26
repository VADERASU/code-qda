"""Browser end-to-end check over the Chrome DevTools protocol (stdlib only).

    make test-browser            # or: python3 tests/browser_e2e.py

Synthetic data in a temp dir, never md/. Drives local mode, the hosted
sign-in redirect, the coding view, the invite page, the admin page and sign
out in headless Chromium, and fails on any console error, uncaught exception
or Content-Security-Policy violation. Not part of `make test`: it needs a
Chromium binary. Set CHROME=/path/to/chrome, or it looks on PATH and in
Playwright's download cache; with none found, it skips.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402


def find_chrome():
    if os.environ.get("CHROME"):
        return Path(os.environ["CHROME"])
    cache = Path.home() / ".cache" / "ms-playwright"
    for pattern in ("chromium_headless_shell-*/*/chrome-headless-shell", "chromium_headless_shell-*/*/headless_shell"):
        found = sorted(cache.glob(pattern))
        if found:
            return found[-1]
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(name):
            return Path(shutil.which(name))
    return None


CHROME = find_chrome()


class WS:
    def __init__(self, url):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b" 101 " in buf.split(b"\r\n")[0], buf
        self.extra = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.extra) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.extra += chunk
        out, self.extra = self.extra[:n], self.extra[n:]
        return out

    def send(self, text):
        data = text.encode()
        n = len(data)
        header = bytes([0x81])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _frame(self):
        """Parse one complete frame from the buffer without consuming a
        partial one, so a read timeout can never desync the stream."""
        while True:
            buf = self.extra
            if len(buf) >= 2:
                n, off = buf[1] & 0x7F, 2
                if n == 126 and len(buf) >= 4:
                    n, off = struct.unpack(">H", buf[2:4])[0], 4
                elif n == 127 and len(buf) >= 10:
                    n, off = struct.unpack(">Q", buf[2:10])[0], 10
                if (buf[1] & 0x7F) < 126 or off > 2:
                    if len(buf) >= off + n:
                        self.extra = buf[off + n:]
                        return buf[0], buf[off:off + n]
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.extra += chunk

    def recv(self):
        parts = []
        while True:
            b1, payload = self._frame()
            parts.append(payload)
            if b1 & 0x80:
                return b"".join(parts).decode()


class Browser:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="qda-chrome-")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        args = [str(CHROME), f"--remote-debugging-port={port}"]
        if "headless" not in CHROME.name:
            # A full Chrome needs headless mode and its own throwaway profile.
            # (The headless shell already uses an in-memory one, and stalls
            # loading pages when handed a profile directory.)
            args += ["--headless=new", f"--user-data-dir={self.profile}"]
        self.proc = subprocess.Popen(
            args + ["--no-first-run", "--no-sandbox", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                pages = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list").read())
                page = next(p for p in pages if p["type"] == "page")
                break
            except Exception:
                time.sleep(0.1)
        self.ws = WS(page["webSocketDebuggerUrl"])
        self.next_id = 0
        self.problems = []
        for domain in ("Runtime", "Log", "Page", "Network"):
            self.call(f"{domain}.enable")

    def _event(self, msg):
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Runtime.exceptionThrown":
            self.problems.append(("exception", params["exceptionDetails"].get("exception", {}).get("description")
                                  or params["exceptionDetails"].get("text")))
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            self.problems.append(("console", [a.get("value", a.get("description")) for a in params["args"]]))
        elif method == "Log.entryAdded":
            entry = params["entry"]
            if entry.get("level") == "error" and "favicon" not in (entry.get("url") or ""):
                self.problems.append(("log", entry.get("source"), entry.get("text"), entry.get("url")))

    def call(self, method, **params):
        self.next_id += 1
        my_id = self.next_id
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            self._event(msg)

    def drain(self, seconds):
        end = time.time() + seconds
        self.ws.sock.settimeout(0.1)
        try:
            while time.time() < end:
                try:
                    self._event(json.loads(self.ws.recv()))
                except (socket.timeout, TimeoutError):
                    pass
        finally:
            self.ws.sock.settimeout(30)

    def go(self, url, settle=1.0):
        self.call("Page.navigate", url=url)
        self.drain(settle)

    def js(self, expr, settle=0.0):
        result = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        if settle:
            self.drain(settle)
        return result["result"].get("value")

    def wait_for(self, expr, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            self.drain(0.2)
        raise AssertionError(f"timed out waiting for: {expr}")

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.profile, ignore_errors=True)


@unittest.skipIf(CHROME is None, "no Chromium found (set CHROME=...)")
class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = Browser()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        self.browser.problems.clear()
        # (status, URL suffix) of refusals a test provokes on purpose; the
        # browser logs those as errors, and nothing else may appear.
        self.expected = set()

    def tearDown(self):
        def provoked(p):
            return (p[0] == "log" and p[1] == "network"
                    and any(f"status of {code}" in p[2] and p[3].endswith(url)
                            for code, url in self.expected))
        problems = [p for p in self.browser.problems if not provoked(p)]
        self.assertEqual(problems, [], "console errors / CSP violations")

    def test_local_mode(self):
        b = self.browser
        project = support.fixture_project(support.temp_dir(self))
        server = support.local_server(self, project)
        b.go(f"http://localhost:{server.port}/")
        b.wait_for("document.querySelectorAll('.file-item').length === 1")
        self.assertTrue(b.js("document.getElementById('session-bar').hidden"))
        b.js("document.querySelector('.file-item').click()")
        b.wait_for("document.querySelectorAll('#text-display .coded').length === 1")
        self.assertIn("simulator", b.js("document.querySelector('#text-display .coded').textContent"))
        self.assertTrue(b.js("location.search.includes('file=alpha.md')"))

    def test_hosted_flow(self):
        b = self.browser
        env = support.hosted_setup(self)
        port = env["server"].port
        origin = f"http://localhost:{port}"
        env["app"].public_origin = origin
        env["app"].allowed_hosts = frozenset({f"localhost:{port}"})

        # Signed out: the project URL leads to sign-in and back.
        b.go(f"{origin}/p/alpha/?file=one.md")
        b.wait_for("location.pathname === '/login'")
        self.assertIn("next=", b.js("location.search"))
        b.js(f"""(() => {{
            const f = document.getElementById('login-form');
            f.email.value = 'carol@example.edu';
            f.password.value = {json.dumps(env['passwords']['carol@example.edu'])};
            f.requestSubmit();
        }})()""")
        b.wait_for("location.pathname === '/p/alpha/'")
        b.wait_for("document.querySelectorAll('#text-display .coded').length === 1")  # ?file= kept
        b.wait_for("!document.getElementById('session-bar').hidden")
        self.assertEqual(b.js("document.querySelector('#session-project').value"), "alpha")
        self.assertEqual(b.js("document.querySelector('.session-who span').textContent"), "carol")
        cookies = b.call("Network.getCookies")["cookies"]
        session = [c for c in cookies if c["name"] == "__Host-qda_session"]
        self.assertEqual(len(session), 1)
        self.assertTrue(session[0]["httpOnly"])
        self.assertEqual(b.js("document.cookie"), "")

        # Coding saves round-trip through the project API (rev included).
        status = b.js(f"""fetch('/p/alpha/api/codes?name=one.md').then(r => r.json()).then(d =>
            fetch('/p/alpha/api/codes?name=one.md', {{method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{annotations: d.annotations, rev: d.rev}})}})).then(r => r.status)""")
        self.assertEqual(status, 200)

        # Another project is invisible.
        self.expected |= {(404, "/p/beta/api/files"), (401, "/p/alpha/api/files")}
        self.assertEqual(b.js("fetch('/p/beta/api/files').then(r => r.status)"), 404)

        # Sign out from the bar.
        b.js("document.getElementById('session-signout').click()")
        b.wait_for("location.pathname === '/login'")
        self.assertEqual(b.js("fetch('/p/alpha/api/files').then(r => r.status)"), 401)

    def test_two_people_editing(self):
        """The real save path: this tab's saves keep another person's codebook
        edits, and a conflicting save reloads with a notice instead of
        overwriting their codings."""
        b = self.browser
        env = support.hosted_setup(self)
        port = env["server"].port
        origin = f"http://localhost:{port}"
        env["app"].public_origin = origin
        env["app"].allowed_hosts = frozenset({f"localhost:{port}"})
        other = support.Client(env["server"], host=f"localhost:{port}", origin=origin)
        other.post("/auth/login", {"email": "alice@example.edu",
                                   "password": env["passwords"]["alice@example.edu"]})
        other.post("/p/alpha/api/add-code", {"id": "theirs-1", "name": "Theirs", "parentId": None,
                                             "order": 1})
        codes_file = env["alpha"].md_dir / "codes.json"

        def names():
            return {n["id"]: n["name"] for n in json.loads(codes_file.read_text())}

        def rename(old, new):
            b.js(f"""document.querySelector('button[aria-label="Rename {old}"]').click()""")
            b.wait_for("document.querySelector('.code-rename-input') !== null")
            b.js(f"""(() => {{ const i = document.querySelector('.code-rename-input');
                i.value = {json.dumps(new)};
                i.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); }})()""")

        b.go(f"{origin}/login?next=" + urllib.parse.quote("/p/alpha/?file=one.md"))
        b.js(f"""(() => {{ const f = document.getElementById('login-form');
            f.email.value = 'carol@example.edu';
            f.password.value = {json.dumps(env['passwords']['carol@example.edu'])};
            f.requestSubmit(); }})()""")
        b.wait_for("document.querySelectorAll('#text-display .coded').length === 1")

        # Alice renames her code after this tab loaded; this tab renames Tools.
        tree = other.get("/p/alpha/api/codes?name=one.md").json()["codeTree"]
        base = [dict(n) for n in tree]
        for n in tree:
            if n["id"] == "theirs-1":
                n["name"] = "Theirs renamed"
        other.post("/p/alpha/api/save-codes", {"codeTree": tree, "codeTreeBase": base})
        rename("Tools", "Tools v2")
        b.wait_for("document.getElementById('save-status').textContent === 'saved'")
        self.assertEqual(names(), {"code-root1": "Tools v2", "theirs-1": "Theirs renamed"})

        # Alice changes this transcript's codings; this tab's next save conflicts.
        rev = other.get("/p/alpha/api/codes?name=one.md").json()["rev"]
        self.assertEqual(other.post("/p/alpha/api/codes?name=one.md",
                                    {"annotations": [], "rev": rev}).status, 200)
        self.expected.add((409, "/p/alpha/api/codes?name=one.md"))
        rename("Tools v2", "Tools v3")
        b.wait_for("document.getElementById('save-status').textContent.includes('someone else')")
        b.wait_for("document.querySelectorAll('#text-display .coded').length === 0")  # hers now
        self.assertEqual(names()["code-root1"], "Tools v2")  # the refused save changed nothing
        rename("Tools v2", "Tools v3")  # redoing it works
        b.wait_for("document.getElementById('save-status').textContent === 'saved'")
        self.assertEqual(names()["code-root1"], "Tools v3")
        self.assertEqual(json.loads((env["alpha"].md_dir / "one.annot.json").read_text()), [])

    def test_invite_and_admin_pages(self):
        b = self.browser
        env = support.hosted_setup(self)
        port = env["server"].port
        origin = f"http://localhost:{port}"
        env["app"].public_origin = origin
        env["app"].allowed_hosts = frozenset({f"localhost:{port}"})
        token = env["auth"].create_invite("erin@example.edu", handle="erin")

        b.go(f"{origin}/invite#{token}")
        b.wait_for("!document.getElementById('invite-form').hidden")
        self.assertEqual(b.js("location.href"), f"{origin}/invite")  # token gone from the URL
        self.assertEqual(b.js("document.getElementById('email').value"), "erin@example.edu")
        b.js("""(() => { const f = document.getElementById('invite-form');
            f.password.value = 'a perfectly fine passphrase';
            f.confirm.value = 'a perfectly fine passphrase'; f.requestSubmit(); })()""")
        b.wait_for("location.pathname === '/'")
        b.wait_for("!document.getElementById('empty').hidden")  # no projects yet
        b.js("document.getElementById('signout').click()")
        b.wait_for("location.pathname === '/login'")

        b.js(f"""(() => {{ const f = document.getElementById('login-form');
            f.email.value = 'alice@example.edu';
            f.password.value = {json.dumps(env['passwords']['alice@example.edu'])};
            f.requestSubmit(); }})()""")
        b.wait_for("location.pathname === '/'")
        b.wait_for("document.querySelectorAll('#projects li').length === 1")
        b.go(f"{origin}/admin")
        b.wait_for("document.querySelectorAll('#users tr').length === 4")
        b.js("""(() => { document.getElementById('invite-email').value = 'frank@example.edu';
            document.querySelector('#invite-projects input[value=alpha]').checked = true;
            document.getElementById('invite-form').requestSubmit(); })()""")
        b.wait_for("!document.getElementById('link-out').hidden")
        link = b.js("document.getElementById('link-value').value")
        self.assertTrue(link.startswith(f"{origin}/invite#"))
        b.wait_for("document.querySelectorAll('#invites tr').length === 1")
        self.assertIn("frank@example.edu", b.js("document.getElementById('projects').textContent"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

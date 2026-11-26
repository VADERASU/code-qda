"""Test harness: synthetic projects in temp dirs and a server on a free port.

Never point a test at real data. md/ holds IRB-protected transcripts and
someone's uncommitted review work; the default state directory holds real
accounts. `guard()` refuses any path that resolves under either.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as appmod  # noqa: E402
import qda_auth  # noqa: E402
import txfmt  # noqa: E402
from qda_projects import Project, Registry  # noqa: E402
from qda_store import Store, default_state_dir  # noqa: E402

FORBIDDEN = [ROOT / "md", default_state_dir()]


def guard(*paths):
    for path in paths:
        resolved = Path(path).resolve()
        for bad in FORBIDDEN:
            try:
                resolved.relative_to(bad.resolve())
            except ValueError:
                continue
            raise RuntimeError(f"test touched real data: {resolved}")


def make_tx(title, lines, audio):
    segments = []
    t = 0.0
    for i, (speaker, text) in enumerate(lines, 1):
        segments.append({"id": f"s{i:04d}", "speaker": speaker, "start": t, "end": t + 2.0,
                         "text": text, "orig": text, "words": [], "status": "unreviewed"})
        t += 2.5
    return {"version": 1, "title": title, "audio": audio, "duration": t,
            "speakers": {"SPEAKER_00": "INTERVIEWER", "SPEAKER_01": "PARTICIPANT"},
            "segments": segments, "edits": []}


def fixture_project(base, slug="demo", stems=("alpha",), name=None):
    """A project directory with transcripts, one annotation each, and audio."""
    md = Path(base) / slug / "md"
    audio = Path(base) / slug / "audio"
    md.mkdir(parents=True)
    audio.mkdir(parents=True)
    guard(md, audio)
    codes = [{"id": "code-root1", "name": "Tools", "parentId": None, "order": 0,
              "color": "#1a73e8"}]
    (md / "codes.json").write_text(json.dumps(codes), encoding="utf-8")
    for stem in stems:
        tx = make_tx(stem, [("SPEAKER_00", "How do you use the robot?"),
                            ("SPEAKER_01", "Mostly with a simulator first."),
                            ("SPEAKER_00", "Tell me more about that.")],
                     f"{stem}.16k.opus")
        txfmt.save(md / f"{stem}.tx.json", tx)
        text, spans = txfmt.render_md(tx)
        (md / f"{stem}.md").write_text(text, encoding="utf-8")
        start, end = spans["s0002"]
        annotations = [{"id": "ann-1", "codeId": "code-root1", "text": text[start:end],
                        "start": start, "end": end}]
        (md / f"{stem}.annot.json").write_text(json.dumps(annotations), encoding="utf-8")
        (audio / f"{stem}.16k.opus").write_bytes(os.urandom(200_000))
    return Project(slug, name or slug.title(), md, audio)


class ServerThread:
    def __init__(self, app, unix_path=None):
        if unix_path:
            self.server = appmod.QDAUnixServer(unix_path, app)
        else:
            self.server = appmod.QDAServer(("127.0.0.1", 0), app)
        self.unix_path = unix_path
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


class Response:
    def __init__(self, raw):
        self.status = raw.status
        self.headers = raw.headers
        self.body = raw.read()

    def json(self):
        return json.loads(self.body)

    def header(self, name):
        return self.headers.get(name)


class Client:
    """Requests with an explicit Host and Origin, and a cookie jar of one."""

    def __init__(self, server, host=None, origin=None):
        self.server = server
        self.host = host or f"localhost:{server.port}"
        self.origin = origin if origin is not None else f"http://{self.host}"
        self.cookie = None

    def connection(self):
        if self.server.unix_path:
            return UnixHTTPConnection(self.server.unix_path)
        return http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)

    def headers(self, extra=None, origin=True):
        headers = {"Host": self.host}
        if origin and self.origin:
            headers["Origin"] = self.origin
        if self.cookie:
            headers["Cookie"] = f"__Host-qda_session={self.cookie}"
        headers.update(extra or {})
        return {k: v for k, v in headers.items() if v is not None}

    def request(self, method, path, body=None, headers=None, conn=None, origin=True):
        own = conn is None
        conn = conn or self.connection()
        data = None
        hdrs = self.headers(headers, origin=origin and method == "POST")
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif method == "POST":
            data = b""
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = Response(conn.getresponse())
        cookie = resp.header("Set-Cookie")
        if cookie and cookie.startswith("__Host-qda_session="):
            value = cookie.split(";", 1)[0].split("=", 1)[1]
            self.cookie = value or None
        if own:
            conn.close()
        return resp

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body={} if body is None else body, **kw)


def temp_dir(testcase):
    tmp = tempfile.TemporaryDirectory(prefix="qda-test-")
    testcase.addCleanup(tmp.cleanup)
    path = Path(tmp.name)
    guard(path)
    return path


def local_server(testcase, project):
    app = appmod.App(mode="local", local=project)
    server = ServerThread(app)
    testcase.addCleanup(server.close)
    return server


ORIGIN = "https://qda.test"


def hosted_setup(testcase, unix=False):
    """A hosted server with projects alpha (alice) and beta (bob); alice is admin."""
    base = temp_dir(testcase)
    state = base / "state"
    state.mkdir(mode=0o700)
    store = Store(state)
    store.update("config.json", {}, lambda c: c.update(
        public_origin=ORIGIN, version=1,
        data_roots=[str(base / "roots" / "data")], audio_roots=[str(base / "roots" / "audio")]))
    (base / "roots" / "data").mkdir(parents=True)
    (base / "roots" / "audio").mkdir(parents=True)
    auth = qda_auth.Auth(store, scrypt_n=2 ** 10)
    registry = Registry(store)
    alpha = fixture_project(base, "alpha", stems=("one",), name="Alpha study")
    beta = fixture_project(base, "beta", stems=("two",), name="Beta study")
    for project in (alpha, beta):
        registry.add(project.slug, project.name, project.md_dir, project.audio_dir, now=1.0)
    passwords = {}
    for email, admin in (("alice@example.edu", True), ("bob@example.edu", False),
                         ("carol@example.edu", False)):
        token = auth.create_invite(email, admin=admin, by="test")
        passwords[email] = f"correct horse {email}"
        auth.accept_invite(token, passwords[email])
    registry.set_member("alpha", "alice@example.edu", True)
    registry.set_member("beta", "bob@example.edu", True)
    registry.set_member("alpha", "carol@example.edu", True)
    app = appmod.App(mode="hosted", auth=auth, registry=registry, public_origin=ORIGIN,
                     allowed_hosts=frozenset({"qda.test"}), state_dir=state,
                     trust_forwarded=unix,
                     roots={"data": [str(base / "roots" / "data")],
                            "audio": [str(base / "roots" / "audio")]})
    server = ServerThread(app, unix_path=str(base / "http.sock") if unix else None)
    testcase.addCleanup(server.close)
    return {"base": base, "state": state, "store": store, "auth": auth, "registry": registry,
            "app": app, "server": server, "alpha": alpha, "beta": beta, "passwords": passwords}


def hosted_client(env):
    return Client(env["server"], host="qda.test", origin=ORIGIN)


def signed_in(env, email):
    client = hosted_client(env)
    resp = client.post("/auth/login", {"email": email, "password": env["passwords"][email]})
    assert resp.status == 200, (resp.status, resp.body)
    return client

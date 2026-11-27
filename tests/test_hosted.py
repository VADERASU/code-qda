"""Hosted mode over HTTP: sign-in, CSRF, project isolation, admin."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from qda_projects import ProjectError, Registry
from qda_store import Store
from support import (ORIGIN, ROOT, Client, appmod, hosted_client, hosted_setup, signed_in,
                     temp_dir, txfmt)

PROJECT_PATHS = ["/p/alpha/", "/p/alpha/api/files", "/p/alpha/api/file?name=one.md",
                 "/p/alpha/api/codes?name=one.md", "/p/alpha/api/codes-overview",
                 "/p/alpha/api/instances?codeId=code-root1", "/p/alpha/api/tx?name=one.md",
                 "/p/alpha/api/audio?name=one.16k.opus", "/", "/admin", "/admin/api/state"]


def comparable(resp):
    """Status, headers and body, minus what legitimately varies per request."""
    headers = sorted((k.lower(), v) for k, v in resp.headers.items() if k.lower() != "date")
    return resp.status, headers, resp.body


class SignedOut(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.client = hosted_client(self.env)

    def test_nothing_without_a_session(self):
        for path in PROJECT_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertIn(resp.status, (303, 401))
                if resp.status == 303:
                    self.assertTrue(resp.header("Location").startswith("/login?next="))
                self.assertNotIn(b"simulator", resp.body)
        for path in ("/p/alpha/api/codes?name=one.md", "/p/alpha/api/tx-edit?name=one.md",
                     "/admin/api/invite"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, {"annotations": []}).status, 401)
        # Same answer whether or not a project exists.
        self.assertEqual(comparable(self.client.get("/p/alpha/api/files")),
                         comparable(self.client.get("/p/nope/api/files")))

    def test_public_pages(self):
        for path in ("/login", "/invite", "/static/auth.js", "/static/app.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status, 200)
        self.assertEqual(self.client.get("/api/files").status, 401)
        self.assertEqual(self.client.get("/auth/me").status, 401)

    def test_host_must_match(self):
        self.assertEqual(self.client.get("/login", headers={"Host": "evil.example"}).status, 421)

    def test_headers(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.header("Strict-Transport-Security"), "max-age=31536000")
        self.assertIn("frame-ancestors 'none'", resp.header("Content-Security-Policy"))
        self.assertEqual(resp.header("Cache-Control"), "no-store")


class SignIn(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.client = hosted_client(self.env)

    def login(self, email, password, client=None, **kw):
        return (client or self.client).post("/auth/login", {"email": email, "password": password}, **kw)

    def test_cookie_attributes(self):
        resp = self.login("bob@example.edu", self.env["passwords"]["bob@example.edu"])
        self.assertEqual(resp.status, 200)
        cookie = resp.header("Set-Cookie")
        name, *attrs = [part.strip() for part in cookie.split(";")]
        self.assertRegex(name, r"^__Host-qda_session=[A-Za-z0-9_-]{43}$")
        self.assertEqual(set(attrs), {"Path=/", "Secure", "HttpOnly", "SameSite=Lax",
                                      "Max-Age=2592000"})
        self.assertNotIn("Domain", cookie)
        me = self.client.get("/auth/me").json()
        self.assertEqual(me["projects"], [{"slug": "beta", "name": "Beta study", "owned": False},
                                          {"slug": "sandbox", "name": "Sandbox", "sandbox": True}])

    def test_wrong_password_and_throttle(self):
        for _ in range(5):
            resp = self.login("bob@example.edu", "wrong password!!")
            self.assertEqual(resp.status, 401)
            self.assertNotIn("Set-Cookie", resp.headers)
        # Even the right password is refused once throttled.
        self.assertEqual(self.login("bob@example.edu", self.env["passwords"]["bob@example.edu"]).status, 429)
        for _ in range(5):
            self.login("ghost@example.edu", "wrong password!!")
        self.assertEqual(self.login("ghost@example.edu", "wrong password!!").status, 429)
        # Unknown and known accounts get the same refusal.
        a = self.login("alice@example.edu", "wrong password!!")
        b = self.login("nobody@example.edu", "wrong password!!")
        self.assertEqual((a.status, a.body), (b.status, b.body))

    def test_csrf(self):
        good = self.env["passwords"]["bob@example.edu"]
        self.assertEqual(self.login("bob@example.edu", good,
                                    headers={"Origin": "https://evil.example"}).status, 403)
        self.assertEqual(self.login("bob@example.edu", good, headers={"Origin": "null"}).status, 403)
        self.assertEqual(self.login("bob@example.edu", good, origin=False).status, 403)
        self.assertEqual(self.login("bob@example.edu", good, origin=False,
                                    headers={"Sec-Fetch-Site": "same-origin"}).status, 200)
        resp = self.client.post("/p/beta/api/save-codes", b'{"codeTree": []}',
                                headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        resp = self.client.post("/p/beta/api/save-codes", {"codeTree": []},
                                headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)

    def test_safe_next(self):
        good = self.env["passwords"]["bob@example.edu"]
        for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "javascript:x"):
            with self.subTest(next=bad):
                resp = self.client.post("/auth/login", {"email": "bob@example.edu",
                                                        "password": good, "next": bad})
                self.assertEqual(resp.json()["next"], "/")
        resp = self.client.get("/login?next=//evil.example")
        self.assertEqual((resp.status, resp.header("Location")), (303, "/"))

    def test_logout_ends_session(self):
        client = signed_in(self.env, "bob@example.edu")
        token = client.cookie
        self.assertEqual(client.post("/auth/logout").status, 200)
        client.cookie = token
        self.assertEqual(client.get("/p/beta/api/files").status, 401)

    def test_disabled_account_loses_access(self):
        client = signed_in(self.env, "bob@example.edu")
        self.env["auth"].set_disabled("bob@example.edu", True)
        self.assertEqual(client.get("/p/beta/api/files").status, 401)

    def test_busy_hashing(self):
        with mock.patch.object(self.env["auth"], "_hashing", side_effect=__import__("qda_auth").Busy()):
            resp = self.login("bob@example.edu", "anything at all")
        self.assertEqual(resp.status, 503)


class Isolation(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)

    def test_members_only(self):
        bob = signed_in(self.env, "bob@example.edu")
        self.assertEqual(bob.get("/p/beta/api/files").json()[0]["name"], "two.md")
        # Not a member of alpha: indistinguishable from a project that doesn't exist.
        for suffix in ("", "/", "/api/files", "/api/audio?name=one.16k.opus",
                       "/api/file?name=one.md"):
            with self.subTest(suffix=suffix):
                self.assertEqual(comparable(bob.get("/p/alpha" + suffix)),
                                 comparable(bob.get("/p/nope" + suffix)))
                self.assertEqual(bob.get("/p/alpha" + suffix).status, 404)
        self.assertEqual(bob.post("/p/alpha/api/save-codes", {"codeTree": []}).status, 404)
        # The un-prefixed local API does not exist when hosted.
        self.assertEqual(bob.get("/api/files").status, 404)
        resp = bob.get("/p/beta")
        self.assertEqual((resp.status, resp.header("Location")), (308, "/p/beta/"))
        resp = bob.get("/")  # always the project list: their projects and their sandbox
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Your projects", resp.body)

    def test_membership_changes_apply_immediately(self):
        bob = signed_in(self.env, "bob@example.edu")
        self.env["registry"].set_member("alpha", "bob@example.edu", True)
        self.assertEqual(bob.get("/p/alpha/api/files").status, 200)
        self.env["registry"].set_member("alpha", "bob@example.edu", False)
        self.assertEqual(bob.get("/p/alpha/api/files").status, 404)

    def test_identity_is_reset_between_requests_on_one_connection(self):
        carol = signed_in(self.env, "carol@example.edu")
        conn = carol.connection()
        self.assertEqual(carol.get("/p/alpha/api/files", conn=conn).status, 200)
        stranger = Client(self.env["server"], host="qda.test", origin=ORIGIN)
        self.assertEqual(stranger.get("/p/alpha/api/files", conn=conn).status, 401)
        self.assertEqual(carol.get("/p/alpha/api/files", conn=conn).status, 200)
        conn.close()

    def test_edit_author_comes_from_session(self):
        carol = signed_in(self.env, "carol@example.edu")
        resp = carol.post("/p/alpha/api/tx-edit?name=one.md",
                          {"seg": "s0002", "text": "Mostly in a simulator first.", "by": "mallory"})
        self.assertEqual(resp.status, 200, resp.body)
        tx = txfmt.load(self.env["alpha"].md_dir / "one.tx.json")
        self.assertEqual(tx["edits"][-1]["by"], "carol")
        log = (self.env["state"] / "audit.log").read_text()
        self.assertIn('"event": "write"', log)
        self.assertNotIn(self.env["passwords"]["carol@example.edu"], log)

    def test_audio_streams_for_members(self):
        carol = signed_in(self.env, "carol@example.edu")
        resp = carol.get("/p/alpha/api/audio?name=one.16k.opus", headers={"Range": "bytes=0-99"})
        self.assertEqual((resp.status, len(resp.body)), (206, 100))
        self.assertEqual(resp.header("Cache-Control"), "no-store")


class Admin(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.admin = signed_in(self.env, "alice@example.edu")

    def test_editors_cannot_see_admin(self):
        bob = signed_in(self.env, "bob@example.edu")
        for path in ("/admin", "/admin/api/state"):
            self.assertEqual(bob.get(path).status, 404)
        self.assertEqual(bob.post("/admin/api/invite", {"email": "x@example.edu"}).status, 404)

    def test_invite_cycle(self):
        resp = self.admin.post("/admin/api/invite", {"email": "dan@example.edu",
                                                     "projects": ["alpha"], "handle": "dan"})
        self.assertEqual(resp.status, 200, resp.body)
        link = resp.json()["link"]
        self.assertTrue(link.startswith(ORIGIN + "/invite#"))
        token = link.split("#", 1)[1]
        self.assertNotIn(token, json.dumps(self.admin.get("/admin/api/state").json()))
        dan = hosted_client(self.env)
        info = dan.post("/auth/invite/info", {"token": token}).json()
        self.assertEqual(info, {"email": "dan@example.edu", "kind": "invite", "handle": "dan"})
        self.assertEqual(dan.post("/auth/invite/accept", {"token": token, "password": "short"}).status, 400)
        resp = dan.post("/auth/invite/accept", {"token": token, "password": "a fine passphrase"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(dan.get("/p/alpha/api/files").status, 200)
        self.assertEqual(dan.get("/p/beta/api/files").status, 404)
        again = hosted_client(self.env).post("/auth/invite/accept",
                                             {"token": token, "password": "a fine passphrase"})
        self.assertEqual(again.status, 400)

    def test_accepting_an_invite_signs_out_the_previous_user(self):
        bob = signed_in(self.env, "bob@example.edu")
        token = self.env["auth"].create_invite("erin@example.edu", by="test")
        bobs_session = bob.cookie
        resp = bob.post("/auth/invite/accept", {"token": token, "password": "a fine passphrase"})
        self.assertEqual(resp.status, 200)
        self.assertNotEqual(bob.cookie, bobs_session)
        stale = hosted_client(self.env)
        stale.cookie = bobs_session
        self.assertEqual(stale.get("/p/beta/api/files").status, 401)

    def test_reset_link(self):
        bob = signed_in(self.env, "bob@example.edu")
        link = self.admin.post("/admin/api/reset", {"email": "bob@example.edu"}).json()["link"]
        token = link.split("#", 1)[1]
        new = hosted_client(self.env)
        self.assertEqual(new.post("/auth/invite/accept",
                                  {"token": token, "password": "brand new passphrase"}).status, 200)
        self.assertEqual(bob.get("/p/beta/api/files").status, 401)  # old session ended
        old = hosted_client(self.env).post("/auth/login", {"email": "bob@example.edu",
                                                           "password": self.env["passwords"]["bob@example.edu"]})
        self.assertEqual(old.status, 401)

    def test_disable_and_self_protection(self):
        bob = signed_in(self.env, "bob@example.edu")
        self.assertEqual(self.admin.post("/admin/api/user", {"email": "bob@example.edu",
                                                             "disabled": True}).status, 200)
        self.assertEqual(bob.get("/p/beta/api/files").status, 401)
        self.assertEqual(self.admin.post("/admin/api/user", {"email": "alice@example.edu",
                                                             "disabled": True}).status, 400)

    def test_registering_existing_project_is_confined(self):
        base = self.env["base"]
        (base / "roots" / "data" / "gamma").mkdir()
        (base / "roots" / "audio" / "gamma").mkdir()
        outside = base / "elsewhere"
        outside.mkdir()
        cases = [
            ({"dataDir": str(outside), "audioDir": str(base / "roots/audio/gamma")}, 400),
            ({"dataDir": str(ROOT), "audioDir": str(base / "roots/audio/gamma")}, 400),
            ({"dataDir": "relative/path", "audioDir": str(base / "roots/audio/gamma")}, 400),
            ({"dataDir": str(self.env["alpha"].md_dir), "audioDir": str(base / "roots/audio/gamma")}, 400),
            ({"dataDir": str(base / "roots/data/gamma"), "audioDir": str(base / "roots/audio/gamma")}, 200),
        ]
        for extra, status in cases:
            with self.subTest(extra=extra):
                resp = self.admin.post("/admin/api/project", dict({"slug": "gamma", "name": "Gamma"}, **extra))
                self.assertEqual(resp.status, status, resp.body)
        self.assertEqual(self.admin.post("/admin/api/project", {"slug": "Bad Slug!", "name": "x",
                                                                "dataDir": "/", "audioDir": "/"}).status, 400)

    def test_creates_project_with_private_storage_without_registration_roots(self):
        self.env["app"].roots = {"data": [], "audio": []}
        self.env["store"].update("projects.json", {}, lambda doc: doc["projects"]["alpha"].pop("storage"))
        response = self.admin.post("/admin/api/project/create", {"slug": "md", "name": "New study"})
        self.assertEqual(response.status, 200, response.body)
        projects = {project["slug"]: project for project in response.json()["projects"]}
        self.assertEqual(projects["alpha"]["storage"], "registered")  # pre-existing record
        project = projects["md"]
        self.assertEqual(project["storage"], "managed")
        self.assertEqual(project["members"], [])
        data, audio = Path(project["dataDir"]), Path(project["audioDir"])
        root = self.env["state"].resolve() / "projects"
        self.assertEqual(data.parent.parent, root)
        self.assertEqual(audio.parent, data.parent)
        self.assertNotEqual(data, audio)
        for path in (root, data.parent, data, audio):
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(list(data.iterdir()), [])
        self.assertEqual(list(audio.iterdir()), [])
        self.assertEqual(appmod.build_hosted_app(self.env["state"]).registry.records()["md"]["data_dir"], str(data))
        self.assertEqual(self.admin.get("/p/alpha/api/files").status, 200)
        self.assertEqual(self.admin.get("/p/md/api/files").status, 404)
        self.assertEqual(self.admin.post("/admin/api/members", {"slug": "md", "email": "alice@example.edu",
                                                               "member": True}).status, 200)
        self.assertEqual(self.admin.get("/p/md/api/files").json(), [])
        self.assertEqual(self.admin.post("/admin/api/project/create", {"slug": "md"}).status, 400)
        self.assertEqual(list(root.iterdir()), [data.parent])


class ManagedStorage(unittest.TestCase):
    def test_failed_registry_write_cleans_up_and_allows_retry(self):
        state = temp_dir(self) / "state"
        state.mkdir(mode=0o700)
        store = Store(state)
        registry = Registry(store)
        with mock.patch.object(store, "write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ProjectError, "disk full"):
                registry.create("study", "Study", now=1.0)
        self.assertEqual(registry.records(), {})
        self.assertEqual(list((state / "projects").iterdir()), [])
        project = registry.create("study", "Study", now=1.0)
        with self.assertRaisesRegex(ProjectError, "inside"):
            registry.add("alias", "Alias", project["data_dir"], project["audio_dir"], now=2.0)


class UnixSocket(unittest.TestCase):
    def test_serves_and_trusts_forwarded_for(self):
        env = hosted_setup(self, unix=True)
        sock = Path(env["server"].unix_path)
        self.assertEqual(os.stat(sock).st_mode & 0o077, 0)
        client = hosted_client(env)
        self.assertEqual(client.get("/login").status, 200)
        # Per-address throttling uses the address tailscaled appended (last).
        for _ in range(20):
            client.post("/auth/login", {"email": f"x{_}@example.edu", "password": "wrong password!!"},
                        headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
        resp = client.post("/auth/login", {"email": "new@example.edu", "password": "wrong password!!"},
                           headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
        self.assertEqual(resp.status, 429)
        resp = client.post("/auth/login", {"email": "new@example.edu", "password": "wrong password!!"},
                           headers={"X-Forwarded-For": "203.0.113.10"})
        self.assertEqual(resp.status, 401)


class Startup(unittest.TestCase):
    def test_refuses_unsafe_state(self):
        env = hosted_setup(self)
        state = env["state"]
        os.chmod(state, 0o755)
        with self.assertRaises(SystemExit):
            appmod.build_hosted_app(state)
        os.chmod(state, 0o700)
        app = appmod.build_hosted_app(state)
        self.assertEqual(app.public_origin, ORIGIN)
        self.assertEqual(app.allowed_hosts, frozenset({"qda.test", "qda.test:443"}))
        env["store"].update("config.json", {}, lambda c: c.update(public_origin="http://qda.test"))
        with self.assertRaises(SystemExit):
            appmod.build_hosted_app(state)


if __name__ == "__main__":
    unittest.main()

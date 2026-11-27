"""Everyone's private sandbox, and projects people create and share."""
import json
import unittest
from pathlib import Path
from unittest import mock

import qda_projects
from qda_projects import STARTER_DIR, ProjectError, _place_codings
from support import ORIGIN, hosted_client, hosted_setup, signed_in, txfmt

ALICE, BOB, CAROL = "alice@example.edu", "bob@example.edu", "carol@example.edu"


def comparable(resp):
    headers = sorted((k.lower(), v) for k, v in resp.headers.items() if k.lower() != "date")
    return resp.status, headers, resp.body


class Starter(unittest.TestCase):
    def test_starter_codings_match_the_rendered_transcript(self):
        tx = txfmt.load(STARTER_DIR / "practice-interview.tx.json")
        md, _spans = txfmt.render_md(tx)
        codings = json.loads((STARTER_DIR / "practice-interview.annot.json").read_text())
        codes = {c["id"] for c in json.loads((STARTER_DIR / "codes.json").read_text())}
        self.assertGreaterEqual(len(codings), 5)
        for ann in codings:
            self.assertEqual(md[ann["start"]:ann["end"]], ann["text"])
            self.assertIn(ann["codeId"], codes)

    def test_codings_are_found_again_if_the_render_moves(self):
        md = "# T\n\nhello there, world\n"
        placed = _place_codings([
            {"id": "a", "codeId": "c", "text": "there", "start": 0, "end": 5},   # stale offsets
            {"id": "b", "codeId": "c", "text": "gone", "start": 3, "end": 7},    # text not present
            {"id": "c", "codeId": "c", "text": "world", "start": 19, "end": 24},  # already right
        ], md)
        self.assertEqual([(a["id"], md[a["start"]:a["end"]]) for a in placed],
                         [("a", "there"), ("c", "world")])


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.starter = {p.name: p.read_bytes() for p in STARTER_DIR.iterdir()}

    def test_everyone_has_a_private_sandbox_with_the_starter(self):
        alice, bob = signed_in(self.env, ALICE), signed_in(self.env, BOB)
        for client in (alice, bob):
            files = client.get("/p/sandbox/api/files").json()
            self.assertEqual([f["name"] for f in files], ["practice-interview.md"])
            self.assertFalse(files[0]["audioAvailable"])
            codes = client.get("/p/sandbox/api/codes?name=practice-interview.md").json()
            self.assertGreaterEqual(len(codes["codeTree"]), 5)
            self.assertGreaterEqual(len(codes["annotations"]), 5)
            text = client.get("/p/sandbox/api/file?name=practice-interview.md").json()["content"]
            for ann in codes["annotations"]:
                self.assertEqual(text[ann["start"]:ann["end"]], ann["text"])

        root = self.env["state"] / "sandboxes"
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["alice", "bob"])
        for path in [root, *root.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)
        self.assertEqual(list((root / "alice" / "audio").iterdir()), [])  # no audio

        # Alice's changes stay hers.
        self.assertEqual(alice.post("/p/sandbox/api/add-code", {
            "id": "alices", "name": "Alice's code", "parentId": None, "order": 9,
            "color": "#e8710a"}).status, 200)
        resp = alice.post("/p/sandbox/api/tx-edit?name=practice-interview.md",
                          {"seg": "s0002", "text": "We fly over four plots."})
        self.assertEqual(resp.status, 200, resp.body)
        bobs = bob.get("/p/sandbox/api/codes?name=practice-interview.md").json()
        self.assertNotIn("alices", [c["id"] for c in bobs["codeTree"]])
        self.assertNotIn("four plots", bob.get("/p/sandbox/api/file?name=practice-interview.md").json()["content"])
        self.assertIn("four plots", alice.get("/p/sandbox/api/file?name=practice-interview.md").json()["content"])
        self.assertEqual({p.name: p.read_bytes() for p in STARTER_DIR.iterdir()}, self.starter)

    def test_listed_last_for_everyone_and_reserved(self):
        bob = signed_in(self.env, BOB)
        projects = bob.get("/auth/me").json()["projects"]
        self.assertEqual(projects[-1], {"slug": "sandbox", "name": "Sandbox", "sandbox": True})
        with self.assertRaisesRegex(ProjectError, "reserved"):
            self.env["registry"].create("sandbox", "Mine", now=1.0)
        admin = signed_in(self.env, ALICE)
        self.assertEqual(admin.post("/admin/api/project/create",
                                    {"slug": "sandbox", "name": "x"}).status, 400)

    def test_failed_build_leaves_no_half_sandbox(self):
        bob = signed_in(self.env, BOB)
        with mock.patch.object(qda_projects.os, "rename", side_effect=OSError("disk")):
            self.assertEqual(bob.get("/p/sandbox/api/files").status, 500)
        self.assertEqual(list((self.env["state"] / "sandboxes").iterdir()), [])
        self.assertEqual(len(bob.get("/p/sandbox/api/files").json()), 1)


class OwnProjects(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.bob = signed_in(self.env, BOB)

    def create(self, client, name):
        return client.post("/projects/api/create", {"name": name})

    def test_anyone_can_create_and_owns_it(self):
        resp = self.create(self.bob, "Field notes!")
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.json()["created"], "field-notes")
        rec = self.env["registry"].records()["field-notes"]
        self.assertEqual((rec["owner"], rec["members"], rec["storage"]),
                         (BOB, [BOB], "managed"))
        self.assertEqual(self.bob.get("/p/field-notes/api/files").json(), [])
        mine = {p["slug"]: p for p in self.bob.get("/auth/me").json()["projects"]}
        self.assertEqual(mine["field-notes"], {"slug": "field-notes", "name": "Field notes!",
                                               "owned": True, "members": [BOB]})
        self.assertEqual(self.create(self.bob, "Field notes").json()["created"], "field-notes-2")
        self.assertEqual(self.create(self.bob, "Sandbox").json()["created"], "sandbox-2")
        self.assertEqual(self.create(self.bob, "   ").status, 400)
        carol = signed_in(self.env, CAROL)
        self.assertEqual(comparable(carol.get("/p/field-notes/api/files")),
                         comparable(carol.get("/p/nope/api/files")))
        log = (self.env["state"] / "audit.log").read_text()
        self.assertIn('"by": "bob@example.edu", "data_dir"', log)
        admin = signed_in(self.env, ALICE)
        state = {p["slug"]: p for p in admin.get("/admin/api/state").json()["projects"]}
        self.assertEqual(state["field-notes"]["owner"], BOB)

    def test_owner_adds_only_existing_accounts(self):
        self.create(self.bob, "Shared")
        add = lambda client, email, member=True: client.post(
            "/projects/api/members", {"slug": "shared", "email": email, "member": member})
        resp = add(self.bob, "nobody@example.edu")
        self.assertEqual(resp.status, 400)
        self.assertIn("ask an admin", resp.json()["error"])
        self.assertEqual(add(self.bob, CAROL).status, 200)
        carol = signed_in(self.env, CAROL)
        self.assertEqual(carol.get("/p/shared/api/files").status, 200)
        theirs = {p["slug"]: p for p in carol.get("/auth/me").json()["projects"]}["shared"]
        self.assertEqual(theirs, {"slug": "shared", "name": "Shared", "owned": False})
        # Members who don't own it, and outsiders, can't tell there's anything to manage.
        self.assertEqual(add(carol, ALICE).status, 404)
        self.assertEqual(add(carol, BOB, member=False).status, 404)
        self.assertEqual(add(self.bob, CAROL).status, 200)  # idempotent
        self.assertEqual(self.bob.post("/projects/api/members", {
            "slug": "nope", "email": CAROL, "member": True}).status, 404)
        self.assertEqual(add(self.bob, BOB, member=False).status, 400)  # the owner stays
        self.assertEqual(add(self.bob, CAROL, member=False).status, 200)
        self.assertEqual(carol.get("/p/shared/api/files").status, 404)
        # An admin can manage anyone's project.
        admin = signed_in(self.env, ALICE)
        self.assertEqual(add(admin, CAROL).status, 200)
        # The owner removed by an admin can no longer manage it.
        self.env["registry"].set_member("shared", BOB, False)
        self.assertEqual(add(self.bob, ALICE).status, 404)

    def test_limits_and_csrf(self):
        with mock.patch.object(qda_projects, "MAX_OWNED_PROJECTS", 2):
            for name in ("one", "two"):
                self.assertEqual(self.create(self.bob, name).status, 200)
            resp = self.create(self.bob, "three")
            self.assertEqual(resp.status, 400)
            self.assertIn("at most 2", resp.json()["error"])
        evil = hosted_client(self.env)
        evil.cookie, evil.origin = self.bob.cookie, "https://evil.example"
        self.assertEqual(evil.post("/projects/api/create", {"name": "x"}).status, 403)
        signed_out = hosted_client(self.env)
        self.assertEqual(signed_out.post("/projects/api/create", {"name": "x"}).status, 401)
        self.assertEqual(signed_out.origin, ORIGIN)


if __name__ == "__main__":
    unittest.main()

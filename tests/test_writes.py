"""Data safety: atomic writes, input validation, concurrent and stale saves."""
import json
import os
import threading
import unittest
from unittest import mock

from support import Client, fixture_project, local_server, temp_dir, txfmt


class AtomicWrite(unittest.TestCase):
    def test_keeps_mode_and_replaces_whole(self):
        path = temp_dir(self) / "f.json"
        path.write_text("old")
        os.chmod(path, 0o640)
        txfmt.write_text_atomic(path, "new")
        self.assertEqual(path.read_text(), "new")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)

    def test_failure_leaves_original_and_no_temp_file(self):
        base = temp_dir(self)
        path = base / "f.json"
        path.write_text("original")
        with mock.patch("txfmt.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                txfmt.write_text_atomic(path, "replacement")
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["f.json"])

    def test_writes_through_symlinks(self):
        base = temp_dir(self)
        real = base / "real.json"
        real.write_text("a")
        link = base / "link.json"
        link.symlink_to(real)
        txfmt.write_text_atomic(link, "b")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "b")


class Saves(unittest.TestCase):
    def setUp(self):
        self.project = fixture_project(temp_dir(self))
        self.server = local_server(self, self.project)
        self.client = Client(self.server)
        self.md = self.project.md_dir

    def codes(self):
        return json.loads((self.md / "codes.json").read_text())

    def load(self, name="alpha.md"):
        resp = self.client.get(f"/api/codes?name={name}")
        self.assertEqual(resp.status, 200)
        return resp.json()

    def test_hostile_values_never_stored(self):
        data = self.load()
        tree = data["codeTree"] + [
            {"id": "ok-1", "name": "Fine", "parentId": None, "order": 1,
             "color": "red;background:url(https://evil.example/x)"},
            {"id": '"><img src=x onerror=alert(1)>', "name": "Bad id", "parentId": None},
        ]
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"codeTree": tree, "annotations": data["annotations"],
                                 "rev": data["rev"]})
        self.assertEqual(resp.status, 200, resp.body)
        stored = {n["id"]: n for n in self.codes()}
        self.assertIn("ok-1", stored)
        self.assertNotIn("color", stored["ok-1"])
        self.assertFalse(any("<" in n["id"] for n in stored.values()))

        bad = dict(data["annotations"][0], id='x" onmouseover="alert(1)')
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"annotations": [bad], "rev": resp.json()["rev"]})
        self.assertEqual(resp.status, 400)

    def test_cannot_create_files_for_missing_transcripts(self):
        resp = self.client.post("/api/codes?name=nothing.md", {"annotations": []})
        self.assertEqual(resp.status, 404)
        self.assertFalse((self.md / "nothing.annot.json").exists())

    def test_stale_tab_does_not_erase_new_codes(self):
        stale = self.load()
        # Someone else adds a code after this tab loaded.
        resp = self.client.post("/api/add-code", {"id": "theirs-1", "name": "Theirs",
                                                  "parentId": None, "order": 5})
        self.assertEqual(resp.status, 200)
        # The stale tab saves its whole (older) tree.
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"codeTree": stale["codeTree"],
                                 "annotations": stale["annotations"], "rev": stale["rev"]})
        self.assertEqual(resp.status, 200)
        self.assertIn("theirs-1", {n["id"] for n in self.codes()})
        resp = self.client.post("/api/save-codes", {"codeTree": stale["codeTree"]})
        self.assertIn("theirs-1", {n["id"] for n in resp.json()["codeTree"]})

    def test_stale_tab_does_not_undo_others_edits(self):
        """A tab sends its whole tree plus the tree it last synced (the base).
        Codes it didn't touch keep whatever others have done to them since."""
        self.client.post("/api/add-code", {"id": "spare-1", "name": "Spare", "parentId": None,
                                           "order": 3})
        stale = self.load()
        base = stale["codeTree"]
        # Another person renames one code and prunes the unused one.
        other = Client(self.server)
        tree = other.get("/api/codes?name=alpha.md").json()["codeTree"]
        for node in tree:
            if node["id"] == "code-root1":
                node["name"] = "Tools (renamed)"
        other.post("/api/save-codes", {"codeTree": tree, "codeTreeBase": base})
        other.post("/api/prune-codes")
        # The stale tab makes its own edit: a new code, used by a new annotation.
        mine = [dict(n) for n in base] + [{"id": "mine-1", "name": "Mine", "parentId": None,
                                           "order": 9}]
        annotations = stale["annotations"] + [dict(stale["annotations"][0], id="ann-2",
                                                    codeId="mine-1")]
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"codeTree": mine, "codeTreeBase": base,
                                 "annotations": annotations, "rev": stale["rev"]})
        self.assertEqual(resp.status, 200, resp.body)
        codes = {n["id"]: n["name"] for n in self.codes()}
        self.assertEqual(codes, {"code-root1": "Tools (renamed)", "mine-1": "Mine"})

    def test_own_edits_win_and_used_codes_survive(self):
        stale = self.load()
        base = stale["codeTree"]
        # Someone deletes a code (it was unused at the time)...
        self.client.post("/api/add-code", {"id": "gone-1", "name": "Gone", "parentId": None})
        fresh = self.load()
        self.client.post("/api/delete-code?id=gone-1")
        # ...while a tab that still has it codes a passage with it and renames Tools.
        mine = [dict(n) for n in fresh["codeTree"]]
        for node in mine:
            if node["id"] == "code-root1":
                node["name"] = "Instruments"
        annotations = fresh["annotations"] + [dict(fresh["annotations"][0], id="ann-3",
                                                   codeId="gone-1")]
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"codeTree": mine, "codeTreeBase": fresh["codeTree"],
                                 "annotations": annotations, "rev": fresh["rev"]})
        self.assertEqual(resp.status, 200, resp.body)
        codes = {n["id"]: n["name"] for n in self.codes()}
        self.assertEqual(codes["code-root1"], "Instruments")
        self.assertIn("gone-1", codes, "a code an annotation uses is never dropped")
        self.assertIsNotNone(base)

    def test_stale_annotation_save_is_refused(self):
        first = self.load()
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"annotations": first["annotations"][:0], "rev": first["rev"]})
        self.assertEqual(resp.status, 200)
        new_rev = resp.json()["rev"]
        self.assertEqual(new_rev, self.load()["rev"])
        # A second tab still holding the first version.
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"annotations": first["annotations"], "rev": first["rev"]})
        self.assertEqual(resp.status, 409)
        self.assertEqual(resp.json()["rev"], new_rev)
        self.assertEqual(json.loads((self.md / "alpha.annot.json").read_text()), [])

    def test_transcript_edit_invalidates_open_coding_view(self):
        before = self.load()
        resp = self.client.post("/api/tx-edit?name=alpha.md",
                                {"seg": "s0001", "text": "How do you use the robot arm?",
                                 "by": "rolando"})
        self.assertEqual(resp.status, 200, resp.body)
        tx = txfmt.load(self.md / "alpha.tx.json")
        self.assertEqual(tx["edits"][-1]["by"], "rolando")  # local mode: client says
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"annotations": before["annotations"], "rev": before["rev"]})
        self.assertEqual(resp.status, 409)

    def test_concurrent_code_additions_all_land(self):
        errors = []

        def add(i):
            try:
                client = Client(self.server)
                resp = client.post("/api/add-code", {"id": f"c-{i}", "name": f"Code {i}",
                                                     "parentId": None, "order": i})
                if resp.status != 200:
                    errors.append(resp.status)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = {n["id"] for n in self.codes()}
        self.assertTrue({f"c-{i}" for i in range(20)} <= ids)
        self.assertEqual(self.client.post("/api/add-code", {"id": "c-1", "name": "Dup"}).status, 409)

    def test_delete_and_prune_still_work(self):
        self.client.post("/api/add-code", {"id": "unused-1", "name": "Unused", "parentId": None})
        resp = self.client.post("/api/delete-code?id=code-root1")
        self.assertFalse(resp.json()["ok"])  # still used by an annotation
        resp = self.client.post("/api/prune-codes")
        self.assertEqual(resp.json()["pruned"], 1)
        self.assertEqual({n["id"] for n in self.codes()}, {"code-root1"})


if __name__ == "__main__":
    unittest.main()

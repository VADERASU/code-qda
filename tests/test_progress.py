"""Who coded what and when, and how far each transcript's coding has got."""
import json
import unittest

from support import (Client, appmod, fixture_project, local_server, signed_in, temp_dir,
                     hosted_setup, txfmt)


class Stamps(unittest.TestCase):
    def setUp(self):
        self.project = fixture_project(temp_dir(self))
        self.client = Client(local_server(self, self.project))
        self.md = self.project.md_dir

    def load(self):
        return self.client.get("/api/codes?name=alpha.md").json()

    def stored(self):
        return {a["id"]: a for a in json.loads((self.md / "alpha.annot.json").read_text())}

    def save(self, annotations, rev):
        resp = self.client.post("/api/codes?name=alpha.md",
                                {"annotations": annotations, "rev": rev})
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()["rev"]

    def test_new_codings_are_stamped_by_the_server_only(self):
        data = self.load()
        legacy = data["annotations"][0]
        forged = dict(legacy, id="ann-2", ts="2001-01-01T00:00:00+00:00", by="mallory")
        self.save(data["annotations"] + [forged], data["rev"])
        stored = self.stored()
        self.assertNotIn("ts", stored["ann-1"], "an old coding is not dated to its next save")
        self.assertNotIn("by", stored["ann-2"], "local mode has no accounts to name")
        self.assertNotEqual(stored["ann-2"]["ts"], forged["ts"])
        self.assertIsNotNone(appmod.parse_stamp(stored["ann-2"]["ts"]))

    def test_stamp_survives_resize_and_cannot_be_rewritten(self):
        data = self.load()
        rev = self.save(data["annotations"] + [dict(data["annotations"][0], id="ann-2")],
                        data["rev"])
        first = self.stored()["ann-2"]["ts"]
        moved = [dict(a) for a in self.stored().values()]
        moved[1].update(start=moved[1]["start"] + 1, ts="2001-01-01T00:00:00+00:00",
                        by="mallory")
        self.save(moved, rev)
        stored = self.stored()["ann-2"]
        self.assertEqual(stored["ts"], first)
        self.assertNotIn("by", stored)

    def test_transcript_edit_keeps_stamps(self):
        data = self.load()
        self.save(data["annotations"] + [dict(data["annotations"][0], id="ann-2")],
                  data["rev"])
        stamp = self.stored()["ann-2"]["ts"]
        resp = self.client.post("/api/tx-edit?name=alpha.md",
                                {"seg": "s0001", "text": "How do you use the robot arm?"})
        self.assertEqual(resp.status, 200, resp.body)
        stored = self.stored()["ann-2"]
        self.assertEqual(stored["ts"], stamp)
        self.assertEqual(resp.json()["moved"], 2)


class HostedStamps(unittest.TestCase):
    def test_codings_record_the_signed_in_handle(self):
        env = hosted_setup(self)
        carol = signed_in(env, "carol@example.edu")
        data = carol.get("/p/alpha/api/codes?name=one.md").json()
        new = dict(data["annotations"][0], id="ann-2", by="alice")
        resp = carol.post("/p/alpha/api/codes?name=one.md",
                          {"annotations": data["annotations"] + [new], "rev": data["rev"]})
        self.assertEqual(resp.status, 200, resp.body)
        stored = json.loads((env["alpha"].md_dir / "one.annot.json").read_text())
        self.assertEqual(stored[1]["by"], "carol")
        files = carol.get("/p/alpha/api/files").json()
        self.assertEqual(files[0]["lastChange"]["by"], "carol")
        self.assertEqual(files[0]["lastChange"]["kind"], "coding")


class FileList(unittest.TestCase):
    def setUp(self):
        self.project = fixture_project(temp_dir(self), stems=("alpha", "beta"))
        self.client = Client(local_server(self, self.project))
        self.md = self.project.md_dir

    def files(self):
        resp = self.client.get("/api/files")
        self.assertEqual(resp.status, 200)
        return {f["name"]: f for f in resp.json()}

    def test_progress_is_the_farthest_coding(self):
        text = (self.md / "alpha.md").read_text()
        end = json.loads((self.md / "alpha.annot.json").read_text())[0]["end"]
        (self.md / "beta.annot.json").write_text("[]")
        files = self.files()
        self.assertEqual(files["alpha.md"]["coding"],
                         {"count": 1, "through": round(end / len(text), 3)})
        self.assertEqual(files["beta.md"]["coding"], {"count": 0, "through": 0})
        self.assertIsNone(files["beta.md"]["lastChange"], "unstamped data has no history")

    def test_codings_the_view_would_not_draw_do_not_count(self):
        text = (self.md / "alpha.md").read_text()
        annotations = [
            {"id": "a", "codeId": "no-such-code", "text": "", "start": 0, "end": len(text)},
            {"id": "b", "codeId": "code-root1", "text": "", "start": 5, "end": len(text) + 9},
            {"id": "c", "codeId": "code-root1", "text": "", "start": 7, "end": 7},
        ]
        (self.md / "alpha.annot.json").write_text(json.dumps(annotations))
        self.assertEqual(self.files()["alpha.md"]["coding"], {"count": 0, "through": 0})

    def test_last_change_is_the_latest_across_codings_and_corrections(self):
        annotations = json.loads((self.md / "alpha.annot.json").read_text())
        # 15:30 in Los Angeles is after 17:00 in Chicago, though as text it sorts before.
        annotations[0].update(ts="2026-11-20T15:30:00-08:00", by="erick")
        (self.md / "alpha.annot.json").write_text(json.dumps(annotations))
        tx = txfmt.load(self.md / "alpha.tx.json")
        tx["edits"] = [{"ts": "2026-11-20T17:00:00-06:00", "by": "", "seg": "s0001"},
                       {"ts": "not a time", "by": "x"}, "junk"]
        txfmt.save(self.md / "alpha.tx.json", tx)
        self.assertEqual(self.files()["alpha.md"]["lastChange"],
                         {"ts": "2026-11-20T15:30:00-08:00", "by": "erick", "kind": "coding"})
        tx["edits"].append({"ts": "2026-11-20T18:00:00-06:00", "by": "", "seg": "s0001"})
        txfmt.save(self.md / "alpha.tx.json", tx)
        self.assertEqual(self.files()["alpha.md"]["lastChange"],
                         {"ts": "2026-11-20T18:00:00-06:00", "by": None, "kind": "correction"})


if __name__ == "__main__":
    unittest.main()

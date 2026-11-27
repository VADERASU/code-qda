"""Uploading transcripts and audio: what is accepted, and nothing is replaced."""
import json
import os
import socket
import unittest
from collections import namedtuple
from unittest import mock

from support import (ORIGIN, Client, appmod, fixture_project, hosted_client, hosted_setup,
                     local_server, make_tx, signed_in, temp_dir, txfmt)

BOB = "bob@example.edu"
OGG = b"OggS" + os.urandom(5000)
RAW = {"Content-Type": "application/octet-stream"}


def upload(client, name, data, prefix="/p/beta"):
    from urllib.parse import quote
    return client.post(f"{prefix}/api/upload?file={quote(name)}", data, headers=RAW)


def leftovers(directory):
    return [p.name for p in directory.iterdir() if p.name.startswith(".")]


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.bob = signed_in(self.env, BOB)
        self.md = self.env["beta"].md_dir

    def test_text_file_becomes_a_transcript(self):
        resp = upload(self.bob, "notes.txt", "﻿Q: Hi.\r\nA: Hello.\r\n".encode())
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.json(), {"ok": True, "kind": "transcript", "name": "notes.md"})
        self.assertEqual((self.md / "notes.md").read_bytes(), b"Q: Hi.\nA: Hello.\n")
        self.assertEqual((self.md / "notes.md").stat().st_mode & 0o777, 0o600)
        names = [f["name"] for f in self.bob.get("/p/beta/api/files").json()]
        self.assertIn("notes.md", names)
        self.assertEqual(self.bob.get("/p/beta/api/file?name=notes.md").json()["content"],
                         "Q: Hi.\nA: Hello.\n")
        self.assertIn('"file": "notes.txt"', (self.env["state"] / "audit.log").read_text())

    def test_tx_json_is_rendered(self):
        tx = make_tx("", [("SPEAKER_00", "What do you study?"),
                          ("SPEAKER_01", "Soil, mostly.")], "soil.16k.opus")
        tx["audioAvailable"] = True
        tx["segments"][0]["span"] = [0, 5]
        resp = upload(self.bob, "soil.tx.json", json.dumps(tx).encode())
        self.assertEqual(resp.status, 200, resp.body)
        stored = txfmt.load(self.md / "soil.tx.json")
        self.assertNotIn("audioAvailable", stored)
        self.assertNotIn("span", stored["segments"][0])
        self.assertEqual(stored["title"], "soil")
        self.assertEqual((self.md / "soil.md").read_text(), txfmt.render_md(stored)[0])
        self.assertEqual(self.bob.get("/p/beta/api/tx?name=soil.md").status, 200)

        # Its audio, uploaded afterwards, makes it playable.
        listed = {f["name"]: f for f in self.bob.get("/p/beta/api/files").json()}
        self.assertFalse(listed["soil.md"]["audioAvailable"])
        self.assertEqual(upload(self.bob, "soil.16k.opus", OGG).status, 200)
        listed = {f["name"]: f for f in self.bob.get("/p/beta/api/files").json()}
        self.assertTrue(listed["soil.md"]["audioAvailable"])

    def test_nothing_is_replaced(self):
        before = {p.name: p.read_bytes() for p in self.md.iterdir()}
        for name in ("two.md", "two.txt", "two.tx.json", "two.MD"):   # all become two.md
            with self.subTest(name=name):
                data = json.dumps(make_tx("x", [("A", "b")], "a.opus")).encode() \
                    if name.endswith("json") else b"new text"
                resp = upload(self.bob, name, data)
                self.assertEqual(resp.status, 409, resp.body)
        self.assertEqual({n: b for n, b in ((p.name, p.read_bytes()) for p in self.md.iterdir())
                          if n in before}, before)
        # Leftover codings from a removed transcript are never attached to a new one.
        (self.md / "ghost.annot.json").write_text("[]")
        self.assertEqual(upload(self.bob, "ghost.txt", b"text").status, 409)

    def test_refusals(self):
        bad_tx = make_tx("x", [("A", "one"), ("A", "two")], "a.opus")
        bad_tx["segments"][1]["id"] = bad_tx["segments"][0]["id"]
        cases = [
            ("report.pdf", b"%PDF", 415),
            ("empty.txt", b"", 400),
            ("latin1.txt", "caf\xe9".encode("latin-1"), 400),
            ("blank.md", b"  \n\n", 400),
            ("nul.txt", b"a\x00b", 400),
            (".hidden.md", b"x", 400),
            (" spaced.md", b"x", 400),
            ("junk.tx.json", b"{not json", 400),
            ("old.tx.json", json.dumps({"version": 2, "segments": []}).encode(), 400),
            ("dupes.tx.json", json.dumps(bad_tx).encode(), 400),
            ("times.tx.json", json.dumps(dict(bad_tx, segments=[
                {"id": "s1", "start": "soon", "end": 1, "text": "x"}])).encode(), 400),
        ]
        for name, data, status in cases:
            with self.subTest(name=name):
                resp = upload(self.bob, name, data)
                self.assertEqual(resp.status, status, resp.body)
                self.assertTrue(resp.json()["error"])
        self.assertEqual(sorted(p.name for p in self.md.iterdir()),
                         ["codes.json", "two.annot.json", "two.md", "two.tx.json"])

    def headers_only(self, path, length, cookie=True):
        """Announce a body without sending it: the reply must come from the
        headers alone, with nothing read."""
        sock = socket.create_connection(("127.0.0.1", self.env["server"].port), timeout=5)
        self.addCleanup(sock.close)
        session = f"Cookie: __Host-qda_session={self.bob.cookie}\r\n" if cookie else ""
        sock.sendall((f"POST {path} HTTP/1.1\r\nHost: qda.test\r\nOrigin: {ORIGIN}\r\n"
                      f"{session}Content-Type: application/octet-stream\r\n"
                      f"Content-Length: {length}\r\n\r\n").encode())
        return int(sock.recv(200).split(b" ", 2)[1])

    def test_size_limits(self):
        up = "/p/beta/api/upload?file="
        self.assertEqual(self.headers_only(up + "big.opus", appmod.MAX_UPLOAD + 1),
                         413)
        self.assertEqual(self.headers_only(up + "long.txt", appmod.MAX_TRANSCRIPT_UPLOAD + 1),
                         413)
        self.assertEqual(self.headers_only(up + "big.opus", appmod.MAX_UPLOAD, cookie=False),
                         401)
        # Every other write keeps the small JSON limit.
        self.assertEqual(self.headers_only("/p/beta/api/save-codes", appmod.MAX_BODY + 1),
                         413)
        with mock.patch.object(appmod, "MAX_TRANSCRIPT_UPLOAD", 100):
            self.assertEqual(upload(self.bob, "long.txt", b"x" * 101).status, 413)
        self.assertFalse((self.md / "long.md").exists())

    def test_who_can_upload(self):
        carol = signed_in(self.env, "carol@example.edu")
        self.assertEqual(upload(carol, "x.txt", b"hi").status, 404)       # not in beta
        self.assertEqual(upload(hosted_client(self.env), "x.txt", b"hi").status, 401)
        evil = hosted_client(self.env)
        evil.cookie, evil.origin = self.bob.cookie, "https://evil.example"
        self.assertEqual(upload(evil, "x.txt", b"hi").status, 403)
        self.assertFalse((self.md / "x.md").exists())
        # A sandbox upload lands only in that person's sandbox.
        self.assertEqual(upload(self.bob, "mine.txt", b"hi", prefix="/p/sandbox").status, 200)
        self.assertTrue((self.env["state"] / "sandboxes" / "bob" / "transcripts" / "mine.md").exists())
        self.assertNotIn("mine.md", [f["name"] for f in
                                     carol.get("/p/sandbox/api/files").json()])


class Audio(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        self.bob = signed_in(self.env, BOB)
        self.audio = self.env["beta"].audio_dir

    def test_audio_is_stored_privately_and_streams(self):
        resp = upload(self.bob, "visit.opus", OGG)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.json(), {"ok": True, "kind": "audio", "name": "visit.opus"})
        self.assertEqual((self.audio / "visit.opus").read_bytes(), OGG)
        self.assertEqual((self.audio / "visit.opus").stat().st_mode & 0o777, 0o600)
        resp = self.bob.get("/p/beta/api/audio?name=visit.opus", headers={"Range": "bytes=0-99"})
        self.assertEqual((resp.status, resp.body), (206, OGG[:100]))
        self.assertEqual(upload(self.bob, "visit.opus", OGG).status, 409)
        self.assertEqual(leftovers(self.audio), [])

    def test_contents_must_match_the_type(self):
        good = {"a.m4a": b"\0\0\0\x18ftypM4A " + bytes(64), "a.mp3": b"ID3" + bytes(64),
                "b.mp3": b"\xff\xfb\x90" + bytes(64), "a.wav": b"RIFF\0\0\0\0WAVEfmt " + bytes(64),
                "a.ogg": OGG}
        for name, data in good.items():
            with self.subTest(name=name):
                self.assertEqual(upload(self.bob, name, data).status, 200)
        for name in ("c.m4a", "c.mp3", "c.wav", "c.opus"):
            with self.subTest(name=name):
                resp = upload(self.bob, name, b"<html>" + bytes(64))
                self.assertEqual(resp.status, 400)
                self.assertFalse((self.audio / name).exists())
        self.assertEqual(leftovers(self.audio), [])

    def test_disk_space(self):
        usage = namedtuple("usage", "total used free")
        with mock.patch.object(appmod.shutil, "disk_usage",
                               return_value=usage(10, 9, appmod.DISK_RESERVE + 100)):
            resp = upload(self.bob, "visit.opus", OGG)
        self.assertEqual(resp.status, 507)
        self.assertEqual(list(self.audio.glob("visit*")), [])
        self.assertEqual(self.bob.get("/p/beta/api/files").status, 200)  # connection reusable


class LocalMode(unittest.TestCase):
    def test_upload_works_locally(self):
        project = fixture_project(temp_dir(self), "local")
        client = Client(local_server(self, project))
        self.assertEqual(upload(client, "new.txt", b"hello", prefix="").status, 200)
        self.assertTrue((project.md_dir / "new.md").exists())
        self.assertEqual(upload(client, "new.opus", OGG, prefix="").status, 200)


if __name__ == "__main__":
    unittest.main()

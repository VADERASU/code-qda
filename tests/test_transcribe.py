"""Transcribing uploaded recordings on the server, with a stand-in pipeline."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import qda_transcribe
from qda_transcribe import Transcriber, _error_from, hf_token
from support import (ROOT, Client, fixture_project, local_server, hosted_setup, signed_in,
                     temp_dir, txfmt)

BOB = "bob@example.edu"
M4A = b"\0\0\0\x18ftypM4A " + bytes(4000)
RAW = {"Content-Type": "application/octet-stream"}

# Takes run.py's arguments, prints its progress lines, writes what it writes.
FAKE = f'''
import argparse, json, os, sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
import txfmt
ap = argparse.ArgumentParser()
ap.add_argument("audio"); ap.add_argument("--outdir"); ap.add_argument("--install")
ap.add_argument("--audio-dir"); ap.add_argument("--no-second-opinion", action="store_true")
a = ap.parse_args()
how = json.loads(os.environ.get("FAKE_PIPELINE", "{{}}"))
record = os.environ.get("FAKE_RECORD")
if record:
    Path(record).write_text(json.dumps({{"speakers": os.environ.get("SPEAKERS"),
        "token": bool(os.environ.get("HF_TOKEN")), "skip": a.no_second_opinion,
        "argv": sys.argv[1:]}}))
print("Preparing " + Path(a.audio).name, flush=True)
print("  Transcribing and preparing playback audio...", flush=True)
time.sleep(how.get("sleep", 0))
if how.get("fail"):
    print("HF_TOKEN is not set.", flush=True)
    sys.exit(1)
print("  Refining word timings...", flush=True)
title = Path(a.audio).stem.removesuffix(".16k")
segments = [{{"id": "s0001", "speaker": "SPEAKER_00", "start": 0.5, "end": 2.0,
             "text": "So, tell me about the site.", "words": [], "status": "unreviewed"}}]
tx = {{"version": 1, "title": title, "audio": title + ".16k.opus", "duration": 2.0,
      "speakers": {{"SPEAKER_00": "SPEAKER_00"}}, "segments": segments, "edits": []}}
print("  Building and installing the transcript...", flush=True)
Path(a.install, title + ".tx.json").write_text(json.dumps(tx))
Path(a.install, title + ".md").write_text(txfmt.render_md(tx)[0])
Path(a.audio_dir, title + ".16k.opus").write_bytes(b"OggS" + bytes(100))
'''


def upload(client, name, data, prefix="/p/beta"):
    return client.post(f"{prefix}/api/upload?file={name}", data, headers=RAW)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = hosted_setup(self)
        base = self.env["base"]
        self.fake = base / "fake_run.py"
        self.fake.write_text(FAKE)
        self.record = base / "seen.json"
        patcher = mock.patch.dict(os.environ, {"HF_TOKEN": "hf_test", "FAKE_RECORD": str(self.record)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.problems = []
        self.transcriber = self.make_transcriber()
        self.transcriber.start()
        self.addCleanup(self.transcriber.stop)
        self.env["app"].transcriber = self.transcriber
        self.bob = signed_in(self.env, BOB)

    def make_transcriber(self):
        return Transcriber(self.env["store"], self.env["state"] / "transcription",
                           pipeline=self.fake, check=lambda: list(self.problems),
                           second_opinion=lambda: False)

    def behave(self, **how):
        os.environ["FAKE_PIPELINE"] = json.dumps(how)

    def recordings(self, client=None, prefix="/p/beta"):
        resp = (client or self.bob).get(f"{prefix}/api/recordings")
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def job(self, name, client=None, prefix="/p/beta"):
        found = {r["name"]: r for r in self.recordings(client, prefix)["recordings"]}
        return found[name]["job"] if name in found else None

    def wait(self, name, statuses=("done", "failed", "cancelled"), client=None, prefix="/p/beta"):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.job(name, client, prefix)
            if job and job["status"] in statuses:
                return job
            time.sleep(0.05)
        self.fail(f"{name} never reached {statuses}: {self.job(name, client, prefix)}")

    def start(self, name, speakers=2, client=None, prefix="/p/beta"):
        return (client or self.bob).post(f"{prefix}/api/transcribe",
                                         {"audio": name, "speakers": speakers})


class Flow(Base):
    def test_upload_transcribe_then_verify(self):
        self.behave()
        self.assertEqual(upload(self.bob, "visit.m4a", M4A).status, 200)
        data = self.recordings()
        self.assertEqual(data["recordings"], [{"name": "visit.m4a", "size": len(M4A), "job": None}])
        self.assertTrue(data["transcribe"]["available"])

        resp = self.start("visit.m4a", speakers=3)
        self.assertEqual(resp.status, 200, resp.body)
        job = self.wait("visit.m4a")
        self.assertEqual((job["status"], job["transcript"], job["by"]), ("done", "visit.md", "bob"))

        seen = json.loads(self.record.read_text())
        self.assertEqual((seen["speakers"], seen["token"], seen["skip"]), ("3", True, True))
        md, audio = self.env["beta"].md_dir, self.env["beta"].audio_dir
        for path in (md / "visit.md", md / "visit.tx.json", audio / "visit.16k.opus"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        files = {f["name"]: f for f in self.bob.get("/p/beta/api/files").json()}
        self.assertTrue(files["visit.md"]["audioAvailable"])
        self.assertEqual(self.bob.get("/p/beta/api/tx?name=visit.md").status, 200)
        # The playback render is not offered as another recording.
        self.assertEqual([r["name"] for r in self.recordings()["recordings"]], ["visit.m4a"])
        self.assertEqual(list((self.env["state"] / "transcription").iterdir()), [])
        log = (self.env["state"] / "audit.log").read_text()
        self.assertIn('"event": "transcription_queued"', log)
        # Its transcript exists now, so it can't be transcribed over.
        self.assertEqual(self.start("visit.m4a").status, 400)

    def test_progress_is_shown_while_running(self):
        self.behave(sleep=1.5)
        upload(self.bob, "visit.m4a", M4A)
        self.start("visit.m4a")
        job = self.wait("visit.m4a", statuses=("running",))
        deadline = time.monotonic() + 5
        while job["stage"] != "Transcribing" and time.monotonic() < deadline:
            time.sleep(0.05)
            job = self.job("visit.m4a")
        self.assertEqual(job["stage"], "Transcribing")
        self.assertEqual(self.wait("visit.m4a")["status"], "done")

    def test_failure_then_retry(self):
        self.behave(fail=True)
        upload(self.bob, "visit.m4a", M4A)
        self.start("visit.m4a")
        job = self.wait("visit.m4a")
        self.assertEqual((job["status"], job["error"]), ("failed", "HF_TOKEN is not set."))
        self.behave()
        self.assertEqual(self.start("visit.m4a").status, 200)
        self.assertEqual(self.wait("visit.m4a")["status"], "done")

    def test_cancel(self):
        self.behave(sleep=30)
        upload(self.bob, "a.m4a", M4A)
        upload(self.bob, "b.m4a", M4A)
        self.start("a.m4a")
        self.wait("a.m4a", statuses=("running",))
        self.start("b.m4a")
        b = self.job("b.m4a")
        self.assertEqual((b["status"], b["ahead"]), ("queued", 1))
        self.assertEqual(self.bob.post("/p/beta/api/transcribe/cancel", {"id": b["id"]}).status, 200)
        self.assertEqual(self.job("b.m4a")["status"], "cancelled")
        a = self.job("a.m4a")
        started = time.monotonic()
        self.assertEqual(self.bob.post("/p/beta/api/transcribe/cancel", {"id": a["id"]}).status, 200)
        self.assertEqual(self.wait("a.m4a")["status"], "cancelled")
        self.assertLess(time.monotonic() - started, 5)   # the 30 s process was stopped
        self.assertEqual(self.bob.post("/p/beta/api/transcribe/cancel", {"id": a["id"]}).status, 400)
        self.assertEqual(self.bob.post("/p/beta/api/transcribe/cancel", {"id": "nope"}).status, 404)

    def test_nothing_is_replaced_if_a_file_appears_meanwhile(self):
        self.behave(sleep=1)
        upload(self.bob, "visit.m4a", M4A)
        self.start("visit.m4a")
        self.wait("visit.m4a", statuses=("running",))
        self.assertEqual(upload(self.bob, "visit.txt", b"typed up by hand").status, 200)
        job = self.wait("visit.m4a")
        self.assertEqual(job["status"], "failed")
        self.assertIn("appeared while this ran", job["error"])
        self.assertEqual((self.env["beta"].md_dir / "visit.md").read_text(), "typed up by hand")

    def test_restart_resumes_a_running_job(self):
        self.behave()
        upload(self.bob, "visit.m4a", M4A)
        self.transcriber.stop()
        self.env["store"].update(qda_transcribe.JOBS, qda_transcribe.EMPTY, lambda doc: doc.setdefault(
            "jobs", {}).update(x={"id": "x", "slug": "beta", "md_dir": str(self.env["beta"].md_dir),
                                  "audio_dir": str(self.env["beta"].audio_dir), "audio": "visit.m4a",
                                  "stem": "visit", "speakers": 2, "by": BOB, "handle": "bob",
                                  "status": "running", "created": 1.0}))
        self.transcriber = self.make_transcriber()
        self.env["app"].transcriber = self.transcriber
        self.transcriber.start()
        self.addCleanup(self.transcriber.stop)
        self.assertEqual(self.wait("visit.m4a")["status"], "done")


class Refusals(Base):
    def test_bad_requests(self):
        self.behave(sleep=30)
        upload(self.bob, "visit.m4a", M4A)
        upload(self.bob, "two.m4a", M4A)      # beta already has a two.md
        for body, status in [
            ({"audio": "missing.m4a", "speakers": 2}, 404),
            ({"audio": "two.md", "speakers": 2}, 400),
            ({"audio": "two.m4a", "speakers": 2}, 400),
            ({"audio": "visit.m4a", "speakers": 0}, 400),
            ({"audio": "visit.m4a", "speakers": 11}, 400),
            ({"audio": "visit.m4a", "speakers": "2"}, 400),
            ({"audio": "visit.m4a", "speakers": True}, 400),
            ({"audio": None, "speakers": 2}, 404),
        ]:
            with self.subTest(body=body):
                resp = self.bob.post("/p/beta/api/transcribe", body)
                self.assertEqual(resp.status, status, resp.body)
                self.assertTrue(resp.json()["error"])
        self.assertEqual(self.start("visit.m4a").status, 200)
        self.assertIn("already being transcribed", self.start("visit.m4a").json()["error"])

    def test_limits_and_access(self):
        self.behave(sleep=30)
        upload(self.bob, "a.m4a", M4A)
        upload(self.bob, "b.m4a", M4A)
        with mock.patch.object(qda_transcribe, "MAX_ACTIVE_PER_PERSON", 1):
            self.assertEqual(self.start("a.m4a").status, 200)
            self.assertIn("at most 1", self.start("b.m4a").json()["error"])
        carol = signed_in(self.env, "carol@example.edu")     # not in beta
        self.assertEqual(carol.get("/p/beta/api/recordings").status, 404)
        self.assertEqual(self.start("b.m4a", client=carol).status, 404)

    def test_not_set_up(self):
        self.problems[:] = ["a Hugging Face token in bescribe/.env"]
        upload(self.bob, "visit.m4a", M4A)
        data = self.recordings()
        self.assertEqual(data["transcribe"]["available"], False)
        self.assertEqual(data["transcribe"]["problems"], ["a Hugging Face token in bescribe/.env"])
        resp = self.start("visit.m4a")
        self.assertEqual(resp.status, 400)
        self.assertIn("Hugging Face token", resp.json()["error"])

    def test_sandboxes_stay_private(self):
        self.behave()
        alice = signed_in(self.env, "alice@example.edu")
        self.assertEqual(upload(alice, "mine.m4a", M4A, prefix="/p/sandbox").status, 200)
        self.assertEqual(self.start("mine.m4a", client=alice, prefix="/p/sandbox").status, 200)
        self.assertEqual(self.wait("mine.m4a", client=alice, prefix="/p/sandbox")["status"], "done")
        self.assertEqual(self.recordings(prefix="/p/sandbox")["recordings"], [])   # bob's sandbox
        self.assertEqual(self.recordings()["recordings"], [])                       # beta


class Helpers(unittest.TestCase):
    def test_token_from_env_file(self):
        env_file = temp_dir(self) / ".env"
        env_file.write_text("# comment\nOTHER=1\nHF_TOKEN='hf_abc'\n")
        with mock.patch.object(qda_transcribe, "ENV_FILE", env_file), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hf_token(), "hf_abc")
            env_file.write_text("HF_TOKEN=\n")
            self.assertIsNone(hf_token())
        with mock.patch.dict(os.environ, {"HF_TOKEN": "hf_env"}):
            self.assertEqual(hf_token(), "hf_env")

    def test_error_line(self):
        self.assertEqual(_error_from(["=== x ===", "loading model", "RuntimeError: CUDA out of memory",
                                      "  done"]), "RuntimeError: CUDA out of memory")
        self.assertEqual(_error_from([]), "The transcription stopped unexpectedly.")

    def test_local_mode_points_to_make_prepare(self):
        project = fixture_project(temp_dir(self), "local")
        client = Client(local_server(self, project))
        data = client.get("/api/recordings").json()
        self.assertEqual(data["transcribe"]["local"], True)
        self.assertEqual([r["name"] for r in data["recordings"]], [])  # alpha.16k.opus is playback
        resp = client.post("/api/transcribe", {"audio": "alpha.16k.opus", "speakers": 2})
        self.assertEqual(resp.status, 400)
        self.assertIn("make prepare", resp.json()["error"])


if __name__ == "__main__":
    unittest.main()

"""qda_transcribe — transcribe uploaded recordings on this server.

A member uploads a recording and asks for a transcript. This runs the
existing pipeline (bescribe/pipeline/run.py: WhisperX, speaker diarization,
word re-alignment, build_tx) in a background thread, one job at a time,
since there is one GPU. Nothing leaves this machine; the models run here.

The pipeline never writes into a project. It works in a private staging
folder under the state directory, and only a finished transcript is moved
in, under the project's lock and with the same never-replace rule as
uploads, so a job can't collide with someone's upload or edit.

Jobs are kept in transcriptions.json in the state directory, so their status
survives a restart. A job that was running when the server stopped starts
again on the next start, reusing the stages it had finished.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import txfmt
from qda_projects import lock_for
from qda_store import STATE_FILE_MODE, ensure_private_dir

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "bescribe" / "pipeline" / "run.py"
ENV_FILE = ROOT / "bescribe" / ".env"
WX_PYTHON = ROOT / ".venv-wx" / "bin" / "python"
WHISPERX = ROOT / ".venv-wx" / "bin" / "whisperx"
ASR_PYTHON = ROOT / ".venv-asr" / "bin" / "python"
SECOND_OPINION = ROOT / "bescribe" / "pipeline" / "second_opinion.py"

JOBS = "transcriptions.json"
EMPTY = {"version": 1, "jobs": {}}
AUDIO_SUFFIXES = (".opus", ".ogg", ".m4a", ".mp3", ".wav")
ACTIVE = ("queued", "running")
MAX_ACTIVE_PER_PERSON = 3       # waiting or running, across all their projects
MAX_SPEAKERS = 10
MAX_JOB_SECONDS = 6 * 3600
KEEP_JOBS = 200                 # finished jobs beyond this are forgotten, oldest first

# run.py's progress lines -> what the person is told.
STAGES = (
    ("normalising audio", "Preparing the audio"),
    ("Transcribing and preparing", "Transcribing"),
    ("transcribing + diarizing", "Transcribing and telling speakers apart"),
    ("rendering browser audio", "Preparing audio for playback"),
    ("Preparing playback audio", "Preparing audio for playback"),
    ("Running the second transcription", "Running a second transcription to compare"),
    ("Refining word timings", "Refining word timings"),
    ("Building and installing", "Building the transcript"),
)


class TranscribeError(ValueError):
    """A request that can't be met, with a message for the person."""


class NotFound(TranscribeError):
    pass


def hf_token():
    """The Hugging Face token: from the environment, else bescribe/.env.

    Read when a job starts, so adding it needs no restart. Never logged."""
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "HF_TOKEN":
            value = value.strip().strip("'\"")
            if value:
                return value
    return None


def setup_problems():
    """What this server is missing to transcribe, in plain words."""
    problems = []
    if not WX_PYTHON.exists() or (platform.system() != "Darwin" and not WHISPERX.exists()):
        problems.append("the transcription environment (run make install-wx)")
    if shutil.which("ffmpeg") is None:
        problems.append("ffmpeg")
    if not hf_token():
        problems.append("a Hugging Face token in bescribe/.env")
    return problems


def second_opinion_available():
    return SECOND_OPINION.exists() and ASR_PYTHON.exists()


def transcript_stem(audio_name):
    """The transcript a recording becomes: interview-01.m4a -> interview-01."""
    stem = Path(audio_name).stem
    return stem.removesuffix(".realigned").removesuffix(".16k")


def _stage_for(line):
    for marker, label in STAGES:
        if marker in line:
            return label
    return None


def _error_from(tail):
    """The most telling line of a failed run's output."""
    lines = [l for l in tail if l and not l.startswith(("===", "->"))]
    for line in reversed(lines):
        if any(w in line.lower() for w in ("error", "not set", "not found", "failed", "no such")):
            return line[:300]
    return (lines[-1] if lines else "The transcription stopped unexpectedly.")[:300]


def _install_bytes(src, dst):
    """Copy a file in so it appears whole or not at all, readable only by us."""
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, STATE_FILE_MODE)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Transcriber:
    def __init__(self, store, work_root, pipeline=PIPELINE, check=setup_problems,
                 second_opinion=second_opinion_available, clock=time.time):
        self.store = store
        self.work_root = Path(work_root)
        self.pipeline = Path(pipeline)
        self.check = check
        self.second_opinion = second_opinion
        self.clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc = None
        self._current = None
        self._cancelled = set()
        self._thread = None

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        ensure_private_dir(self.work_root)

        def requeue(doc):
            for job in doc.setdefault("jobs", {}).values():
                if job.get("status") == "running":
                    job.update(status="queued", stage="Waiting to restart after a server restart")
        self.store.update(JOBS, EMPTY, requeue)
        self._thread = threading.Thread(target=self._loop, name="transcriber", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)
        if self._thread is not None:
            self._thread.join(timeout=15)

    # --- requests -------------------------------------------------------------

    def status(self):
        problems = self.check()
        return {"available": not problems, "problems": problems,
                "secondOpinion": self.second_opinion()}

    def submit(self, project, audio, speakers, email, handle):
        if Path(audio).suffix.lower() not in AUDIO_SUFFIXES:
            raise TranscribeError("Only audio files can be transcribed.")
        if not (project.audio_dir / audio).is_file():
            raise NotFound(f"There's no recording named {audio}.")
        if not (isinstance(speakers, int) and not isinstance(speakers, bool)
                and 1 <= speakers <= MAX_SPEAKERS):
            raise TranscribeError(f"Say how many people speak, from 1 to {MAX_SPEAKERS}.")
        stem = transcript_stem(audio)
        for suffix in (".md", ".tx.json", ".annot.json"):
            if os.path.lexists(project.md_dir / (stem + suffix)):
                raise TranscribeError(f"There's already a transcript named {stem}.md.")
        problems = self.check()
        if problems:
            raise TranscribeError("Transcription isn't set up on this server. It needs "
                                  + " and ".join(problems) + ".")
        md_dir, now = str(project.md_dir), self.clock()

        def add(doc):
            jobs = doc.setdefault("jobs", {})
            active = [j for j in jobs.values() if j.get("status") in ACTIVE]
            if any(j["md_dir"] == md_dir and j["audio"] == audio for j in active):
                raise TranscribeError(f"{audio} is already being transcribed.")
            if sum(1 for j in active if j.get("by") == email) >= MAX_ACTIVE_PER_PERSON:
                raise TranscribeError(f"You can have at most {MAX_ACTIVE_PER_PERSON} "
                                      "transcriptions waiting or running.")
            job = {"id": secrets.token_hex(8), "slug": project.slug, "md_dir": md_dir,
                   "audio_dir": str(project.audio_dir), "audio": audio, "stem": stem,
                   "speakers": speakers, "by": email, "handle": handle,
                   "status": "queued", "stage": "Waiting", "created": now,
                   "secondOpinion": bool(self.second_opinion())}
            jobs[job["id"]] = job
            self._prune(jobs)
            return job
        job = self.store.update(JOBS, EMPTY, add)
        self._wake.set()
        return job

    def cancel(self, project, job_id):
        md_dir = str(project.md_dir)

        def change(doc):
            job = doc.get("jobs", {}).get(job_id) if isinstance(job_id, str) else None
            if job is None or job["md_dir"] != md_dir:
                raise NotFound("There's no such transcription.")
            if job["status"] == "queued":
                job.update(status="cancelled", stage="Cancelled", finished=self.clock())
            elif job["status"] != "running":
                raise TranscribeError("That transcription has already finished.")
            return job["status"]
        if self.store.update(JOBS, EMPTY, change) == "running":
            with self._lock:
                # Noted even if its process hasn't started yet: _run checks.
                self._cancelled.add(job_id)
                if self._current == job_id and self._proc is not None:
                    self._terminate(self._proc)

    def jobs_for(self, md_dir):
        """{audio name: its latest job} in one project, queued ones with how
        many jobs (from any project) are ahead of them."""
        jobs = self.store.read(JOBS, EMPTY).get("jobs", {}).values()
        queue = sorted((j for j in jobs if j.get("status") == "queued"), key=lambda j: j["created"])
        running = sum(1 for j in jobs if j.get("status") == "running")
        latest = {}
        for job in sorted(jobs, key=lambda j: j["created"]):
            if job["md_dir"] == str(md_dir):
                latest[job["audio"]] = job
        for job in latest.values():
            if job["status"] == "queued":
                job["ahead"] = running + next(i for i, q in enumerate(queue) if q["id"] == job["id"])
        return latest

    @staticmethod
    def _prune(jobs):
        done = sorted((j for j in jobs.values() if j.get("status") not in ACTIVE),
                      key=lambda j: j.get("finished") or j["created"])
        for job in done[:max(0, len(jobs) - KEEP_JOBS)]:
            del jobs[job["id"]]

    # --- the worker -----------------------------------------------------------

    def _update(self, job_id, **fields):
        def change(doc):
            job = doc.get("jobs", {}).get(job_id)
            if job is not None:
                job.update(fields)
        self.store.update(JOBS, EMPTY, change)

    def _claim(self):
        def change(doc):
            queued = [j for j in doc.get("jobs", {}).values() if j.get("status") == "queued"]
            if not queued:
                return None
            job = min(queued, key=lambda j: j["created"])
            job.update(status="running", stage="Starting", started=self.clock())
            return dict(job)
        return self.store.update(JOBS, EMPTY, change)

    def _loop(self):
        while not self._stop.is_set():
            try:
                job = self._claim()
            except Exception as e:  # a broken jobs file must not kill the worker
                print(f"transcriber: could not read jobs: {e}", file=sys.stderr, flush=True)
                job = None
            if job is None:
                self._wake.wait(5)
                self._wake.clear()
                continue
            try:
                self._run(job)
            except Exception as e:
                print(f"transcriber: job {job['id']} failed: {e!r}", file=sys.stderr, flush=True)
                self._update(job["id"], status="failed", finished=self.clock(),
                             error="The transcription failed on the server.")

    def _work_dir(self, job):
        # Keyed by what determines the result, so a retry reuses finished stages.
        key = json.dumps([job["md_dir"], job["audio"], job["speakers"]])
        return self.work_root / hashlib.sha256(key.encode()).hexdigest()[:24]

    def _terminate(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

        def kill():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        threading.Timer(10, kill).start()

    def _run(self, job):
        problems = self.check()
        if problems:
            self._update(job["id"], status="failed", finished=self.clock(),
                         error="Transcription isn't set up on this server. It needs "
                               + " and ".join(problems) + ".")
            return
        work = self._work_dir(job)
        raw, install, playback = work / "raw", work / "install", work / "audio"
        for path in (work, raw, install, playback):
            path.mkdir(mode=0o700, exist_ok=True)
        argv = [sys.executable, str(self.pipeline), str(Path(job["audio_dir"]) / job["audio"]),
                "--outdir", str(raw), "--install", str(install), "--audio-dir", str(playback)]
        if not job.get("secondOpinion"):
            argv.append("--no-second-opinion")
        env = dict(os.environ, HF_TOKEN=hf_token() or "", SPEAKERS=str(job["speakers"]),
                   PYTHONUNBUFFERED="1")
        with self._lock:
            if self._stop.is_set():
                return
            if job["id"] in self._cancelled:
                self._cancelled.discard(job["id"])
                self._update(job["id"], status="cancelled", stage="Cancelled",
                             finished=self.clock())
                return
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env, cwd=str(ROOT),
                                    start_new_session=True, text=True, errors="replace")
            self._proc, self._current = proc, job["id"]
        timed_out = threading.Event()

        def overtime():
            timed_out.set()
            self._terminate(proc)
        timer = threading.Timer(MAX_JOB_SECONDS, overtime)
        timer.start()
        tail = collections.deque(maxlen=40)
        stage = None
        try:
            with open(work / "log.txt", "a", encoding="utf-8") as log:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    tail.append(line.strip())
                    label = _stage_for(line)
                    if label and label != stage:
                        stage = label
                        self._update(job["id"], stage=label)
            code = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
            with self._lock:
                self._proc = self._current = None
                cancelled = job["id"] in self._cancelled
                self._cancelled.discard(job["id"])
        if cancelled:
            self._update(job["id"], status="cancelled", stage="Cancelled", finished=self.clock())
            shutil.rmtree(work, ignore_errors=True)
        elif self._stop.is_set() and code != 0:
            return  # the server is stopping: left "running", so it restarts next time
        elif code != 0:
            error = ("It took longer than six hours and was stopped." if timed_out.is_set()
                     else _error_from(tail))
            self._update(job["id"], status="failed", finished=self.clock(), error=error)
        else:
            self._install(job, install, playback, work)

    def _install(self, job, install, playback, work):
        """Move the finished transcript and its playback audio into the project."""
        txs = sorted(install.glob("*.tx.json"))
        if len(txs) != 1:
            self._update(job["id"], status="failed", finished=self.clock(),
                         error="The pipeline finished without producing a transcript.")
            return
        stem = txs[0].name[:-len(".tx.json")]
        tx = txfmt.load(txs[0])
        md, _spans = txfmt.render_md(tx)
        md_dir, audio_dir = Path(job["md_dir"]), Path(job["audio_dir"])
        audio_name = Path(tx.get("audio") or "").name
        audio_src = playback / audio_name if audio_name else None
        with lock_for(md_dir):
            taken = [p.name for p in (md_dir / (stem + ".md"), md_dir / (stem + ".tx.json"),
                                      md_dir / (stem + ".annot.json")) if os.path.lexists(p)]
            if audio_src is not None and audio_src.is_file() and os.path.lexists(audio_dir / audio_name):
                taken.append(audio_name)
            if taken:
                self._update(job["id"], status="failed", finished=self.clock(),
                             error=f"{', '.join(taken)} appeared while this ran, and nothing is "
                                   f"ever replaced. Rename or remove it and try again.")
                return
            if audio_src is not None and audio_src.is_file():
                _install_bytes(audio_src, audio_dir / audio_name)
            txfmt.write_text_atomic(md_dir / (stem + ".tx.json"),
                                    json.dumps(tx, indent=1, ensure_ascii=False),
                                    mode=STATE_FILE_MODE)
            txfmt.write_text_atomic(md_dir / (stem + ".md"), md, mode=STATE_FILE_MODE)
        self._update(job["id"], status="done", stage="Done", finished=self.clock(),
                     transcript=stem + ".md")
        shutil.rmtree(work, ignore_errors=True)

"""qda_projects — a project is one study's data: transcripts, codes, audio.

Each project points at its own data directory (the .md / .tx.json /
.annot.json / codes.json files, possibly its own private git repo) and its
own audio directory (IRB-controlled, never committed). Hosted projects use
either storage allocated by the app or existing directories registered by an
admin. Membership is the whole access model in hosted mode: a member sees
everything in a project, a non-member cannot tell it exists.

Local mode has exactly one implicit project, the repository's md/ directory.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from qda_store import STATE_DIR_MODE, StateDirError, ensure_private_dir

ROOT = Path(__file__).resolve().parent
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
PROJECTS_FILE = "projects.json"
EMPTY = {"version": 1, "projects": {}}

_locks = {}
_locks_guard = threading.Lock()


def lock_for(md_dir):
    """One write lock per data directory, however many Project objects exist.

    Project objects are rebuilt whenever the registry changes; keying the lock
    by the resolved directory keeps two of them from ever holding different
    locks over the same files.
    """
    key = os.path.realpath(md_dir)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    md_dir: Path
    audio_dir: Path

    @property
    def codes_file(self):
        return self.md_dir / "codes.json"

    @property
    def lock(self):
        return lock_for(self.md_dir)


def local_project(md_dir, audio_dir):
    return Project("local", "Local", Path(md_dir), Path(audio_dir))


class ProjectError(ValueError):
    pass


def _within(child, parent):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _overlaps(a, b):
    return _within(a, b) or _within(b, a)


def validate_project_dirs(data_dir, audio_dir, *, others=(), protected=(),
                          forbidden_inside=(), roots=None):
    """-> (data, audio) resolved Paths, or raise ProjectError saying why.

    others            [(data, audio)] of every other project; no overlap allowed
    protected         directories a project may not be, or contain (the repo
                      root, the state directory)
    forbidden_inside  directories a project may not sit inside (static/,
                      pages/, the state directory: served or secret)
    roots             {"data": [...], "audio": [...]} — when given, both
                      directories must be inside one of these (the web UI's
                      limit; the CLI passes None)
    """
    out = []
    for label, raw in (("data", data_dir), ("audio", audio_dir)):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ProjectError(f"{label} directory must be an absolute path")
        if not path.is_dir():
            raise ProjectError(f"{label} directory {path} does not exist")
        out.append(path.resolve())
    data, audio = out

    for path, label in ((data, "data"), (audio, "audio")):
        for p in protected:
            p = Path(p).resolve()
            if _within(p, path):
                raise ProjectError(f"{label} directory {path} contains {p}")
        for p in forbidden_inside:
            if _within(path, Path(p).resolve()):
                raise ProjectError(f"{label} directory {path} is inside {p}")
    if _within(data, audio):
        raise ProjectError("data directory cannot be inside the audio directory")

    for other_data, other_audio in others:
        for mine in (data, audio):
            for theirs in (Path(other_data).resolve(), Path(other_audio).resolve()):
                if _overlaps(mine, theirs):
                    raise ProjectError(
                        f"{mine} overlaps another project's directory {theirs}")

    if roots is not None:
        for path, label in ((data, "data"), (audio, "audio")):
            allowed = [Path(r).resolve() for r in roots.get(label, [])]
            if not any(_within(path, r) for r in allowed):
                raise ProjectError(
                    f"{label} directory must be inside one of the configured "
                    f"{label} roots (set with `app.py admin init --{label}-root`)")
    return data, audio


class Registry:
    """Projects and their members, in projects.json in the state directory."""

    def __init__(self, store):
        self.store = store

    def _all(self):
        return self.store.read(PROJECTS_FILE, EMPTY).get("projects", {})

    def records(self):
        return self._all()

    @staticmethod
    def _project(slug, rec):
        return Project(slug, rec.get("name") or slug,
                       Path(rec["data_dir"]), Path(rec["audio_dir"]))

    def for_member(self, slug, email):
        """The project if `email` belongs to it; None if not or no such project.

        Both failures take the same path, so neither the response nor its
        timing tells a non-member that the project exists.
        """
        rec = self._all().get(slug) if isinstance(slug, str) else None
        if rec is None or email not in rec.get("members", ()):
            return None
        return self._project(slug, rec)

    def projects_for(self, email):
        return sorted(
            ({"slug": slug, "name": rec.get("name") or slug}
             for slug, rec in self._all().items()
             if email in rec.get("members", ())),
            key=lambda p: p["name"].lower())

    def add(self, slug, name, data_dir, audio_dir, *, now, protected=(),
            forbidden_inside=(), roots=None):
        """Register existing folders."""
        return self._add(
            slug, name, data_dir, audio_dir, now=now, storage="registered",
            protected=(self.store.dir, *protected),
            forbidden_inside=(self.store.dir, *forbidden_inside), roots=roots)

    def create(self, slug, name, *, now):
        """Allocate private storage for a new hosted project."""
        try:
            return self._add(slug, name, now=now, storage="managed")
        except (OSError, StateDirError) as e:
            raise ProjectError(f"Could not create project storage: {e}") from e

    def _allocate_storage(self, slug, cleanup):
        root = self.store.dir.resolve() / "projects"
        if _overlaps(root, ROOT) or _overlaps(root, Path(os.path.realpath(ROOT / "md"))):
            raise ProjectError("Managed project storage overlaps the checkout or local transcripts")
        root = ensure_private_dir(root)
        directory = Path(tempfile.mkdtemp(prefix=f"{slug}-", dir=root))
        cleanup.callback(directory.rmdir)
        data, audio = directory / "transcripts", directory / "audio"
        for path in (data, audio):
            path.mkdir(mode=STATE_DIR_MODE)
            cleanup.callback(path.rmdir)
        return data, audio

    def _add(self, slug, name, data_dir=None, audio_dir=None, *, now, storage,
             protected=(), forbidden_inside=(), roots=None):
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise ProjectError(
                "Project ID must be 1–40 characters, using lowercase letters, digits or "
                "hyphens, and start and end with a letter or digit.")
        if name is not None and not isinstance(name, str):
            raise ProjectError("Project name must be text")
        name = (name or "").strip() or slug
        if len(name) > 120:
            raise ProjectError("project name is too long")

        with self.store.locked(), ExitStack() as cleanup:
            doc = self.store.read(PROJECTS_FILE, EMPTY, fresh=True)
            projects = doc.setdefault("projects", {})
            if slug in projects:
                raise ProjectError(f"project {slug} already exists")
            if storage == "managed":
                data_dir, audio_dir = self._allocate_storage(slug, cleanup)
            others = [(r["data_dir"], r["audio_dir"]) for r in projects.values()]
            data, audio = validate_project_dirs(
                data_dir, audio_dir, others=others, protected=protected,
                forbidden_inside=forbidden_inside, roots=roots)
            record = {"name": name, "data_dir": str(data), "audio_dir": str(audio),
                      "storage": storage, "members": [], "created": now}
            projects[slug] = record
            self.store.write(PROJECTS_FILE, doc)
            cleanup.pop_all()
            return record

    def set_member(self, slug, email, member):
        def change(doc):
            rec = doc.get("projects", {}).get(slug)
            if rec is None:
                raise ProjectError(f"no project {slug}")
            members = [m for m in rec.get("members", []) if m != email]
            if member:
                members.append(email)
            rec["members"] = sorted(members)
        self.store.update(PROJECTS_FILE, EMPTY, change)

"""qda_projects — a project is one study's data: transcripts, codes, audio.

Each project points at its own data directory (the .md / .tx.json /
.annot.json / codes.json files, possibly its own private git repo) and its
own audio directory (IRB-controlled, never committed). Hosted projects use
either storage allocated by the app or existing directories registered by an
admin, or by any signed-in person, who then owns it. Membership is the whole
access model in hosted mode: a member sees everything in a project, a
non-member cannot tell it exists. An owner can add people who already have
accounts; only admins create accounts.

Every account also has a *sandbox* at /p/sandbox/: a private project only
that person can see, which starts with a copy of the starter files in
starter/ (a made-up interview, a codebook and a few codings) so they can try
things out.

Local mode has exactly one implicit project, the repository's md/ directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import txfmt
from qda_auth import HANDLE_RE
from qda_store import STATE_DIR_MODE, STATE_FILE_MODE, StateDirError, ensure_private_dir

ROOT = Path(__file__).resolve().parent
STARTER_DIR = ROOT / "starter"
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
SANDBOX = "sandbox"
RESERVED_SLUGS = frozenset({SANDBOX})
MAX_OWNED_PROJECTS = 20         # per person; admins are not limited
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


# --- sandboxes ---------------------------------------------------------------

_LEGACY_STARTER_COLORS = {
    "starter-tools": ("#1a73e8", "#1f78b4"),
    "starter-manual": ("#e37400", "#ff7f00"),
    "starter-loss": ("#d93025", "#e31a1c"),
    "starter-workaround": ("#188038", "#b2df8a"),
    "starter-collab": ("#9334e6", "#6a3d9a"),
    "starter-wish": ("#00897b", "#33a02c"),
}


def normalize_starter_colors(nodes):
    """Bring the original sandbox root colors onto the picker palette."""
    for node in nodes:
        if not isinstance(node, dict) or node.get("parentId") is not None:
            continue
        code_id = node.get("id")
        colors = _LEGACY_STARTER_COLORS.get(code_id) if isinstance(code_id, str) else None
        if colors and node.get("color") == colors[0]:
            node["color"] = colors[1]


def person_dir_name(email, handle):
    """A person's sandbox folder: their handle, which is unique and never
    changes. Without one, a digest of the email (which a handle can't look
    like, since handles start with a letter or digit)."""
    if isinstance(handle, str) and HANDLE_RE.fullmatch(handle):
        return handle
    return "_" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]


def _write_private(path, text):
    txfmt.write_text_atomic(path, text, mode=STATE_FILE_MODE)


def _place_codings(annotations, md):
    """Starter codings with offsets that match this render of the .md.

    Offsets are stored with the starter files; if the rendering ever changes,
    each coding is found again by its text, and dropped if it is gone."""
    placed = []
    for ann in annotations if isinstance(annotations, list) else []:
        if not isinstance(ann, dict) or not isinstance(ann.get("text"), str) or not ann["text"]:
            continue
        start = ann.get("start")
        if not (isinstance(start, int) and md[start:start + len(ann["text"])] == ann["text"]):
            start = md.find(ann["text"])
        if start < 0:
            continue
        placed.append(dict(ann, start=start, end=start + len(ann["text"])))
    return placed


def populate_from_starter(md_dir, starter=STARTER_DIR):
    """Copy the starter transcripts, codebook and codings into a new project.

    A .tx.json is rendered to its .md here, so the two always agree.
    """
    starter = Path(starter)
    if not starter.is_dir():
        return
    for src in sorted(starter.iterdir()):
        name = src.name
        if name.startswith(".") or not src.is_file():
            continue
        if name.endswith(".tx.json"):
            stem = name[:-len(".tx.json")]
            tx = txfmt.load(src)
            md, _spans = txfmt.render_md(tx)
            _write_private(md_dir / name, json.dumps(tx, indent=1, ensure_ascii=False))
            _write_private(md_dir / (stem + ".md"), md)
        elif name.endswith(".md") and not (starter / (name[:-3] + ".tx.json")).exists():
            stem, md = name[:-3], src.read_text(encoding="utf-8")
            _write_private(md_dir / name, md)
        else:
            if name == "codes.json":
                _write_private(md_dir / name, src.read_text(encoding="utf-8"))
            continue
        annot = starter / (stem + ".annot.json")
        if annot.is_file():
            codings = _place_codings(json.loads(annot.read_text(encoding="utf-8")), md)
            _write_private(md_dir / (stem + ".annot.json"),
                           json.dumps(codings, indent=2, ensure_ascii=False))


def _slug_from(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
    return base or "project"


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

    def for_member(self, slug, email, handle=None):
        """The project if `email` belongs to it; None if not or no such project.

        Both failures take the same path, so neither the response nor its
        timing tells a non-member that the project exists. "sandbox" is
        always the signed-in person's own sandbox.
        """
        if slug == SANDBOX:
            return self.sandbox(email, handle)
        rec = self._all().get(slug) if isinstance(slug, str) else None
        if rec is None or email not in rec.get("members", ()):
            return None
        return self._project(slug, rec)

    def projects_for(self, email):
        """Their projects by name, then their sandbox. Owners also get the
        member list, to manage it."""
        projects = []
        for slug, rec in self._all().items():
            if email not in rec.get("members", ()):
                continue
            entry = {"slug": slug, "name": rec.get("name") or slug,
                     "owned": rec.get("owner") == email}
            if entry["owned"]:
                entry["members"] = rec.get("members", [])
            projects.append(entry)
        projects.sort(key=lambda p: p["name"].lower())
        projects.append({"slug": SANDBOX, "name": "Sandbox", "sandbox": True})
        return projects

    def owned_count(self, email):
        return sum(1 for rec in self._all().values() if rec.get("owner") == email)

    # --- sandboxes --------------------------------------------------------

    def sandbox(self, email, handle=None):
        """This person's sandbox, made from the starter files on first use."""
        directory = self.store.dir.resolve() / "sandboxes" / person_dir_name(email, handle)
        data, audio = directory / "transcripts", directory / "audio"
        if not data.is_dir():
            with lock_for(data):
                if not data.is_dir():
                    self._make_sandbox(directory)
        return Project(SANDBOX, "Sandbox", data, audio)

    def _make_sandbox(self, directory):
        # Built under a temporary name and renamed into place, so a failure
        # halfway never leaves a sandbox without its starter files.
        root = ensure_private_dir(directory.parent)
        building = Path(tempfile.mkdtemp(prefix=".new-", dir=root))
        try:
            for part in ("transcripts", "audio"):
                (building / part).mkdir(mode=STATE_DIR_MODE)
            try:
                populate_from_starter(building / "transcripts")
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"warning: sandbox starter files not copied: {e}",
                      file=sys.stderr, flush=True)
            os.rename(building, directory)
        except BaseException:
            shutil.rmtree(building, ignore_errors=True)
            raise

    def add(self, slug, name, data_dir, audio_dir, *, now, protected=(),
            forbidden_inside=(), roots=None):
        """Register existing folders."""
        return self._add(
            slug, name, data_dir, audio_dir, now=now, storage="registered",
            protected=(self.store.dir, *protected),
            forbidden_inside=(self.store.dir, *forbidden_inside), roots=roots)

    def create(self, slug, name, *, now, owner=None):
        """Allocate private storage for a new hosted project.

        With an `owner`, they are its first member and manage its members;
        a None slug is made from the name."""
        try:
            return self._add(slug, name, now=now, storage="managed", owner=owner)
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
             protected=(), forbidden_inside=(), roots=None, owner=None):
        if name is not None and not isinstance(name, str):
            raise ProjectError("Project name must be text")
        if slug is None and owner is not None:
            if not (name or "").strip():
                raise ProjectError("Give the project a name.")
        elif not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise ProjectError(
                "Project ID must be 1–40 characters, using lowercase letters, digits or "
                "hyphens, and start and end with a letter or digit.")
        elif slug in RESERVED_SLUGS:
            raise ProjectError(f"The project ID {slug} is reserved.")
        name = (name or "").strip() or slug
        if len(name) > 120:
            raise ProjectError("project name is too long")

        with self.store.locked(), ExitStack() as cleanup:
            doc = self.store.read(PROJECTS_FILE, EMPTY, fresh=True)
            projects = doc.setdefault("projects", {})
            if slug is None:
                base = _slug_from(name)
                slug, n = base, 1
                while slug in projects or slug in RESERVED_SLUGS:
                    n += 1
                    slug = f"{base}-{n}"
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
            if owner is not None:
                record.update(owner=owner, members=[owner])
            projects[slug] = record
            self.store.write(PROJECTS_FILE, doc)
            cleanup.pop_all()
            return dict(record, slug=slug)

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

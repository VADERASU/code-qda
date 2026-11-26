"""qda_store — private JSON state for hosted mode: atomic, locked, cached.

Accounts, sessions, invites and the project registry live in one directory
outside the repository (never git-addable), as small JSON documents. The
server and the admin CLI are separate processes that both write here, so
every change happens under an exclusive flock and re-reads from disk first:
an in-memory copy must never overwrite a change the other process made
(a CLI `disable` silently undone by a session refresh, say).

Stdlib only. fcntl is imported lazily so local mode works where it is absent.
"""
from __future__ import annotations

import copy
import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

import txfmt

STATE_FILE_MODE = 0o600
STATE_DIR_MODE = 0o700


class StateDirError(RuntimeError):
    """The state directory is missing, misplaced, or readable by others."""


def default_state_dir():
    if os.environ.get("QDA_STATE_DIR"):
        return Path(os.environ["QDA_STATE_DIR"]).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "code-qda"


def ensure_private_dir(path, create=True):
    """Create (0700) or verify a directory only this user can enter."""
    path = Path(path).expanduser()
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(mode=STATE_DIR_MODE)
    if not path.is_dir() or path.is_symlink():
        raise StateDirError(f"{path} is not a directory")
    st = path.stat()
    if st.st_uid != os.getuid():
        raise StateDirError(f"{path} is owned by another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise StateDirError(
            f"{path} is accessible to other users; run: chmod 700 {path}")
    for child in path.iterdir():
        cst = child.lstat()
        if stat.S_ISLNK(cst.st_mode):
            raise StateDirError(f"{child} is a symlink; state files must be regular files")
        if stat.S_IMODE(cst.st_mode) & 0o077:
            raise StateDirError(
                f"{child} is accessible to other users; run: chmod 600 {child}")
    return path


class Store:
    """A directory of JSON documents updated under one inter-process lock."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self._lock = threading.RLock()
        self._depth = 0
        self._lock_fd = None
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._audit_lock = threading.Lock()

    # --- reading ------------------------------------------------------------

    def read(self, name, default, fresh=False):
        """The document's current contents (a private copy), or `default`.

        Reads are cached on the file's identity; `fresh` bypasses the cache,
        which every read-modify-write does.
        """
        path = self.dir / name
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return copy.deepcopy(default)
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        with self._cache_lock:
            cached = self._cache.get(name)
        if fresh or cached is None or cached[0] != sig:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self._cache_lock:
                self._cache[name] = (sig, data)
        else:
            data = cached[1]
        return copy.deepcopy(data)

    # --- writing ------------------------------------------------------------

    @contextmanager
    def locked(self):
        """Exclusive across threads and processes. Re-entrant within a thread."""
        with self._lock:
            if self._depth == 0:
                import fcntl
                fd = os.open(self.dir / ".lock", os.O_RDWR | os.O_CREAT, STATE_FILE_MODE)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._lock_fd = fd
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    import fcntl
                    fd, self._lock_fd = self._lock_fd, None
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def write(self, name, data):
        """Replace a document. Call only while holding `locked()`."""
        text = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        txfmt.write_text_atomic(self.dir / name, text, mode=STATE_FILE_MODE)

    def update(self, name, default, fn):
        """Re-read under the lock, let `fn` mutate it, write it back.

        Returns whatever `fn` returns. If `fn` raises, nothing is written.
        """
        with self.locked():
            data = self.read(name, default, fresh=True)
            result = fn(data)
            self.write(name, data)
            return result

    # --- audit log ----------------------------------------------------------

    def append_jsonl(self, name, record):
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._audit_lock:
            fd = os.open(self.dir / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                         STATE_FILE_MODE)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

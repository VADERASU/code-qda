#!/usr/bin/env python3
"""code-qda — minimal qualitative data analysis for .md files

Two ways to run it:

    python3 app.py [PORT]            local: this computer only, no accounts
    python3 app.py --hosted ...      hosted: invite-only accounts, projects

Local mode serves the repository's md/ directory to http://localhost only.
Hosted mode is for putting the app behind Tailscale Funnel (see
docs/hosting.md): every request is authenticated, and each study's data is a
separate project that only its members can see. `python3 app.py admin ...`
manages accounts and projects.
"""
from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import http.server
import json
import math
import os
import re
import shutil
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import txfmt
from qda_hosted import HostedMixin
from qda_projects import SANDBOX, Project, local_project, normalize_starter_colors

ROOT = Path(__file__).resolve().parent
MD_DIR = ROOT / "md"
STATIC_DIR = ROOT / "static"
PAGES_DIR = ROOT / "pages"
# Interview audio is not in the repo and must not be — it is IRB-controlled.
# Point this at approved storage; the default is a gitignored local directory.
AUDIO_DIR = Path(os.environ.get("QDA_AUDIO_DIR", MD_DIR / "audio"))
STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}
AUDIO_CONTENT_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}

MAX_BODY = 4 * 1024 * 1024      # a whole file's annotations fit many times over
MAX_UPLOAD = 1024 ** 3          # one uploaded audio file
MAX_TRANSCRIPT_UPLOAD = 32 * 1024 * 1024
UPLOAD_MIN_RATE = 64 * 1024     # bytes/second an upload must average
DISK_RESERVE = 2 * 1024 ** 3    # uploads never leave less than this free
MAX_SEGMENTS = 100_000
MAX_CONNECTIONS = 64
REQUEST_TIMEOUT = 30            # seconds per socket operation, not per request
BODY_DEADLINE = 20              # seconds to receive a whole request body

# Inline style attributes are part of how coded spans are drawn, hence
# 'unsafe-inline' for styles only. No inline script anywhere.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self'; media-src 'self'; connect-src 'self'; font-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
COLOR_RE = re.compile(r"#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})")
LOCAL_HOST_RE = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?", re.I)
# A request carrying any of these came through a proxy. Local mode has no
# login, so it must never answer one: pointing Tailscale Funnel (or any
# reverse proxy) at it by mistake fails closed instead of publishing data.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                 "forwarded", "x-real-ip", "via")
MAX_NAME_LENGTH = 1000
MAX_TEXT_LENGTH = 1_000_000


def safe_name(name):
    """A bare file name from a request, or None. Never a path."""
    if not isinstance(name, str):
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if (not base or base.startswith(".") or len(base) > 255
            or any(ord(c) < 32 for c in base)):
        return None
    return base


def audio_available(project, tx):
    audio = tx.get("audio")
    name = safe_name(audio) if isinstance(audio, str) else None
    return bool(name and (project.audio_dir / name).is_file())


def transcript_name(project, name):
    """Resolve old transcript links without creating separate coding files."""
    for suffix in (".md", ".tx.json"):
        legacy = ".16k" + suffix
        if name.endswith(legacy) and not (project.md_dir / name).exists():
            canonical = name[:-len(legacy)] + suffix
            if (project.md_dir / canonical).exists():
                return canonical
    return name


def file_rev(path):
    """A version stamp for an annotation file: changes whenever it does."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]
    except FileNotFoundError:
        return "none"


def _clean_node(raw, index):
    """One code-tree node in its on-disk shape, or None if it is unusable."""
    if not isinstance(raw, dict):
        return None
    code_id, name = raw.get("id"), raw.get("name")
    if not isinstance(code_id, str) or not ID_RE.fullmatch(code_id):
        return None
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
        return None
    parent_id = raw.get("parentId")
    if not isinstance(parent_id, str) or not ID_RE.fullmatch(parent_id):
        parent_id = None
    color = raw.get("color")
    color = color if isinstance(color, str) and COLOR_RE.fullmatch(color) else None
    order = raw.get("order")
    order = order if isinstance(order, int) and not isinstance(order, bool) else index
    node = {"id": code_id, "name": name.strip(), "parentId": parent_id,
            "color": color, "order": order}
    if raw.get("manualOrder") is True:
        node["manualOrder"] = True
    return node


def sanitize_code_tree(code_tree):
    """-> {id: node}: valid nodes, no dangling parents or cycles, and only
    root codes carrying a color (children inherit theirs)."""
    nodes = {}
    for index, raw in enumerate(code_tree if isinstance(code_tree, list) else []):
        node = _clean_node(raw, index)
        if node is not None:
            nodes[node["id"]] = node

    for node in nodes.values():
        parent_id = node["parentId"]
        if parent_id == node["id"] or parent_id not in nodes:
            node["parentId"] = None

    for node in nodes.values():
        seen = set()
        current = node
        while current["parentId"]:
            parent_id = current["parentId"]
            if parent_id in seen:
                node["parentId"] = None
                break
            seen.add(parent_id)
            current = nodes.get(parent_id)
            if current is None:
                node["parentId"] = None
                break

    for node in nodes.values():
        if node["parentId"] is not None:
            node["color"] = None
    return nodes


def disk_tree(nodes):
    """The on-disk list for sanitized nodes: no color key on child codes."""
    out = []
    for node in nodes.values():
        node = dict(node)
        if node.get("color") is None:
            node.pop("color", None)
        out.append(node)
    return out


def _clean_tree(tree):
    nodes = {}
    for index, raw in enumerate(tree if isinstance(tree, list) else []):
        node = _clean_node(raw, index)
        if node is not None:
            nodes[node["id"]] = node
    return nodes


def merge_code_trees(server_tree, client_tree, base_tree=None, keep_ids=()):
    """Fold a client's code tree into the shared one, by code id.

    Clients always send their whole tree, and with several people editing, a
    tab opened an hour ago must neither erase codes created since nor undo
    renames, moves and deletions made since. So the client also sends
    `base_tree`, the tree as it last synced it: a code it sends unchanged from
    that base is not its edit, and the server's current version of it stands,
    including its absence (someone deleted it). Only codes the client changed
    or created are taken from the client.

    `keep_ids` are codes the client's own annotations use; those (and their
    ancestors) are never dropped, or the annotations would be orphaned.
    Without a base (older clients), the client's version of each code wins.
    Deleting a code has its own endpoint, so omission never means deletion.
    """
    merged = {}
    for raw in server_tree if isinstance(server_tree, list) else []:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            merged[raw["id"]] = raw
    client = _clean_tree(client_tree)
    base = _clean_tree(base_tree) if isinstance(base_tree, list) else None

    keep = set()
    for code_id in keep_ids:
        seen = set()
        while code_id in client and code_id not in seen:
            keep.add(code_id)
            seen.add(code_id)
            code_id = client[code_id]["parentId"]

    for code_id, node in client.items():
        untouched = base is not None and base.get(code_id) == node
        if untouched and not (code_id in keep and code_id not in merged):
            continue
        merged[code_id] = node
    return disk_tree(sanitize_code_tree(list(merged.values())))


def clean_annotations(items):
    """-> annotations in their on-disk shape, or None if any is malformed."""
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            return None
        ann_id, code_id = item.get("id"), item.get("codeId")
        start, end, text = item.get("start"), item.get("end"), item.get("text", "")
        if not (isinstance(ann_id, str) and ID_RE.fullmatch(ann_id)
                and isinstance(code_id, str) and ID_RE.fullmatch(code_id)):
            return None
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                   for v in (start, end)) or end < start:
            return None
        if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
            return None
        ann = {"id": ann_id, "codeId": code_id, "text": text, "start": start, "end": end}
        if item.get("stale") is True:
            ann["stale"] = True
        out.append(ann)
    return out


def now_stamp():
    """The time now, as the transcript edit log writes it."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_stamp(value):
    """An edit-log or coding timestamp as an aware datetime, or None.

    Stamps carry the offset of whichever machine wrote them, and the data has
    been coded from several time zones, so they are compared as times, never
    as strings."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def stamp_annotations(annotations, previous, author, now):
    """Record who applied each coding, and when.

    Only the server writes these: clients send whole annotation lists, so a
    stamp taken from the request could claim someone else's work or rewrite
    history. A coding keeps the stamp it was first saved with through moves,
    resizes and transcript remaps. Codings saved before stamps existed stay
    unstamped rather than being dated to whenever they are next saved.
    """
    before = {a["id"]: a for a in previous
              if isinstance(a, dict) and isinstance(a.get("id"), str)}
    for ann in annotations:
        old = before.get(ann["id"])
        if old is None:
            ann["ts"] = now
            if author:
                ann["by"] = author
            continue
        for key in ("ts", "by"):
            if isinstance(old.get(key), str) and old[key]:
                ann[key] = old[key]


def coding_progress(annotations, length, code_ids):
    """How far coding has got in one transcript.

    `through` is the share of the transcript above the end of the farthest
    coding. Coders work top to bottom and leave irrelevant talk uncoded, so
    coverage says little (finished interviews here are 13-42% covered) while
    the farthest coding's position tracks progress closely.
    """
    count = reach = 0
    for ann in annotations:
        if not isinstance(ann, dict) or not isinstance(ann.get("codeId"), str):
            continue
        start, end = ann.get("start"), ann.get("end")
        if (ann["codeId"] not in code_ids or not isinstance(start, int)
                or not isinstance(end, int) or not 0 <= start < end <= length):
            continue  # what the coding view would not draw either
        count += 1
        reach = max(reach, end)
    return {"count": count, "through": round(reach / length, 3) if length else 0}


def last_change(annotations, tx):
    """The latest recorded change to a transcript, or None.

    Records are coding stamps and the transcript's correction log. Removing a
    coding leaves no record, and neither did codings saved before stamping,
    so this can be older than the files' last write. File times are no
    substitute: copying the data in resets every one of them.
    """
    best = None
    records = [(a, "coding") for a in annotations]
    edits = tx.get("edits") if isinstance(tx, dict) else None
    records += [(e, "correction") for e in (edits if isinstance(edits, list) else [])]
    for record, kind in records:
        if not isinstance(record, dict):
            continue
        when = parse_stamp(record.get("ts"))
        if when is None or (best is not None and when <= best[0]):
            continue
        by = record.get("by")
        best = (when, {"ts": record["ts"], "by": by if isinstance(by, str) and by else None,
                       "kind": kind})
    return best[1] if best else None


def write_json(path, data, indent=2):
    txfmt.write_text_atomic(path, json.dumps(data, indent=indent, ensure_ascii=False))


@dataclass
class App:
    """What one server instance serves, and how strictly."""
    mode: str = "local"                 # "local" | "hosted"
    local: Project | None = None        # local mode's only project
    auth: object = None                 # qda_auth.Auth (hosted)
    registry: object = None             # qda_projects.Registry (hosted)
    public_origin: str | None = None    # e.g. https://host.tailnet.ts.net
    allowed_hosts: frozenset = frozenset()
    trust_forwarded: bool = False       # X-Forwarded-For is set by tailscaled
    max_body: int = MAX_BODY
    max_upload: int = MAX_UPLOAD
    state_dir: Path | None = None
    roots: dict | None = None           # allowed roots for registering existing storage
    transcriber: object = None          # qda_transcribe.Transcriber (hosted)


# (method, path under the project) -> (handler, writes?)
PROJECT_ROUTES = {
    ("GET", "/api/files"): ("_list_files", False),
    ("GET", "/api/file"): ("_get_file", False),
    ("GET", "/api/codes"): ("_get_codes", False),
    ("GET", "/api/codes-overview"): ("_get_codes_overview", False),
    ("GET", "/api/instances"): ("_get_instances", False),
    ("GET", "/api/tx"): ("_get_tx", False),
    ("GET", "/api/audio"): ("_get_audio", False),
    ("HEAD", "/api/audio"): ("_get_audio", False),
    ("POST", "/api/codes"): ("_save_codes", True),
    ("POST", "/api/prune-codes"): ("_prune_codes", True),
    ("POST", "/api/delete-code"): ("_delete_code_node", True),
    ("POST", "/api/add-code"): ("_add_code_node", True),
    ("POST", "/api/save-codes"): ("_save_shared_tree", True),
    ("POST", "/api/tx-edit"): ("_tx_edit", True),
    ("POST", "/api/upload"): ("_upload", "stream"),   # reads its own raw body
    ("GET", "/api/recordings"): ("_list_recordings", False),
    ("POST", "/api/transcribe"): ("_start_transcription", True),
    ("POST", "/api/transcribe/cancel"): ("_cancel_transcription", True),
}

_playback_cache = {}    # .tx.json path -> (signature, the audio file it names)


def playback_name(tx_path):
    """The audio file a .tx.json names, read once per version of the file."""
    try:
        st = os.stat(tx_path)
    except OSError:
        return None
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    cached = _playback_cache.get(str(tx_path))
    if cached and cached[0] == sig:
        return cached[1]
    try:
        audio = txfmt.load(tx_path).get("audio")
    except (OSError, ValueError, AttributeError):
        audio = None
    name = safe_name(audio) if isinstance(audio, str) else None
    _playback_cache[str(tx_path)] = (sig, name)
    return name

UPLOAD_TYPES = ("Upload a transcript (.txt, .md or .tx.json) or audio "
                f"({', '.join(sorted(AUDIO_CONTENT_TYPES))}).")


def upload_kind(name):
    """-> ("tx" | "text", stem) for a transcript, ("audio", None), or (None, None)."""
    lower = name.lower()
    if lower.endswith(".tx.json"):
        return "tx", name[:-len(".tx.json")]
    for ext in (".md", ".txt"):
        if lower.endswith(ext):
            return "text", name[:-len(ext)]
    if Path(lower).suffix in AUDIO_CONTENT_TYPES:
        return "audio", None
    return None, None


def looks_like_audio(suffix, head):
    """Does the file start the way this kind of audio file does?"""
    if suffix in (".opus", ".ogg"):
        return head[:4] == b"OggS"
    if suffix == ".m4a":
        return head[4:8] == b"ftyp"
    if suffix == ".mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if suffix == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    return False


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def tx_problem(tx):
    """Why an uploaded .tx.json can't be used, or None if it can."""
    if not isinstance(tx, dict) or tx.get("version") != 1:
        return "That isn't a version 1 .tx.json transcript."
    segments = tx.get("segments")
    if not isinstance(segments, list) or not segments or len(segments) > MAX_SEGMENTS:
        return "The transcript has no segments."
    speakers = tx.get("speakers", {})
    if not isinstance(speakers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in speakers.items()):
        return "\"speakers\" must map speaker ids to names."
    for key in ("title", "audio"):
        if tx.get(key) is not None and not isinstance(tx[key], str):
            return f"\"{key}\" must be text."
    if not isinstance(tx.get("edits", []), list):
        return "\"edits\" must be a list."
    seen = set()
    for n, seg in enumerate(segments, 1):
        if not isinstance(seg, dict):
            return f"Segment {n} isn't an object."
        seg_id = seg.get("id")
        if not isinstance(seg_id, str) or not ID_RE.fullmatch(seg_id) or seg_id in seen:
            return f"Segment {n} needs a unique id (letters, digits, - or _)."
        seen.add(seg_id)
        if not (_number(seg.get("start")) and _number(seg.get("end"))):
            return f"Segment {seg_id} needs start and end times in seconds."
        if not isinstance(seg.get("text", ""), str) or not isinstance(seg.get("speaker", ""), str):
            return f"Segment {seg_id}'s text and speaker must be text."
        words = seg.get("words", [])
        if not isinstance(words, list) or not all(isinstance(w, dict) for w in words):
            return f"Segment {seg_id}'s words must be a list of objects."
    return None


class Handler(HostedMixin, http.server.BaseHTTPRequestHandler):
    # Keep-alive. Audio seeking issues a burst of Range requests, and
    # HTTP/1.0 tears down the connection after each one. Every response
    # here sets Content-Length, so this is safe.
    protocol_version = "HTTP/1.1"
    server_version = "code-qda"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    # --- per-request state ------------------------------------------------
    #
    # One Handler serves every request on a keep-alive connection, and behind
    # Tailscale one backend connection carries requests from different people.
    # Anything describing "this request" is reset before each one is parsed.

    def _reset(self):
        self.user = None
        self.session_digest = None
        self.project = None
        self.body = None
        self._body_length = 0
        self._body_pending = False
        self._responded = False
        self._sent_headers = set()
        self._status_code = 200

    def setup(self):
        super().setup()
        self._reset()

    def parse_request(self):
        self._reset()
        return super().parse_request()

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            # Browsers disconnect mid-response (seeking audio) and leave idle
            # keep-alive connections behind; neither is an error.
            self.close_connection = True

    def version_string(self):
        return self.server_version  # not the Python version

    def address_string(self):
        ip = self.client_ip()
        if ip:
            return ip
        ca = self.client_address
        return ca[0] if isinstance(ca, tuple) and ca else "local"

    def client_ip(self):
        """The real client's address when a trusted proxy reported it."""
        if not self.server.app.trust_forwarded:
            return None
        headers = getattr(self, "headers", None)
        values = headers.get_all("X-Forwarded-For") if headers else None
        if not values:
            return None
        # The right-most entry is the one tailscaled appended; anything to
        # its left came from the client and proves nothing.
        last = values[-1].split(",")[-1].strip()
        return last[:64] or None

    # --- responses --------------------------------------------------------

    def send_header(self, keyword, value):
        self._sent_headers.add(keyword.lower())
        super().send_header(keyword, value)

    def send_response_only(self, code, message=None):
        self._status_code = int(code)
        super().send_response_only(code, message)

    def end_headers(self):
        if 100 <= getattr(self, "_status_code", 200) < 200:
            # An interim "100 Continue" (Expect: 100-continue): not the
            # response, so none of the response's headers and no bookkeeping.
            super().end_headers()
            self._sent_headers = set()
            return
        for name, value in SECURITY_HEADERS:
            if name.lower() not in self._sent_headers:
                self.send_header(name, value)
        if self.server.app.mode == "hosted" and "strict-transport-security" not in self._sent_headers:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if "cache-control" not in self._sent_headers:
            self.send_header("Cache-Control", "no-store")
        if self._body_pending and "connection" not in self._sent_headers:
            # The request body was never read, so the next bytes on this
            # connection are not a request. Close rather than misparse them.
            self.send_header("Connection", "close")
        self._responded = True
        super().end_headers()
        self._sent_headers = set()

    def _body_out(self, data):
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_file(self, path, content_type, cache="no-store"):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._status(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self._body_out(data)

    def _json(self, data, status=200, headers=()):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self._body_out(body)

    def _status(self, code, headers=(), close=False):
        # Content-Length is mandatory under keep-alive even for an empty body:
        # without it the client waits for a body that never arrives and the
        # connection hangs. A browser hits this on the first /favicon.ico.
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for name, value in headers:
            self.send_header(name, value)
        if close:
            self.send_header("Connection", "close")
        self.end_headers()

    def _redirect(self, code, location):
        self._status(code, headers=[("Location", location)])

    def log_request(self, *args):
        pass  # suppress per-request logs; errors still print

    # --- dispatch ---------------------------------------------------------

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        try:
            if not self._precheck():
                return
            split = urllib.parse.urlsplit(self.path)
            try:
                params = urllib.parse.parse_qs(split.query, max_num_fields=50)
            except ValueError:
                self._status(400)
                return
            if self.server.app.mode == "hosted":
                self._dispatch_hosted(split.path, params, split.query)
            else:
                self._dispatch_local(split.path, params)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True
        except Exception:
            # The traceback, not the request: query strings can carry names.
            print(f"error handling {self.command}:", file=sys.stderr, flush=True)
            traceback.print_exc()
            if self._responded:
                self.close_connection = True
            else:
                self._status(500, close=True)

    def _precheck(self):
        """Refuse malformed or misdirected requests before routing them."""
        app = self.server.app
        if not self.path.startswith("/") or self.path.startswith("//"):
            self._status(400, close=True)
            return False
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") is not None:
            self._body_pending = True
            self._status(411, close=True)
            return False
        if len(lengths) > 1 or (lengths and not re.fullmatch(r"\d{1,12}", lengths[0])):
            self._status(400, close=True)
            return False
        self._body_length = int(lengths[0]) if lengths else 0
        self._body_pending = self._body_length > 0
        if self.command == "POST" and not lengths:
            self._status(411, close=True)
            return False
        if self.command != "POST" and self._body_length:
            self._status(400)
            return False
        limit = app.max_body
        if self.command == "POST" and urllib.parse.urlsplit(self.path).path.endswith("/api/upload"):
            limit = app.max_upload   # still refused unread unless signed in and a member
        if self._body_length > limit:
            self._status(413)
            return False

        hosts = self.headers.get_all("Host") or []
        host = hosts[0].strip().lower() if len(hosts) == 1 else ""
        if app.mode == "hosted":
            if host not in app.allowed_hosts:
                self._status(421)
                return False
        else:
            if not LOCAL_HOST_RE.fullmatch(host):
                self._status(421)
                return False
            for name in self.headers.keys():
                lowered = name.lower()
                if lowered in PROXY_HEADERS or lowered.startswith("tailscale-"):
                    self._status(403)
                    return False
        return True

    def _origin_ok(self):
        """Is this state-changing request from our own pages?

        Browsers attach Origin to every POST; a page on another site cannot
        forge it. Requests without one (curl) are accepted locally, and in
        hosted mode only with the browser's own same-origin marker.
        """
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        app = self.server.app
        if app.mode == "hosted":
            if origin is not None:
                return origin == app.public_origin
            return fetch_site == "same-origin"
        if origin is not None:
            host = (self.headers.get("Host") or "").strip().lower()
            return origin.lower() == "http://" + host
        return fetch_site in (None, "same-origin", "none")

    def _read_json(self, limit):
        """The request's JSON object body, or None after sending the error."""
        if self._body_length > limit:
            self._status(413)
            return None
        if self.headers.get_content_type() != "application/json":
            self._status(415)
            return None
        raw = self._read_body_bytes(self._body_length)
        self._body_pending = False
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._json({"error": "Expected a JSON object."}, status=400)
            return None
        return data

    def _read_body_bytes(self, length, seconds=None):
        """Read exactly `length` body bytes within `seconds` (default
        BODY_DEADLINE) in total.

        The socket timeout bounds each read, not the whole body: a client
        trickling one byte at a time would otherwise hold a connection slot
        forever, and enough of them would lock everyone out.
        """
        chunks = []
        self._read_body_into(chunks.append, length,
                             BODY_DEADLINE if seconds is None else seconds)
        return b"".join(chunks)

    def _read_body_into(self, sink, length, seconds):
        """Pass the body to `sink` in chunks, as it arrives (see above)."""
        deadline = time.monotonic() + seconds
        remaining = length
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("request body too slow")
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(remaining, 64 * 1024))
                if not chunk:
                    raise ConnectionResetError("request body cut short")
                sink(chunk)
                remaining -= len(chunk)
        except BaseException:
            self.close_connection = True
            raise
        finally:
            self.connection.settimeout(self.timeout)
        self._body_pending = False

    def _write_body(self, limit):
        if not self._origin_ok():
            self._status(403)
            return None
        return self._read_json(limit)

    def _dispatch_local(self, path, params):
        if path in ("/", "/index.html") and self.command == "GET":
            self._serve_file(ROOT / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/") and self.command == "GET":
            self._try_serve_static(path)
        elif path == "/auth/me" and self.command == "GET":
            self._json({"mode": "local"})
        elif path.startswith("/api/"):
            self._project_route(self.server.app.local, path, params)
        else:
            self._status(404)

    def _project_route(self, project, path, params):
        route = PROJECT_ROUTES.get((self.command, path))
        if route is None:
            self._status(404)
            return
        method, writes = route
        self.project = project
        if "name" in params:
            name = safe_name(params["name"][0])
            if name is None:
                self._status(404)
                return
            params["name"] = [transcript_name(project, name)]
        handler = getattr(self, method)
        if not writes:
            handler(params)
            return
        if writes == "stream":
            if not self._origin_ok():
                self._status(403)
                return
            handler(params)
            self._audit_write(path, {"name": params.get("file", [None])})
            return
        body = self._write_body(self.server.app.max_body)
        if body is None:
            return
        self.body = body
        with project.lock:
            handler(params)
        self._audit_write(path, params)

    def _serve_index(self):
        self._serve_file(ROOT / "index.html", "text/html; charset=utf-8")

    def _serve_offline_worker(self):
        assets = sorted(p for p in STATIC_DIR.iterdir() if p.suffix in (".js", ".css"))
        shell = ROOT / "index.html"
        version = hashlib.sha256()
        for path in [shell, *assets]:
            version.update(path.name.encode())
            version.update(path.read_bytes())
        data = ("const CACHE = " + json.dumps("qda-shell-" + version.hexdigest()[:24])
                + ";\nconst ASSETS = "
                + json.dumps(["/offline-shell", *["/static/" + p.name for p in assets
                                                   if p.name != "offline-worker.js"]])
                + ";\n").encode() + (STATIC_DIR / "offline-worker.js").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._body_out(data)

    # --- project data -----------------------------------------------------

    def _name(self, params):
        return params.get("name", [""])[0]

    def _md_path(self, params):
        """The project's .md for ?name=, or None if there is no such file."""
        name = self._name(params)
        fp = self.project.md_dir / name
        return fp if name.endswith(".md") and fp.is_file() else None

    def _annot_path(self, md_path):
        return md_path.with_name(md_path.name[:-len(".md")] + ".annot.json")

    def _list_files(self, params):
        code_ids = sanitize_code_tree(self._load_shared_tree()).keys()
        files = []
        for f in sorted(self.project.md_dir.glob("*.md")):
            if f.name.startswith("."):
                continue
            tx_path = f.with_suffix(".tx.json")
            tx = None
            available = False
            if tx_path.exists():
                try:
                    tx = txfmt.load(tx_path)
                    available = audio_available(self.project, tx)
                except (OSError, ValueError, AttributeError):
                    pass  # Keep the file list usable if a transcript cannot be read.
            try:
                length = len(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                length = 0
            annotations = self._read_annotations(self._annot_path(f))
            files.append({
                "name": f.name,
                "tx": tx_path.exists(),
                "audioAvailable": available,
                "coding": coding_progress(annotations, length, code_ids),
                "lastChange": last_change(annotations, tx),
            })
        self._json(files)

    def _get_file(self, params):
        fp = self._md_path(params)
        if fp is None:
            self._status(404)
            return
        self._json({"content": fp.read_text(encoding="utf-8")})

    def _read_annotations(self, af):
        if not af.exists():
            return []
        try:
            data = json.loads(af.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _get_codes(self, params):
        name = self._name(params)
        if not name.endswith(".md"):
            self._status(404)
            return
        if not self.project.codes_file.exists():
            with self.project.lock:
                if not self.project.codes_file.exists():
                    self._migrate_to_shared_codes()
        fp = self.project.md_dir / name
        af = self._annot_path(fp)
        with self.project.lock:  # annotations and their rev must match
            result = {"version": 2, "codeTree": self._load_shared_tree(),
                      "annotations": self._read_annotations(af), "rev": file_rev(af)}
            if params.get("text") == ["1"]:
                try:
                    raw = fp.read_bytes()
                except FileNotFoundError:
                    self._status(404)
                    return
                result.update(content=raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"),
                              textRev=hashlib.sha256(raw).hexdigest()[:32],
                              owner=self.user["email"] if self.user else None)
        self._json(result)

    def _get_codes_overview(self, params):
        counts = {}  # codeId -> count
        nodes = {}
        if self.project.codes_file.exists():
            nodes = sanitize_code_tree(self._load_shared_tree())

        for af in sorted(self.project.md_dir.glob("*.annot.json")):
            for annotation in self._read_annotations(af):
                code_id = annotation.get("codeId") if isinstance(annotation, dict) else None
                if not isinstance(code_id, str) or code_id not in nodes:
                    continue
                counts[code_id] = counts.get(code_id, 0) + 1

        overview = [{"id": k, "count": v} for k, v in counts.items()]
        self._json({"codes": overview, "codeTree": list(nodes.values())})

    def _save_codes(self, params):
        """Save one transcript's annotations (and fold in its code tree).

        `rev` is the annotation file's version when the client loaded it. A
        conflicting save is refused (409). Offline saves also check the text
        version, and retries can confirm an earlier write.
        """
        fp = self._md_path(params)
        if fp is None:
            self._status(404)
            return
        data = self.body
        af = self._annot_path(fp)
        current = file_rev(af)
        rev = data.get("rev")
        offline = "textRev" in data
        if offline and (self.user is None or data.get("owner") != self.user["email"]):
            self._status(403)
            return
        if offline and data["textRev"] != file_rev(fp):
            self._json({"error": "The transcript text changed.", "rev": current}, status=409)
            return
        stale = rev is not None and rev != current
        if stale and not offline:
            self._json({"error": "These codes were changed by someone else.",
                        "rev": current}, status=409)
            return
        annotations = clean_annotations(data.get("annotations", []))
        if annotations is None:
            self._json({"error": "Malformed annotations."}, status=400)
            return
        previous = self._read_annotations(af)
        shared = self._load_shared_tree()
        merged = shared
        if "codeTree" in data:
            if not isinstance(data["codeTree"], list):
                self._json({"error": "Malformed code tree."}, status=400)
                return
            if offline:
                base = _clean_tree(data.get("codeTreeBase"))
                server = _clean_tree(shared)
                edited = _clean_tree(data["codeTree"])
                if any(node != base.get(key) and server.get(key) not in (node, base.get(key))
                       for key, node in edited.items()):
                    self._json({"error": "The same code was changed by someone else.",
                                "rev": current}, status=409)
                    return
            merged = merge_code_trees(shared, data["codeTree"], data.get("codeTreeBase"),
                                      keep_ids={a["codeId"] for a in annotations})
        if stale:
            # A retry whose acknowledgement was lost can confirm the earlier
            # write without touching the project again.
            if annotations == clean_annotations(previous) and merged == shared:
                self._json({"ok": True, "rev": current, "annotations": previous, "codeTree": shared})
            else:
                self._json({"error": "These codes were changed by someone else.",
                            "rev": current}, status=409)
            return
        if merged != shared:
            write_json(self.project.codes_file, merged)
        author = self.user.get("handle") if self.user is not None else None
        stamp_annotations(annotations, previous, author, now_stamp())
        text = json.dumps(annotations, indent=2, ensure_ascii=False)
        txfmt.write_text_atomic(af, text)
        result = {"ok": True, "rev": hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]}
        if offline:
            result.update(annotations=annotations, codeTree=merged)
        self._json(result)

    def _migrate_to_shared_codes(self):
        """One-time migration: merge codeTrees from all *.annot.json → codes.json."""
        all_nodes = {}
        for af in sorted(self.project.md_dir.glob("*.annot.json")):
            try:
                raw = json.loads(af.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or raw.get("version") != 2:
                continue
            for node in raw.get("codeTree", []):
                if isinstance(node, dict):
                    node_id = node.get("id")
                    if node_id and node_id not in all_nodes:
                        all_nodes[node_id] = node
        write_json(self.project.codes_file, list(all_nodes.values()))
        print(f"migrated {len(all_nodes)} codes to {self.project.codes_file.name}", flush=True)

    def _get_instances(self, params):
        code_id = params.get("codeId", [""])[0]
        if not code_id:
            self._status(400)
            return
        nodes = sanitize_code_tree(self._load_shared_tree())
        if code_id not in nodes:
            self._json([])
            return

        def subtree_ids(nid):
            ids = {nid}
            for node in nodes.values():
                if node.get("parentId") == nid:
                    ids |= subtree_ids(node["id"])
            return ids

        target_ids = subtree_ids(code_id)
        file_contents = {}
        instances = []
        for af in sorted(self.project.md_dir.glob("*.annot.json")):
            file_name = af.name.replace(".annot.json", ".md")
            for ann in self._read_annotations(af):
                if not isinstance(ann, dict) or ann.get("codeId") not in target_ids:
                    continue
                if file_name not in file_contents:
                    md_path = self.project.md_dir / file_name
                    try:
                        file_contents[file_name] = (
                            md_path.read_text(encoding="utf-8")
                            if md_path.exists()
                            else ""
                        )
                    except (OSError, ValueError):
                        file_contents[file_name] = ""
                content = file_contents[file_name]
                start = ann.get("start", 0)
                end = ann.get("end", start)
                if not isinstance(start, int) or not isinstance(end, int):
                    continue
                # extract surrounding paragraph (double-newline boundaries)
                para_start = content.rfind("\n\n", 0, start)
                para_start = para_start + 2 if para_start >= 0 else 0
                para_end = content.find("\n\n", end)
                para_end = para_end if para_end >= 0 else len(content)
                context = content[para_start:para_end].strip()
                # offsets of annotation within context (after strip)
                strip_offset = len(content[para_start:para_end]) - len(context.lstrip())
                ctx_start = max(0, start - para_start - strip_offset)
                ctx_end = max(ctx_start, end - para_start - strip_offset)
                instances.append(
                    {
                        "file": file_name,
                        "codeId": ann.get("codeId"),
                        "annotId": ann.get("id"),
                        "text": ann.get("text", ""),
                        "context": context,
                        "contextStart": ctx_start,
                        "contextEnd": ctx_end,
                    }
                )
        self._json(instances)

    # --- transcript verification ------------------------------------------
    #
    # The .tx.json is the source of truth for a transcript; the .md that codes
    # are applied to is a render of it. Editing a segment therefore rewrites
    # the .md, which shifts every character offset after the edit — and code
    # annotations *are* character offsets. So every edit re-renders and remaps
    # in one place, here, under the project's write lock. Nothing else may
    # write the .md.

    def _tx_path(self, name):
        stem = name
        for suffix in (".md", ".tx.json"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        fp = self.project.md_dir / (stem + ".tx.json")
        return fp if stem and fp.is_file() else None

    def _get_tx(self, params):
        fp = self._tx_path(self._name(params))
        if fp is None:
            self._status(404)
            return
        try:
            tx = txfmt.load(fp)
        except Exception as e:
            print(f"tx read error: {e}", flush=True)
            self._status(500)
            return
        _md, spans = txfmt.render_md(tx)
        for seg in tx.get("segments", []):
            seg["span"] = spans.get(seg["id"])
        tx["audioAvailable"] = audio_available(self.project, tx)
        self._json(tx)

    def _tx_edit(self, params):
        """Apply one correction: edit, re-render the .md, remap annotations."""
        fp = self._tx_path(self._name(params))
        if fp is None:
            self._status(404)
            return
        try:
            payload = self.body
            seg_id = payload.get("seg")
            if not isinstance(seg_id, str):
                self._status(400)
                return

            tx = txfmt.load(fp)
            seg = txfmt.find_segment(tx, seg_id)
            if seg is None:
                self._status(404)
                return
            old_md, old_spans = txfmt.render_md(tx)
            overwrite_token = payload.get("overwriteToken")
            overwritten = txfmt.check_markdown(fp, old_md, overwrite_token)

            # Signed in, the edit log records who: from the session, never the
            # request. Locally there are no accounts, so the client may say.
            if self.user is not None:
                author = self.user.get("handle") or ""
            else:
                author = str(payload.get("by") or "")
            method = str(payload.get("method") or "human")

            edit = None
            decision = payload.get('speakerDecision')
            if decision is not None:
                if (not isinstance(decision, dict) or
                        any(k in payload for k in ('text', 'speaker', 'newSpeaker', 'status'))):
                    self._json({'error': 'Invalid speaker decision'}, status=400)
                    return
                try:
                    edit = txfmt.review_speaker(tx, seg_id, decision.get('action'),
                                                decision, author=author)
                except ValueError as error:
                    self._json({'error': str(error)}, status=409)
                    return
            if isinstance(payload.get("text"), str):
                edit = txfmt.apply_edit(
                    tx, seg_id, payload["text"], author=author, method=method)

            # Reassigning a segment to a person diarization missed entirely:
            # mint them a key first, then attach the segment to it.
            speaker = payload.get("speaker")
            if isinstance(payload.get("newSpeaker"), str):
                speaker = txfmt.add_speaker(tx, payload["newSpeaker"])
            if isinstance(speaker, str):
                edit = txfmt.set_speaker(
                    tx, seg_id, speaker, author=author, method=method) or edit

            if isinstance(payload.get("status"), str):
                txfmt.mark_reviewed(tx, seg_id, payload["status"], author=author)
            elif edit is not None and (decision or {}).get('action') != 'keep':
                txfmt.mark_reviewed(tx, seg_id, "edited")

            new_md, new_spans = txfmt.render_md(tx)
            moved = stale = 0
            if new_md != old_md or overwritten:
                moved, stale = self._rewrite_md(
                    fp, old_md, new_md, old_spans, new_spans, {seg_id},
                    overwrite_token)
            txfmt.save(fp, tx)

            self._json({"ok": True, "edit": edit, "moved": moved, "stale": stale,
                        "segment": {"id": seg["id"], "text": seg["text"],
                                    "status": seg["status"],
                                    "speaker": seg.get("speaker"),
                                    "speakerSuggestion": seg.get('speaker_suggestion'),
                                    "flags": seg.get('flags', []),
                                    "wordsStale": bool(seg.get("words_stale"))},
                        "speakers": tx.get("speakers") or {},
                        "span": new_spans.get(seg_id),
                        # Every offset after the edit has moved, and the
                        # verification view hands one back to the coding view
                        # when it closes. Cheaper than re-fetching the tx.
                        "spans": new_spans if new_md != old_md or overwritten else None})
        except txfmt.MarkdownConflict as e:
            self._json({"error": str(e), "overwriteToken": e.overwrite_token}, status=409)
        except Exception as e:
            print(f"tx-edit error: {e}", flush=True)
            self._status(500)

    def _rewrite_md(self, tx_path, old_md, new_md, old_spans, new_spans, changed,
                    overwrite_token=None):
        """Write the re-rendered .md and move code annotations onto it."""
        overwritten = txfmt.check_markdown(tx_path, old_md, overwrite_token)
        stem = tx_path.name[: -len(".tx.json")]
        txfmt.write_text_atomic(self.project.md_dir / (stem + ".md"), new_md)

        af = self.project.md_dir / (stem + ".annot.json")
        if not af.exists():
            return 0, 0
        try:
            annotations = json.loads(af.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0, 0
        if not isinstance(annotations, list):
            return 0, 0
        if overwritten:
            # Their offsets refer to independently edited Markdown, so the
            # old transcript's segment spans cannot safely relocate them.
            for annotation in annotations:
                annotation["stale"] = True
            moved, stale = 0, len(annotations)
        else:
            annotations, moved, stale = txfmt.remap_annotations(
                annotations, old_spans, new_spans, old_md, new_md, changed)
        write_json(af, annotations)
        if stale:
            print(f"{stem}: {moved} annotations moved, {stale} marked stale "
                  f"(their text was edited)", flush=True)
        return moved, stale

    def _get_audio(self, params):
        """Serve interview audio with Range support.

        Without 206 responses a browser cannot seek: <audio> either refuses to
        scrub or re-downloads the whole file, and this UI is nothing but
        scrubbing. http.server does not implement Range, so it is done here.
        """
        # Basename only, so no request can escape the audio directory.
        # Deliberately not resolve()d: audio is normally a symlink into
        # approved storage, and resolving would point outside the directory
        # and reject every real setup.
        name = safe_name(params.get("name", [""])[0])
        if not name:
            self._status(404)
            return
        fp = self.project.audio_dir / name
        content_type = AUDIO_CONTENT_TYPES.get(fp.suffix.lower())
        if content_type is None or not fp.is_file():
            self._status(404)
            return

        size = fp.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range", "")
        partial = False
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip()) if header else None
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:  # suffix range: the last N bytes
                start = max(0, size - int(last))
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        # IRB audio: never leave a copy in a browser's disk cache.
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fp, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                block = fh.read(min(64 * 1024, remaining))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
                    # The browser seeked away mid-chunk, or paused and stopped
                    # reading; it reconnects with a new Range when it resumes.
                    self.close_connection = True
                    return
                remaining -= len(block)

    # --- uploads --------------------------------------------------------
    #
    # One file per request, its bytes as the body and its name in ?file=.
    # Nothing is ever replaced: an upload that would overwrite a transcript
    # (and so orphan its codings) or an audio file is refused with 409.

    def _upload_error(self, status, message):
        self._json({"error": message}, status=status)

    def _upload(self, params):
        name = safe_name(params.get("file", [""])[0])
        if name is None or len(name) > 200:
            self._upload_error(400, "Choose a file with a name.")
            return
        kind, stem = upload_kind(name)
        if kind is None:
            self._upload_error(415, UPLOAD_TYPES)
            return
        if not self._body_length:
            self._upload_error(400, f"{name} is empty.")
            return
        if kind == "audio":
            self._upload_audio(name)
        else:
            self._upload_transcript(name, kind, stem)

    def _upload_seconds(self):
        return BODY_DEADLINE + self._body_length / UPLOAD_MIN_RATE

    def _upload_transcript(self, name, kind, stem):
        if self._body_length > MAX_TRANSCRIPT_UPLOAD:
            self._upload_error(413, "Transcripts can be at most 32 MB.")
            return
        if not stem.strip() or stem != stem.strip():
            self._upload_error(400, "Give the file a name that doesn't start or end with a space.")
            return
        raw = self._read_body_bytes(self._body_length, self._upload_seconds())
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            self._upload_error(400, f"{name} isn't UTF-8 text.")
            return
        tx = None
        if kind == "tx":
            try:
                tx = json.loads(text)
            except ValueError:
                self._upload_error(400, f"{name} isn't valid JSON.")
                return
            problem = tx_problem(tx)
            if problem:
                self._upload_error(400, f"{name}: {problem}")
                return
            tx.pop("audioAvailable", None)       # added by /api/tx, not stored
            for seg in tx["segments"]:
                seg.pop("span", None)
            if not tx.get("title"):
                tx["title"] = stem
            md, _spans = txfmt.render_md(tx)
        else:
            md = text.replace("\r\n", "\n").replace("\r", "\n")
            if "\x00" in md or not md.strip():
                self._upload_error(400, f"{name} is empty or isn't text.")
                return
        if len(md) > MAX_TEXT_LENGTH:
            self._upload_error(413, f"{name} is too long for one transcript.")
            return
        md_dir = self.project.md_dir
        paths = [md_dir / (stem + suffix) for suffix in (".md", ".tx.json", ".annot.json")]
        with self.project.lock:
            if any(os.path.lexists(p) for p in paths):
                self._upload_error(409, f"There's already a transcript named {stem}.md. "
                                        f"Rename the file to upload it as well.")
                return
            if tx is not None:   # as txfmt.save writes it, but private
                txfmt.write_text_atomic(paths[1], json.dumps(tx, indent=1, ensure_ascii=False),
                                        mode=0o600)
            txfmt.write_text_atomic(paths[0], md, mode=0o600)
        self._json({"ok": True, "kind": "transcript", "name": stem + ".md"})

    def _upload_audio(self, name):
        audio_dir = self.project.audio_dir
        final = audio_dir / name
        taken = f"There's already an audio file named {name}."
        if os.path.lexists(final):
            self._upload_error(409, taken)
            return
        try:
            free = shutil.disk_usage(audio_dir).free
        except OSError:
            free = None
        if free is not None and free - self._body_length < DISK_RESERVE:
            self._upload_error(507, "The server is too low on disk space for this file.")
            return
        fd, tmp = tempfile.mkstemp(dir=audio_dir, prefix=".upload-", suffix=".part")
        head = bytearray()

        def sink(chunk):
            if len(head) < 16:
                head.extend(chunk[:16 - len(head)])
            fh.write(chunk)

        try:
            with os.fdopen(fd, "wb") as fh:
                try:
                    self._read_body_into(sink, self._body_length, self._upload_seconds())
                    fh.flush()
                    os.fsync(fh.fileno())
                except OSError as e:
                    if e.errno not in (errno.ENOSPC, errno.EDQUOT):
                        raise
                    self.close_connection = True
                    self._upload_error(507, "The server ran out of disk space.")
                    return
            if not looks_like_audio(final.suffix.lower(), bytes(head)):
                self._upload_error(400, f"{name} doesn't look like a {final.suffix} audio file.")
                return
            with self.project.lock:
                if os.path.lexists(final):
                    self._upload_error(409, taken)
                    return
                os.replace(tmp, final)
                tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
        self._json({"ok": True, "kind": "audio", "name": name})

    # --- transcription ---------------------------------------------------
    #
    # Recordings are the project's audio files that no transcript plays yet
    # (playback renders made by the pipeline are hidden), each with its
    # latest transcription job. The jobs run in qda_transcribe.

    def _job_view(self, job):
        view = {k: job.get(k) for k in ("id", "status", "stage", "error", "transcript",
                                        "speakers", "ahead", "secondOpinion")}
        view["by"] = job.get("handle")
        return view

    def _recordings(self):
        transcriber = self.server.app.transcriber
        playback = {playback_name(p) for p in self.project.md_dir.glob("*.tx.json")}
        jobs = transcriber.jobs_for(self.project.md_dir) if transcriber else {}
        recordings = []
        try:
            entries = sorted(self.project.audio_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            name = path.name
            if (name.startswith(".") or path.suffix.lower() not in AUDIO_CONTENT_TYPES
                    or not path.is_file() or (name in playback and name not in jobs)):
                continue
            job = jobs.get(name)
            recordings.append({"name": name, "size": path.stat().st_size,
                               "job": self._job_view(job) if job else None})
        if transcriber is None:
            status = {"available": False, "local": True, "problems": []}
        else:
            status = transcriber.status()
        return {"recordings": recordings, "transcribe": status}

    def _list_recordings(self, params):
        self._json(self._recordings())

    def _transcription_request(self, action):
        from qda_transcribe import NotFound, TranscribeError
        if self.server.app.transcriber is None:
            self._json({"error": "Transcription runs only on the hosted server. "
                                 "Here, use make prepare AUDIO=... in a terminal."}, status=400)
            return
        try:
            action(self.server.app.transcriber)
        except NotFound as e:
            self._json({"error": str(e)}, status=404)
            return
        except TranscribeError as e:
            self._json({"error": str(e)}, status=400)
            return
        self._json(self._recordings())

    def _start_transcription(self, params):
        audio = safe_name(self.body.get("audio")) if isinstance(self.body.get("audio"), str) else None
        user = self.user or {}

        def start(transcriber):
            from qda_transcribe import NotFound
            if audio is None:
                raise NotFound("Choose a recording.")
            job = transcriber.submit(self.project, audio, self.body.get("speakers"),
                                     user.get("email"), user.get("handle"))
            if self.server.app.auth is not None:
                self.server.app.auth.audit("transcription_queued", email=user.get("email"),
                                           project=self.project.slug, file=audio,
                                           speakers=job["speakers"], job=job["id"])
        self._transcription_request(start)

    def _cancel_transcription(self, params):
        self._transcription_request(
            lambda transcriber: transcriber.cancel(self.project, self.body.get("id")))

    def _load_shared_tree(self):
        if not self.project.codes_file.exists():
            return []
        try:
            data = json.loads(self.project.codes_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        if self.project.slug == SANDBOX:
            normalize_starter_colors(data)
        return data

    def _used_code_ids(self, nodes):
        """Every code used by an annotation, plus all of its ancestors."""
        used_ids = set()
        for af in sorted(self.project.md_dir.glob("*.annot.json")):
            for ann in self._read_annotations(af):
                code_id = ann.get("codeId") if isinstance(ann, dict) else None
                if not isinstance(code_id, str) or code_id not in nodes:
                    continue
                current_id = code_id
                seen = set()
                while current_id and current_id not in seen:
                    used_ids.add(current_id)
                    seen.add(current_id)
                    current_id = nodes.get(current_id, {}).get("parentId")
        return used_ids

    def _prune_codes(self, params):
        shared_tree = self._load_shared_tree()
        if not shared_tree:
            self._json({"pruned": 0, "codeTree": []})
            return
        nodes = sanitize_code_tree(shared_tree)
        used_ids = self._used_code_ids(nodes)
        new_tree = [
            n for n in shared_tree if isinstance(n, dict) and n.get("id") in used_ids
        ]
        pruned = len(shared_tree) - len(new_tree)
        write_json(self.project.codes_file, new_tree)
        self._json({"pruned": pruned, "codeTree": new_tree})

    def _delete_code_node(self, params):
        code_id = params.get("id", [""])[0]
        if not ID_RE.fullmatch(code_id):
            self._status(400)
            return
        shared_tree = self._load_shared_tree()
        nodes = sanitize_code_tree(shared_tree)
        if code_id not in nodes:
            self._json({"ok": False, "error": "code not found"})
            return

        def subtree_ids(nid):
            ids = {nid}
            for node in nodes.values():
                if node.get("parentId") == nid:
                    ids |= subtree_ids(node["id"])
            return ids

        to_delete = subtree_ids(code_id)
        for af in sorted(self.project.md_dir.glob("*.annot.json")):
            for ann in self._read_annotations(af):
                if isinstance(ann, dict) and ann.get("codeId") in to_delete:
                    stem = af.name[: -len(".annot.json")]
                    self._json({"ok": False, "error": f"still used in {stem}.md"})
                    return
        new_tree = [
            n
            for n in shared_tree
            if isinstance(n, dict) and n.get("id") not in to_delete
        ]
        write_json(self.project.codes_file, new_tree)
        self._json({"ok": True, "codeTree": new_tree})

    def _add_code_node(self, params):
        node = _clean_node(self.body, len(self._load_shared_tree()))
        if node is None:
            self._status(400)
            return
        shared_tree = self._load_shared_tree()
        if any(isinstance(n, dict) and n.get("id") == node["id"] for n in shared_tree):
            self._json({"ok": False, "error": "a code with this id exists"}, status=409)
            return
        if node["parentId"] is not None:
            node["color"] = None
        shared_tree.append(disk_tree({node["id"]: node})[0])
        write_json(self.project.codes_file, shared_tree)
        self._json({"ok": True, "codeTree": shared_tree})

    def _save_shared_tree(self, params):
        """Persist the shared code tree from the no-file overview view.

        Mirrors the file view, which already saves the whole codeTree; the
        client has computed the reparent/reorder. Merged by id and sanitized
        before writing, so neither a stale tab nor a bad payload can erase or
        corrupt codes.json.
        """
        tree = self.body.get("codeTree")
        if not isinstance(tree, list):
            self._status(400)
            return
        merged = merge_code_trees(self._load_shared_tree(), tree, self.body.get("codeTreeBase"))
        write_json(self.project.codes_file, merged)
        self._json({"ok": True, "codeTree": merged})

    def _try_serve_static(self, request_path):
        relative_path = request_path.removeprefix("/static/")
        asset_path = (STATIC_DIR / relative_path).resolve()

        try:
            asset_path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._status(404)
            return True

        content_type = STATIC_CONTENT_TYPES.get(asset_path.suffix)
        if content_type is None or not asset_path.is_file():
            self._status(404)
            return True

        self._serve_file(asset_path, content_type, cache="no-cache")
        return True


# --- servers -------------------------------------------------------------


class _ConnectionCap:
    """Refuse connections beyond a fixed number rather than spawn threads
    without bound."""

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class QDAServer(_ConnectionCap, http.server.ThreadingHTTPServer):
    def __init__(self, address, app, max_connections=MAX_CONNECTIONS):
        self.app = app
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)


class QDAUnixServer(_ConnectionCap, socketserver.ThreadingMixIn,
                    socketserver.UnixStreamServer):
    """Hosted mode behind tailscaled: a socket only this user (and root)
    can connect to, unlike a loopback port, which any local account can."""
    daemon_threads = True

    def __init__(self, path, app, max_connections=MAX_CONNECTIONS):
        self.app = app
        self._slots = threading.BoundedSemaphore(max_connections)
        # Tailscale opens several upstream connections at once for workspace assets.
        self.request_queue_size = max_connections
        self.socket_path = str(path)
        super().__init__(self.socket_path, Handler)

    def server_bind(self):
        path = self.socket_path
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)  # left behind by a previous run
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def _within(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def build_hosted_app(state_dir):
    """Load and check hosted-mode state. Raises SystemExit with the fix."""
    import qda_auth
    from qda_projects import Registry
    from qda_store import StateDirError, Store, ensure_private_dir

    state_dir = Path(state_dir).expanduser()
    if not state_dir.exists():
        raise SystemExit(f"No state directory at {state_dir}.\n"
                         f"Run: python3 app.py admin init --public-origin https://HOST")
    try:
        state_dir = ensure_private_dir(state_dir, create=False)
    except StateDirError as e:
        raise SystemExit(f"Refusing to start: {e}")
    if _within(state_dir, ROOT):
        raise SystemExit(f"Refusing to start: the state directory {state_dir} is inside "
                         f"the repository, where it could be committed.")
    store = Store(state_dir)
    config = store.read("config.json", {})
    origin = config.get("public_origin")
    parts = urllib.parse.urlsplit(origin) if isinstance(origin, str) else None
    if (parts is None or parts.scheme != "https" or not parts.hostname
            or parts.path not in ("", "/") or parts.query or parts.username):
        raise SystemExit("Refusing to start: config.json needs an https public_origin.\n"
                         "Run: python3 app.py admin init --public-origin https://HOST")
    origin = f"https://{parts.netloc.lower()}"
    hosts = {parts.netloc.lower()}
    if parts.port in (None, 443):
        hosts |= {parts.hostname.lower(), f"{parts.hostname.lower()}:443"}
    hosts |= {h.lower() for h in config.get("allowed_hosts", []) if isinstance(h, str)}
    if not hasattr(__import__("hashlib"), "scrypt"):
        raise SystemExit("Refusing to start: this Python has no hashlib.scrypt "
                         "(it needs an OpenSSL-backed build).")

    registry = Registry(store)
    for slug, rec in registry.records().items():
        for key in ("data_dir", "audio_dir"):
            if _within(state_dir, rec[key]):
                raise SystemExit(f"Refusing to start: project {slug}'s {key} contains "
                                 f"the state directory.")
            if not Path(rec[key]).is_dir():
                print(f"warning: project {slug}: {key} {rec[key]} does not exist",
                      file=sys.stderr)
    auth = qda_auth.Auth(store)
    from qda_transcribe import Transcriber
    transcriber = Transcriber(store, state_dir / "transcription")
    if not auth.has_users():
        print("No accounts yet. Create yours with:\n"
              "  python3 app.py admin invite you@example.edu --admin", flush=True)
    return App(mode="hosted", auth=auth, registry=registry, public_origin=origin,
               allowed_hosts=frozenset(hosts), state_dir=state_dir,
               roots={"data": config.get("data_roots", []),
                      "audio": config.get("audio_roots", [])},
               transcriber=transcriber)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["admin"]:
        import qda_admin
        return qda_admin.main(argv[1:])

    parser = argparse.ArgumentParser(
        description="code-qda — minimal QDA for .md files",
        epilog="Accounts and projects: python3 app.py admin --help")
    parser.add_argument("port", nargs="?", type=int, default=8000,
                        help="port to serve on (default: 8000)")
    parser.add_argument("-p", "--port", dest="port_opt", type=int, default=None,
                        help="port to serve on (alternative to the positional argument)")
    hosted = parser.add_argument_group("hosted mode (see docs/hosting.md)")
    hosted.add_argument("--hosted", action="store_true",
                        help="require sign-in and serve registered projects")
    hosted.add_argument("--socket", metavar="PATH",
                        help="listen on this Unix socket (recommended behind Tailscale)")
    hosted.add_argument("--state-dir", metavar="DIR",
                        help="accounts and projects (default: $QDA_STATE_DIR or "
                             "~/.local/share/code-qda)")
    args = parser.parse_args(argv)
    port = args.port_opt if args.port_opt is not None else args.port

    if not args.hosted:
        if args.socket or args.state_dir:
            parser.error("--socket and --state-dir only apply with --hosted")
        MD_DIR.mkdir(exist_ok=True)
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        app = App(mode="local", local=local_project(MD_DIR, AUDIO_DIR))
        server = QDAServer(("127.0.0.1", port), app)
        print(f"code-qda → http://localhost:{port}")
    else:
        from qda_store import default_state_dir
        app = build_hosted_app(args.state_dir or default_state_dir())
        if args.socket:
            app.trust_forwarded = True
            server = QDAUnixServer(args.socket, app)
            where = f"unix:{args.socket}"
        else:
            # Any account on this machine can reach a loopback port and claim
            # any X-Forwarded-For, so per-address rate limits are off here.
            server = QDAServer(("127.0.0.1", port), app)
            where = f"http://127.0.0.1:{port}"
        print(f"code-qda (hosted, {app.public_origin}) → {where}", flush=True)
        app.transcriber.start()
        problems = app.transcriber.check()
        if problems:
            print("Transcription is off until this server has " + " and ".join(problems) + ".",
                  flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()
        if app.transcriber is not None:
            app.transcriber.stop()


if __name__ == "__main__":
    sys.exit(main())

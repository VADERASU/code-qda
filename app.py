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
import hashlib
import http.server
import json
import os
import re
import socket
import socketserver
import stat
import sys
import threading
import traceback
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import txfmt
from qda_hosted import HostedMixin
from qda_projects import Project, local_project

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
MAX_CONNECTIONS = 64
REQUEST_TIMEOUT = 30            # seconds per socket operation, not per request

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


def merge_code_trees(server_tree, client_tree):
    """Fold a client's code tree into the shared one, by code id.

    The client's version of each code it sends wins; codes only the server
    has are kept. Clients always send their whole tree, and with several
    people editing, a tab opened an hour ago must not erase every code
    created since. Deleting a code has its own endpoint for that reason.
    """
    merged = {}
    for raw in server_tree if isinstance(server_tree, list) else []:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            merged[raw["id"]] = raw
    for index, raw in enumerate(client_tree):
        node = _clean_node(raw, index)
        if node is not None:
            merged[node["id"]] = node
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
    state_dir: Path | None = None
    roots: dict | None = None           # where the admin page may put projects


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
}


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

    def end_headers(self):
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
        if self._body_length > app.max_body:
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
        raw = self.rfile.read(self._body_length) if self._body_length else b""
        if len(raw) != self._body_length:
            self.close_connection = True
            raise ConnectionResetError("request body cut short")
        self._body_pending = False
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._json({"error": "Expected a JSON object."}, status=400)
            return None
        return data

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
        body = self._write_body(self.server.app.max_body)
        if body is None:
            return
        self.body = body
        with project.lock:
            handler(params)
        self._audit_write(path, params)

    def _serve_index(self):
        self._serve_file(ROOT / "index.html", "text/html; charset=utf-8")

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
        files = []
        for f in sorted(self.project.md_dir.glob("*.md")):
            if f.name.startswith("."):
                continue
            tx_path = f.with_suffix(".tx.json")
            available = False
            if tx_path.exists():
                try:
                    available = audio_available(self.project, txfmt.load(tx_path))
                except (OSError, ValueError, AttributeError):
                    pass  # Keep the file list usable if a transcript cannot be read.
            files.append({
                "name": f.name,
                "tx": tx_path.exists(),
                "audioAvailable": available,
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
        af = self._annot_path(self.project.md_dir / name)
        with self.project.lock:  # annotations and their rev must match
            annotations = self._read_annotations(af)
            rev = file_rev(af)
        self._json({"version": 2, "codeTree": self._load_shared_tree(),
                    "annotations": annotations, "rev": rev})

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

        `rev` is the annotation file's version when the client loaded it. If
        someone else saved since, the save is refused (409) instead of
        silently replacing their work; the client reloads and the user redoes
        their last change.
        """
        fp = self._md_path(params)
        if fp is None:
            self._status(404)
            return
        data = self.body
        af = self._annot_path(fp)
        current = file_rev(af)
        rev = data.get("rev")
        if rev is not None and rev != current:
            self._json({"error": "These codes were changed by someone else.",
                        "rev": current}, status=409)
            return
        annotations = clean_annotations(data.get("annotations", []))
        if annotations is None:
            self._json({"error": "Malformed annotations."}, status=400)
            return
        if "codeTree" in data:
            if not isinstance(data["codeTree"], list):
                self._json({"error": "Malformed code tree."}, status=400)
                return
            shared = self._load_shared_tree()
            merged = merge_code_trees(shared, data["codeTree"])
            if merged != shared:
                write_json(self.project.codes_file, merged)
        text = json.dumps(annotations, indent=2, ensure_ascii=False)
        txfmt.write_text_atomic(af, text)
        self._json({"ok": True,
                    "rev": hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]})

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

    def _load_shared_tree(self):
        if not self.project.codes_file.exists():
            return []
        try:
            data = json.loads(self.project.codes_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

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
        merged = merge_code_trees(self._load_shared_tree(), tree)
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
    if not auth.has_users():
        print("No accounts yet. Create yours with:\n"
              "  python3 app.py admin invite you@example.edu --admin", flush=True)
    return App(mode="hosted", auth=auth, registry=registry, public_origin=origin,
               allowed_hosts=frozenset(hosts), state_dir=state_dir,
               roots={"data": config.get("data_roots", []),
                      "audio": config.get("audio_roots", [])})


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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())

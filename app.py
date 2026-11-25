#!/usr/bin/env python3
"""code-qda — minimal qualitative data analysis for .md files"""
import argparse
import http.server
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import txfmt

ROOT = Path(__file__).parent
MD_DIR = ROOT / "md"
STATIC_DIR = ROOT / "static"
CODES_FILE = MD_DIR / "codes.json"  # shared code tree across all files
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


def audio_available(tx):
    audio = tx.get("audio")
    return bool(isinstance(audio, str) and audio
                and (AUDIO_DIR / Path(audio).name).is_file())


def transcript_name(name):
    """Resolve old transcript links without creating separate coding files."""
    name = Path(name).name
    for suffix in (".md", ".tx.json"):
        legacy = ".16k" + suffix
        if name.endswith(legacy) and not (MD_DIR / name).exists():
            canonical = name[:-len(legacy)] + suffix
            if (MD_DIR / canonical).exists():
                return canonical
    return name


class Handler(http.server.BaseHTTPRequestHandler):
    # Keep-alive. Audio seeking issues a burst of Range requests, and
    # HTTP/1.0 tears down the connection after each one. Every response
    # here sets Content-Length, so this is safe.
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # Browsers can disconnect during a response or while a keep-alive
            # connection waits for its next request.
            self.close_connection = True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, params = parsed.path, urllib.parse.parse_qs(parsed.query)
        if "name" in params:
            params["name"] = [transcript_name(params["name"][0])]

        if path in ("/", "/index.html"):
            self._serve_file(ROOT / "index.html", "text/html; charset=utf-8")
        elif self._try_serve_static(path):
            return
        elif path == "/api/files":
            files = []
            for f in sorted(MD_DIR.glob("*.md")):
                tx_path = f.with_suffix(".tx.json")
                available = False
                if tx_path.exists():
                    try:
                        available = audio_available(txfmt.load(tx_path))
                    except (OSError, ValueError, AttributeError):
                        pass  # Keep the file list usable if a transcript cannot be read.
                files.append({
                    "name": f.name,
                    "tx": tx_path.exists(),
                    "audioAvailable": available,
                })
            self._json(files)
        elif path == "/api/file":
            self._get_file(params)
        elif path == "/api/codes":
            self._get_codes(params)
        elif path == "/api/codes-overview":
            self._get_codes_overview()
        elif path == "/api/instances":
            self._get_instances(params)
        elif path == "/api/tx":
            self._get_tx(params)
        elif path == "/api/audio":
            self._get_audio(params)
        else:
            self._status(404)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/audio":
            self._get_audio(urllib.parse.parse_qs(parsed.query))
        else:
            self._status(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, params = parsed.path, urllib.parse.parse_qs(parsed.query)
        if "name" in params:
            params["name"] = [transcript_name(params["name"][0])]

        if path == "/api/codes":
            self._save_codes(params)
        elif path == "/api/prune-codes":
            self._prune_codes()
        elif path == "/api/delete-code":
            self._delete_code_node(params)
        elif path == "/api/add-code":
            self._add_code_node()
        elif path == "/api/save-codes":
            self._save_shared_tree()
        elif path == "/api/tx-edit":
            self._tx_edit(params)
        else:
            self._status(404)

    def _get_file(self, params):
        name = params.get("name", [""])[0]
        fp = MD_DIR / Path(name).name  # prevent path traversal
        if fp.suffix == ".md" and fp.exists():
            self._json({"content": fp.read_text(encoding="utf-8")})
        else:
            self._status(404)

    def _get_codes(self, params):
        name = params.get("name", [""])[0]
        fp = MD_DIR / Path(name).name
        if fp.suffix != ".md":
            self._status(404)
            return
        if not CODES_FILE.exists():
            self._migrate_to_shared_codes()
        shared_tree = self._load_shared_tree()
        af = MD_DIR / (Path(name).stem + ".annot.json")
        annotations = []
        if af.exists():
            try:
                data = json.loads(af.read_text(encoding="utf-8"))
                annotations = data if isinstance(data, list) else []
            except Exception:
                pass
        self._json({"version": 2, "codeTree": shared_tree, "annotations": annotations})

    def _get_codes_overview(self):
        counts = {}  # codeId -> count

        nodes = {}
        if CODES_FILE.exists():
            nodes = self._sanitize_code_tree(self._load_shared_tree())

        for af in sorted(MD_DIR.glob("*.annot.json")):
            try:
                data = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for annotation in data:
                code_id = annotation.get("codeId")
                if not isinstance(code_id, str) or code_id not in nodes:
                    continue
                counts[code_id] = counts.get(code_id, 0) + 1

        overview = [{"id": k, "count": v} for k, v in counts.items()]
        code_tree = list(nodes.values())
        self._json({"codes": overview, "codeTree": code_tree})

    def _save_codes(self, params):
        try:
            name = params.get("name", [""])[0]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            if "codeTree" in data:
                CODES_FILE.write_text(
                    json.dumps(data["codeTree"], indent=2, ensure_ascii=False)
                )
            af = MD_DIR / (Path(name).stem + ".annot.json")
            af.write_text(
                json.dumps(data.get("annotations", []), indent=2, ensure_ascii=False)
            )
            self._json({"ok": True})
        except Exception as e:
            print(f"save error: {e}", flush=True)
            self._status(500)

    def _migrate_to_shared_codes(self):
        """One-time migration: merge codeTrees from all *.annot.json → codes.json."""
        all_nodes = {}
        for af in sorted(MD_DIR.glob("*.annot.json")):
            try:
                raw = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("version") != 2:
                continue
            for node in raw.get("codeTree", []):
                if isinstance(node, dict):
                    node_id = node.get("id")
                    if node_id and node_id not in all_nodes:
                        all_nodes[node_id] = node
        CODES_FILE.write_text(
            json.dumps(list(all_nodes.values()), indent=2, ensure_ascii=False)
        )
        print(f"migrated {len(all_nodes)} codes to {CODES_FILE.name}", flush=True)

    def _get_instances(self, params):
        code_id = params.get("codeId", [""])[0]
        if not code_id:
            self._status(400)
            return
        nodes = self._sanitize_code_tree(self._load_shared_tree())
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
        for af in sorted(MD_DIR.glob("*.annot.json")):
            try:
                data = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            file_name = af.name.replace(".annot.json", ".md")
            for ann in data:
                if ann.get("codeId") not in target_ids:
                    continue
                if file_name not in file_contents:
                    md_path = MD_DIR / file_name
                    try:
                        file_contents[file_name] = (
                            md_path.read_text(encoding="utf-8")
                            if md_path.exists()
                            else ""
                        )
                    except Exception:
                        file_contents[file_name] = ""
                content = file_contents[file_name]
                start = ann.get("start", 0)
                end = ann.get("end", start)
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
    # in one place, here, atomically. Nothing else may write the .md.

    def _tx_path(self, name):
        stem = Path(name).name
        for suffix in (".md", ".tx.json"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        fp = MD_DIR / (stem + ".tx.json")
        return fp if fp.exists() else None

    def _get_tx(self, params):
        fp = self._tx_path(params.get("name", [""])[0])
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
        tx["audioAvailable"] = audio_available(tx)
        self._json(tx)

    def _tx_edit(self, params):
        """Apply one correction: edit, re-render the .md, remap annotations."""
        fp = self._tx_path(params.get("name", [""])[0])
        if fp is None:
            self._status(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
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
        (MD_DIR / (stem + ".md")).write_text(new_md, encoding="utf-8")

        af = MD_DIR / (stem + ".annot.json")
        if not af.exists():
            return 0, 0
        try:
            annotations = json.loads(af.read_text(encoding="utf-8"))
        except Exception:
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
        af.write_text(json.dumps(annotations, indent=2, ensure_ascii=False),
                      encoding="utf-8")
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
        # Basename only, so no request can escape AUDIO_DIR. Deliberately not
        # resolve()d: audio is normally a symlink into approved storage, and
        # resolving would point outside AUDIO_DIR and reject every real setup.
        name = Path(params.get("name", [""])[0]).name
        if not name or name.startswith("."):
            self._status(404)
            return
        fp = AUDIO_DIR / name
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
            if start >= size:
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
                except (BrokenPipeError, ConnectionResetError):
                    return  # the browser seeked away mid-chunk; normal
                remaining -= len(block)

    def _load_shared_tree(self):
        if not CODES_FILE.exists():
            return []
        try:
            data = json.loads(CODES_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _prune_codes(self):
        shared_tree = self._load_shared_tree()
        if not shared_tree:
            self._json({"pruned": 0, "codeTree": []})
            return
        nodes = self._sanitize_code_tree(shared_tree)
        used_ids = set()
        for af in sorted(MD_DIR.glob("*.annot.json")):
            try:
                data = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for ann in data:
                code_id = ann.get("codeId")
                if not isinstance(code_id, str) or code_id not in nodes:
                    continue
                current_id = code_id
                seen = set()
                while current_id and current_id not in seen:
                    used_ids.add(current_id)
                    seen.add(current_id)
                    current_id = nodes.get(current_id, {}).get("parentId")
        new_tree = [
            n for n in shared_tree if isinstance(n, dict) and n.get("id") in used_ids
        ]
        pruned = len(shared_tree) - len(new_tree)
        CODES_FILE.write_text(json.dumps(new_tree, indent=2, ensure_ascii=False))
        self._json({"pruned": pruned, "codeTree": new_tree})

    def _delete_code_node(self, params):
        code_id = params.get("id", [""])[0]
        if not code_id:
            self._status(400)
            return
        shared_tree = self._load_shared_tree()
        nodes = self._sanitize_code_tree(shared_tree)
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
        for af in sorted(MD_DIR.glob("*.annot.json")):
            try:
                data = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for ann in data:
                if ann.get("codeId") in to_delete:
                    stem = af.stem.replace(".annot", "")
                    self._json({"ok": False, "error": f"still used in {stem}.md"})
                    return
        new_tree = [
            n
            for n in shared_tree
            if isinstance(n, dict) and n.get("id") not in to_delete
        ]
        CODES_FILE.write_text(json.dumps(new_tree, indent=2, ensure_ascii=False))
        self._json({"ok": True, "codeTree": new_tree})

    def _add_code_node(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            node = json.loads(body)
            if not isinstance(node, dict) or not node.get("id") or not node.get("name"):
                self._status(400)
                return
            shared_tree = self._load_shared_tree()
            shared_tree.append(node)
            CODES_FILE.write_text(json.dumps(shared_tree, indent=2, ensure_ascii=False))
            self._json({"ok": True, "codeTree": shared_tree})
        except Exception as e:
            print(f"add-code error: {e}", flush=True)
            self._status(500)

    def _save_shared_tree(self):
        """Persist the full shared code tree (used by the no-file overview view).

        Mirrors the file view, which already saves the whole codeTree; the client
        has computed the reparent/reorder. Sanitized before writing so a bad
        payload can't corrupt codes.json.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            tree = data.get("codeTree") if isinstance(data, dict) else None
            if not isinstance(tree, list):
                self._status(400)
                return
            sanitized = list(self._sanitize_code_tree(tree).values())
            for node in sanitized:  # keep on-disk shape: only roots carry a color
                if node.get("color") is None:
                    node.pop("color", None)
            CODES_FILE.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False))
            self._json({"ok": True, "codeTree": sanitized})
        except Exception as e:
            print(f"save-codes error: {e}", flush=True)
            self._status(500)

    def _sanitize_code_tree(self, code_tree):
        nodes = {}

        for index, raw in enumerate(code_tree):
            if not isinstance(raw, dict):
                continue
            code_id = raw.get("id")
            name = raw.get("name")
            if not isinstance(code_id, str) or not code_id.strip():
                continue
            if not isinstance(name, str) or not name.strip():
                continue

            parent_id = raw.get("parentId")
            if not isinstance(parent_id, str) or not parent_id.strip():
                parent_id = None

            color = (
                raw.get("color")
                if isinstance(raw.get("color"), str) and raw.get("color")
                else None
            )
            order = raw.get("order") if isinstance(raw.get("order"), int) else index
            nodes[code_id] = {
                "id": code_id,
                "name": name.strip(),
                "parentId": parent_id,
                "color": color,
                "order": order,
            }
            if raw.get("manualOrder") is True:
                nodes[code_id]["manualOrder"] = True

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

    def _try_serve_static(self, request_path):
        if not request_path.startswith("/static/"):
            return False

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

        self._serve_file(asset_path, content_type)
        return True

    def _serve_file(self, path, content_type):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._status(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _status(self, code):
        # Content-Length is mandatory under keep-alive even for an empty body:
        # without it the client waits for a body that never arrives and the
        # connection hangs. A browser hits this on the first /favicon.ico.
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_request(self, *args):
        pass  # suppress per-request logs; errors still print


if __name__ == "__main__":
    MD_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="code-qda — minimal QDA for .md files")
    parser.add_argument("port", nargs="?", type=int, default=8000,
                        help="port to serve on (default: 8000)")
    parser.add_argument("-p", "--port", dest="port_opt", type=int, default=None,
                        help="port to serve on (alternative to the positional argument)")
    args = parser.parse_args()
    port = args.port_opt if args.port_opt is not None else args.port
    server = http.server.ThreadingHTTPServer(("", port), Handler)
    print(f"code-qda → http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
        server.server_close()

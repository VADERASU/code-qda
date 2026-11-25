#!/usr/bin/env python3
"""
build_tx.py — WhisperX JSON (+ optional second opinion) -> .tx.json and .md

The .tx.json is the source of truth: segments with word timings, an
append-only edit log, and a ranked verification queue. The .md is a render of
it that code-qda codes against, and is regenerated on every edit.

Usage:
  python pipeline/build_tx.py data/raw/ip1feb102026.16k.json
  python pipeline/build_tx.py data/raw/*.16k.json --alt-suffix .parakeet.json
  python pipeline/build_tx.py data/raw/ip1*.json --install md/       # + copy

Speaker names come from <name>.speakers.json, as before; a stub is written on
first run for you to fill in after a 30-second listen.
"""

import argparse
import difflib
import hashlib
import json
import pathlib
import shutil
import statistics
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import txfmt  # noqa: E402
from speaker_evidence import refresh_suggestions
from artifacts import find_transcript, transcript_stem, transcript_stems


def reuse_transcript(src, install=None):
    """Reuse the shared source of truth before even reading local ASR output."""
    directories = ([pathlib.Path(install)] if install else []) + [src.parent]
    existing = find_transcript(src.stem, *directories)
    if existing is None:
        # A Markdown-only transcript may already have offset-based coding.
        for directory in directories:
            for candidate in transcript_stems(src.stem):
                for suffix in (".md", ".annot.json"):
                    path = directory / (candidate + suffix)
                    if path.exists():
                        raise FileExistsError(
                            f"refusing to rebuild over {path} without its .tx.json; "
                            "restore the transcript from git first")
        return False

    # Validate before copying or rendering; corrupt shared data must never
    # become an excuse to silently replace it with fresh ASR output.
    tx = txfmt.load(existing)
    md_name = existing.name.removesuffix(".tx.json") + ".md"
    destination = pathlib.Path(install) if install else existing.parent
    destination.mkdir(parents=True, exist_ok=True)
    target_tx = destination / existing.name
    target_md = destination / md_name
    source_md = existing.parent / md_name
    if target_tx.resolve() != existing.resolve():
        if target_md.exists() or target_md.with_suffix(".annot.json").exists():
            raise FileExistsError(f"refusing to install over {target_md} without its .tx.json")
        shutil.copy2(existing, target_tx)
    if not target_md.exists():
        if source_md.exists():
            shutil.copy2(source_md, target_md)
        else:
            md, _ = txfmt.render_md(tx)
            target_md.write_text(md, encoding="utf-8")
    print(f"  reusing {target_tx}")
    return True


def sha256(path, limit=64 << 20):
    """Hash the first 64 MB — enough to identify a recording, fast on 150 MB
    WAVs. Recorded so a finding can be traced to a specific file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout
        return round(float(out.strip()), 3)
    except Exception:
        return None


def load_alt_words(path):
    """Second-opinion ASR output -> [(t0, t1, word)] sorted by time."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    words = data.get("words")
    if words is None:  # tolerate a WhisperX-shaped file as the alt too
        words = [w for s in data.get("segments", []) for w in s.get("words", [])]
    out = []
    for w in words:
        text = w.get("w") or w.get("word") or w.get("text")
        if not text:
            continue
        try:
            out.append((float(w.get("t0", w.get("start", 0))),
                        float(w.get("t1", w.get("end", 0))),
                        text.strip()))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


# `alt` used to be sliced by time, with a 3s pad on each side to absorb drift
# between the two models. That pad is wider than most segments here (median
# duration ~2s), so the line under a segment routinely showed its neighbours'
# words — and where the models genuinely disagreed, the pad was still not
# enough and it showed the wrong stretch anyway. The alignment below already
# knows which of the other model's words correspond to this segment's, so the
# alt is taken from that correspondence instead. No window, no pad.


def align_segments(segments, alt_words):
    """Align the two models' full word streams, then attribute misses to segments.

    Done once over the whole file rather than per segment, and on word ORDER
    rather than timestamps. A time-windowed comparison mistakes a local lag —
    which is exactly what a real disagreement causes downstream — for more
    disagreement, so one genuine error flags the correct segments after it too.

    Writes seg["align"] = {n, miss, words}: how many words the segment has, how
    many the second model did not produce, and which content words those were.
    """
    stream, owner = [], []
    for seg in segments:
        for word in txfmt._norm_words(seg["text"]):
            stream.append(word)
            owner.append(seg["id"])

    # Keep each normalised token's origin, so an aligned token can be traced
    # back to the alt word it came from and thus to that word's timestamp.
    other, source = [], []
    for i, (_t0, _t1, word) in enumerate(alt_words):
        for token in txfmt._norm_words(word):
            other.append(token)
            source.append(i)

    matched = [False] * len(stream)
    # A monotone map from each of our words to a position in the other stream.
    # Equal blocks map one-to-one; replaced blocks are spread proportionally,
    # so a word neither model agrees on still lands in the right neighbourhood
    # rather than being dropped out of the span.
    lo = [None] * len(stream)
    hi = [None] * len(stream)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=stream, b=other, autojunk=False).get_opcodes():
        span = i2 - i1
        for k in range(span):
            if tag == "equal":
                matched[i1 + k] = True
                lo[i1 + k] = hi[i1 + k] = j1 + k
            else:
                a = j1 + (j2 - j1) * k // span
                b = j1 + (j2 - j1) * (k + 1) // span
                lo[i1 + k] = a
                hi[i1 + k] = max(a, b - 1)

    counts = {seg["id"]: {"n": 0, "miss": 0, "words": []} for seg in segments}
    reach = {}
    for idx, (ok, seg_id, word) in enumerate(zip(matched, owner, stream)):
        entry = counts[seg_id]
        entry["n"] += 1
        if not ok:
            entry["miss"] += 1
            if len(word) >= 5 and word not in txfmt.STOPWORDS:
                entry["words"].append(word)
        if lo[idx] is not None:
            here = reach.setdefault(seg_id, [lo[idx], hi[idx]])
            here[0] = min(here[0], lo[idx])
            here[1] = max(here[1], hi[idx])

    for seg in segments:
        seg["align"] = counts[seg["id"]]
        bounds = reach.get(seg["id"])
        if not bounds or not other:
            seg["alt"] = ""
            continue
        a = source[max(0, min(bounds[0], len(source) - 1))]
        b = source[max(0, min(bounds[1], len(source) - 1))]
        picked = alt_words[a:b + 1]
        seg["alt"] = " ".join(w for _t0, _t1, w in picked)


def timing_offsets(segments, alt_words):
    """Per segment: where our word times and the other model's part company.

    Computed over *word* streams rather than the text-token stream used above,
    because only words carry timestamps, and over matched pairs only — an
    interpolated position for a word the two models never agreed on says
    nothing about whether our clock is right.

    Two shapes of failure, and one field for both. A short segment can be
    displaced whole, which the median catches. A long one more often drifts
    part-way through: measured on ip1, s00273 runs dead-on for 21 words and
    then steps to +0.99s for its last five and stays there — the median over
    that segment is 0.009s, so a per-segment statistic cannot see it at all.
    A sustained run is what distinguishes real drift from the single-word
    timestamp spikes both models produce (those flip back within a word or two).

    Writes seg["drift"] = {at, dt, n}: where we currently place the displaced
    words, how far off they are, and how many of them there are.
    """
    stream, owner, ours = [], [], []
    for seg in segments:
        for word in seg.get("words") or []:
            if not isinstance(word.get("t0"), (int, float)):
                continue
            tokens = txfmt._norm_words(word["w"])
            if tokens:
                stream.append(tokens[0])
                owner.append(seg["id"])
                ours.append(word["t0"])

    other, times = [], []
    for t0, _t1, word in alt_words:
        tokens = txfmt._norm_words(word)
        if tokens:
            other.append(tokens[0])
            times.append(t0)

    found = {}
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(
            a=stream, b=other, autojunk=False).get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            found.setdefault(owner[i1 + k], []).append(
                (ours[i1 + k], times[j1 + k] - ours[i1 + k]))

    for seg in segments:
        seg.pop("dt", None)
        seg.pop("drift", None)
        pairs = found.get(seg["id"]) or []
        if len(pairs) < txfmt.TIMING_WORDS:
            continue
        offsets = [d for _t, d in pairs]

        # Longest run of consecutive words displaced the same way.
        best, run, cur, sign = [], [], [], 0
        for (t, d) in pairs:
            s = 1 if d > txfmt.TIMING_RUN_DRIFT else (
                -1 if d < -txfmt.TIMING_RUN_DRIFT else 0)
            if s and s == sign:
                cur.append((t, d))
            elif s:
                sign, cur = s, [(t, d)]
            else:
                sign, cur = 0, []
            if len(cur) > len(best):
                best = list(cur)
        if len(best) >= txfmt.TIMING_RUN_WORDS:
            run = best
        elif abs(statistics.median(offsets)) > txfmt.TIMING_DRIFT:
            run = pairs                      # the whole segment is displaced

        if run:
            seg["drift"] = {
                "at": round(run[0][0], 3),
                "dt": round(statistics.median([d for _t, d in run]), 3),
                "n": len(run),
            }


def build(src, speakers, alt_words, audio, models, title):
    data = json.loads(src.read_text(encoding="utf-8"))
    segments = []
    for i, seg in enumerate(data.get("segments", []), 1):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        words = []
        for w in seg.get("words", []):
            word = (w.get("word") or "").strip()
            if not word:
                continue
            # A word missing start/end is one the aligner could not place; keep
            # the token, drop the timing rather than inventing one.
            t0, t1 = w.get("start"), w.get("end")
            entry = {"w": word}
            if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
                entry["t0"] = round(float(t0), 3)
                entry["t1"] = round(float(t1), 3)
            if isinstance(w.get("score"), (int, float)):
                entry["p"] = round(float(w["score"]), 3)
            if isinstance(w.get("speaker"), str):
                entry["speaker"] = w["speaker"]
            words.append(entry)
        entry = {
            "id": seg.get("source_id", f"s{i:05d}"),
            "speaker": seg.get("speaker", "UNKNOWN"),
            "start": round(float(seg.get("start", 0)), 3),
            "end": round(float(seg.get("end", 0)), 3),
            "text": text,
            "orig": text,
            "words": words,
            "status": "unreviewed",
        }
        if isinstance(seg.get("avg_logprob"), (int, float)):
            entry["logprob"] = round(float(seg["avg_logprob"]), 4)
        if seg.get("speaker_basis"):
            entry["speaker_basis"] = seg["speaker_basis"]
        segments.append(entry)   # `alt` is filled in by align_segments below

    if alt_words:
        align_segments(segments, alt_words)
        timing_offsets(segments, alt_words)

    tx = {
        "version": txfmt.VERSION,
        "title": title,
        "audio": audio,
        "models": models,
        "speakers": speakers,
        "segments": segments,
        "edits": [],
    }
    if data.get("diarization"):
        tx["diarization"] = data["diarization"]
    refresh_suggestions(tx)
    return tx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_files", nargs="+")
    ap.add_argument("--alt-suffix", default=".parakeet.json",
                    help="second-opinion file, as <stem><suffix> (default: "
                         ".parakeet.json). Absent is fine — the queue then "
                         "ranks on Whisper's own confidence alone.")
    ap.add_argument("--audio-suffix", default=".opus",
                    help="browser-playable render of the audio (default .opus)")
    ap.add_argument("--budget", type=float, default=txfmt.REVIEW_BUDGET,
                    help="fraction of segments to put in the review queue")
    ap.add_argument("--install", metavar="DIR",
                    help="also copy the .tx.json and .md into DIR (e.g. md/)")
    ap.add_argument("--raw-timings", action="store_true",
                    help="ignore <stem>.realigned.json and use Whisper's own "
                         "segment timings, warts and all")
    ap.add_argument("--force", action="store_true",
                    help="rebuild existing local and installed transcripts "
                         "(DESTROYS corrections and review status)")
    args = ap.parse_args()

    for name in args.json_files:
        src = pathlib.Path(name)
        if not args.force and reuse_transcript(src, args.install):
            continue
        if not src.exists():
            print(f"  !! missing {name}", file=sys.stderr)
            continue
        stem = src.stem
        # Transcript identity follows the recording; raw audio keeps its
        # preparation suffix for cache reuse and provenance.
        title = transcript_stem(stem)
        out_tx = src.with_name(title + ".tx.json")
        out_md = src.with_name(title + ".md")
        audio_src = src.with_suffix(".wav")

        # Speaker map, same convention as before.
        smap_path = src.with_suffix(".speakers.json")
        if smap_path.exists():
            speakers = json.loads(smap_path.read_text(encoding="utf-8"))
        else:
            data = json.loads(src.read_text(encoding="utf-8"))
            found = sorted({s.get("speaker", "UNKNOWN")
                            for s in data.get("segments", [])})
            speakers = {s: s for s in found}
            smap_path.write_text(json.dumps(speakers, indent=2))
            print(f"  wrote speaker stub -> {smap_path.name} (fill in the names)")

        # Prefer re-aligned timings when realign.py has produced them. Whisper
        # segment windows constrain WhisperX's aligner, so the shipped timings
        # can place a segment seconds from the speech it transcribes; realign.py
        # re-runs alignment between anchors both models agree on. Same words
        # either way — only the clock differs.
        realigned = src.parent / (stem + ".realigned.json")
        if realigned.exists() and not args.raw_timings:
            print(f"    using re-aligned timings ({realigned.name})")
            src = realigned

        alt_path = src.parent / (stem + args.alt_suffix)
        alt_words = []
        if alt_path.exists():
            alt_words = load_alt_words(alt_path)

        models = {"asr": "whisperx large-v3", "align": "wav2vec2",
                  "diarize": "pyannote"}
        models.update(json.loads(src.read_text(encoding="utf-8")).get("models", {}))
        if alt_words:
            models["second_opinion"] = alt_path.name

        tx = build(src, speakers, alt_words,
                   audio=stem + args.audio_suffix, models=models, title=title)
        if audio_src.exists():
            tx["duration"] = probe_duration(audio_src)
            tx["audio_sha256"] = sha256(audio_src)

        flagged = txfmt.flag(tx, budget=args.budget)
        txfmt.save(out_tx, tx)

        md, _spans = txfmt.render_md(tx)
        out_md.write_text(md, encoding="utf-8")

        total = len(tx["segments"])
        runtime = txfmt.hms(tx["segments"][-1]["end"]) if total else "0:00:00"
        alt_note = "" if alt_words else "  (no second opinion — see second_opinion.py)"
        print(f"  {stem}: {total} segments, {runtime} runtime, "
              f"{flagged} queued for review ({flagged / max(1, total):.0%}){alt_note}")

        if args.install:
            dest = pathlib.Path(args.install)
            dest.mkdir(parents=True, exist_ok=True)
            if dest.resolve() != out_tx.parent.resolve():
                shutil.copy2(out_tx, dest / out_tx.name)
                shutil.copy2(out_md, dest / out_md.name)
            print(f"    -> {dest / out_md.name}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Move re-aligned word timings onto a reviewed .tx.json, in place.

realign.py writes a corrected copy of the raw WhisperX JSON. This carries its
timings across without rebuilding the transcript, because build_tx.py would
reset `edits` to [] and every segment to `unreviewed`.

Words map by position in the file's word stream: re-alignment changes times,
never the words themselves, so the two streams are the same length and in the
same order (checked below, and refused if not).

Where a segment moves far enough that a human judgement about it was made while
the wrong audio was playing, its status goes back to `unreviewed`. Its text is
left alone: being back in the queue already forces a second look with the right
audio, and a correction like "Rust bags" -> "rosbags" is right whatever was in
the speakers. --revert-text restores the machine's wording as well.
"""
import argparse
import array
import json
import math
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import txfmt                                                     # noqa: E402
from speaker_evidence import refresh_suggestions
from artifacts import raw_artifact
from build_tx import align_segments, load_alt_words, timing_offsets  # noqa: E402


FRAME = 0.10          # seconds per energy frame
SPEECH_FLOOR = 0.35   # of the file's median frame RMS
# Reject a proposed move only when it is a substantial degradation: more than a
# third of the segment's word onsets leaving speech for silence. Rejecting on
# *any* drop is too blunt — ip1's s00020, whose move to 82.8s is confirmed by
# the energy profile, scores 1.000 -> 0.875 because one word of eight lands on
# a brief pause. That threshold vetoed a correct fix; a third does not, and
# still catches every move into a stretch that is under 30% speech.
REJECT_MARGIN = 1.0 / 3.0
# ...and separately, never accept a move that lands in mostly silence, however
# small the drop. A segment going from 0.50 to 0.25 is inside the margin above
# but is still landing where nobody is speaking.
REJECT_FLOOR = 0.30


def energy(audio_path):
    """Per-frame RMS, and the level below which a frame counts as silence."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", str(audio_path), "-f", "s16le",
         "-ac", "1", "-ar", "16000", "-"], capture_output=True).stdout
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) // 2 * 2])
    step = int(FRAME * 16000)
    frames = [math.sqrt(sum(x * x for x in pcm[i:i + step]) / step)
              for i in range(0, len(pcm) - step, step)]
    if not frames:
        return [], 0.0
    return frames, sorted(frames)[len(frames) // 2] * SPEECH_FLOOR


def speech_score(frames, floor, onsets):
    """Fraction of these word onsets that land on a frame containing speech.

    This is the referee. Re-alignment proposes; a word onset landing in silence
    is evidence against wherever it was put. Measured per segment, because a
    segment is the unit someone presses play on.
    """
    if not onsets or not frames:
        return None
    hit = 0
    for t in onsets:
        k = int(t / FRAME)
        if 0 <= k < len(frames) and frames[k] > floor:
            hit += 1
    return hit / len(onsets)


def stream(segments, key):
    return [(seg, w) for seg in segments for w in (seg.get("words") or [])
            if isinstance(w.get(key), (int, float))]


def matched_streams(tx, raw_segments):
    """New captures have stable IDs; do not pair re-sorted segments by position."""
    if raw_segments and all(s.get('source_id') for s in raw_segments):
        by_id = {s['source_id']: s for s in raw_segments}
        if len(by_id) != len(raw_segments) or any(s['id'] not in by_id for s in tx['segments']):
            raise ValueError('segment identities do not match')
        raw_segments = [by_id[s['id']] for s in tx['segments']]
    mine = stream(tx['segments'], 't0')
    theirs = stream(raw_segments, 'start')
    if len(mine) != len(theirs) or any(
            ours.get('w', '').strip() != new.get('word', '').strip()
            for (_s, ours), (_r, new) in zip(mine, theirs)):
        raise ValueError('original word streams do not match')
    return mine, theirs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tx_files", nargs="+")
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--source-suffix", default=".realigned.json")
    ap.add_argument("--alt-suffix", default=".parakeet.json")
    ap.add_argument("--audio-dir", default="../md/audio")
    ap.add_argument("--reset-moved", type=float, default=0.5)
    ap.add_argument("--revert-text", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for name in args.tx_files:
        path = pathlib.Path(name)
        stem = path.name[: -len(".tx.json")]
        raw = raw_artifact(stem, args.source_suffix, args.raw_dir)
        if not raw.exists():
            print(f"  !! no re-aligned source for {stem}", file=sys.stderr)
            continue

        tx = txfmt.load(path)
        old_md, _ = txfmt.render_md(tx)
        try:
            mine, theirs = matched_streams(tx, json.loads(raw.read_text(encoding='utf-8'))['segments'])
        except ValueError as error:
            print(f"  !! {stem}: {error} — refusing to guess", file=sys.stderr)
            continue

        text_before = {s["id"]: s["text"] for s in tx["segments"]}
        edits_before = len(tx.get("edits") or [])

        audio = pathlib.Path(args.audio_dir) / (tx.get("audio") or "")
        frames, floor = energy(audio) if audio.exists() else ([], 0.0)
        if not frames:
            print(f"  !! {stem}: no audio at {audio} — cannot referee the "
                  f"re-alignment, skipping", file=sys.stderr)
            continue

        # Group the proposal by segment, then keep it only where the audio
        # agrees it is an improvement. Re-alignment fixes segments Whisper
        # mistimed, but it also moves some into silence: on ip3 it put "Why do
        # you create the mask?" 47s earlier, into a stretch that is 4.8% speech
        # (it had been in one that is 66.7%). Applying it wholesale would trade
        # one set of wrong timings for another.
        proposed = {}
        for (seg, ours), (_s, new_word) in zip(mine, theirs):
            proposed.setdefault(seg["id"], []).append((ours, new_word))

        moved, kept_old, took_new = {}, 0, 0
        for seg in tx["segments"]:
            rows = proposed.get(seg["id"])
            if not rows:
                continue
            before = speech_score(frames, floor, [w["t0"] for w, _n in rows])
            after = speech_score(frames, floor, [n["start"] for _w, n in rows])
            shift = max(abs(n["start"] - w["t0"]) for w, n in rows)
            if shift > 0.25 and after is not None and (
                    after < before - REJECT_MARGIN
                    or (after < REJECT_FLOOR and after < before)):
                kept_old += 1          # the proposal is worse; leave it alone
                continue
            took_new += 1
            for ours, new_word in rows:
                moved[seg["id"]] = max(moved.get(seg["id"], 0.0),
                                       abs(new_word["start"] - ours["t0"]))
                ours["t0"] = round(float(new_word["start"]), 3)
                ours["t1"] = round(float(new_word["end"]), 3)
                if isinstance(new_word.get("score"), (int, float)):
                    ours["p"] = round(float(new_word["score"]), 3)

        for seg in tx["segments"]:
            timed = [w for w in (seg.get("words") or [])
                     if isinstance(w.get("t0"), (int, float))]
            if timed:
                seg["start"] = round(min(w["t0"] for w in timed), 3)
                seg["end"] = round(max(w["t1"] for w in timed), 3)
        tx["segments"].sort(key=lambda s: (s["start"], s["id"]))

        returned = []
        for seg in tx["segments"]:
            if moved.get(seg["id"], 0.0) <= args.reset_moved:
                continue
            if seg.get("status", "unreviewed") == "unreviewed":
                continue
            was = seg["text"]
            if args.revert_text and was != (seg.get("orig") or was):
                tx.setdefault("edits", []).append({
                    "ts": txfmt.datetime.datetime.now().astimezone()
                          .isoformat(timespec="seconds"),
                    "seg": seg["id"], "field": "realign-reset",
                    "t0": seg["start"], "t1": seg["end"],
                    "before": was, "after": seg["orig"],
                    "by": "", "method": "realign"})
                seg["text"] = seg["orig"]
                seg.pop("words_stale", None)
            seg["status"] = "unreviewed"
            returned.append((seg["id"], round(moved[seg["id"]], 2),
                             was, seg.get("orig") or was))

        alt_path = raw_artifact(stem, args.alt_suffix, args.raw_dir)
        if alt_path.exists():
            alt_words = load_alt_words(alt_path)
            align_segments(tx["segments"], alt_words)
            timing_offsets(tx["segments"], alt_words)
        suggestions = refresh_suggestions(tx)
        flagged = txfmt.flag(tx)
        print(f"  {suggestions} speaker suggestions for review (no labels changed)")

        if not args.revert_text:
            assert {s["id"]: s["text"] for s in tx["segments"]} == text_before, \
                f"{stem}: text changed"
            assert len(tx.get("edits") or []) == edits_before, \
                f"{stem}: edit log changed"

        big = sum(1 for v in moved.values() if v > 1.0)
        print(f"    re-alignment accepted on {took_new} segments, rejected on "
              f"{kept_old} where it moved words into silence")
        kept = sum(1 for s in tx["segments"] if s["status"] != "unreviewed")
        suspect = sum(1 for s in tx["segments"] if "timing" in s.get("flags", []))
        print(f"  {stem}: {big} segments moved over 1s; {flagged} queued, "
              f"{suspect} still timing-suspect; {len(returned)} reviews returned, "
              f"{kept} kept" + ("  [dry run]" if args.dry_run else ""))
        edited = [r for r in returned if r[2] != r[3]]
        for seg_id, dist, was, orig in edited:
            print(f"      {seg_id} moved {dist}s — re-check this correction")
            print(f"        yours: {was[:58]!r}")
            print(f"        asr  : {orig[:58]!r}")

        if not args.dry_run:
            try:
                txfmt.check_markdown(path, old_md)
            except txfmt.MarkdownConflict as conflict:
                print(str(conflict), file=sys.stderr)
                try:
                    answer = input("Overwrite and continue? [y/N] ")
                except EOFError:
                    answer = ""
                if answer.strip().lower() != "y":
                    print("Cancelled; no files changed for this transcript.")
                    continue
                txfmt.check_markdown(path, old_md, conflict.overwrite_token)
            txfmt.save(path, tx)
            md, spans = txfmt.render_md(tx)
            (path.parent / (stem + ".md")).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()

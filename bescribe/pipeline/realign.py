#!/usr/bin/env python3
"""Re-align a transcript's word timings, freed from Whisper's segment windows.

WhisperX aligns each Whisper segment inside that segment's own time window:

    f1 = int(t1 * SAMPLE_RATE); f2 = int(t2 * SAMPLE_RATE)
    waveform_segment = audio[:, f1:f2]          # whisperx/alignment.py

Forced alignment can therefore only redistribute words *within* a window
Whisper already chose. When Whisper's timestamp is wrong the words cannot
escape it: on ip1, segments s00019-s00020 are packed into 65.9-75.4s while the
speech they transcribe runs to 88s, leaving 12.5 seconds of continuous audio
with no word assigned to it at all.

This re-aligns between *anchors* instead. An anchor is a word where WhisperX and
the independent second model place the same token within ANCHOR_TOL — they
agree to a median of 30ms across a 90-minute recording, so where they agree the
time is not in question. Everything between two anchors is re-aligned as one
block, so a word can move anywhere inside it. The second model is used only to
choose trustworthy boundaries, never as the clock: it is not more accurate
overall (measured by contiguous speech containing no word onset, the two are
comparable and each is better on different files), it is just independent.

    .venv-wx/bin/python pipeline/realign.py data/raw/ip1*.json
"""
import argparse
import bisect
import difflib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import txfmt                                    # noqa: E402
from artifacts import DEFAULT_INSTALL_DIR, find_transcript
from backend import clear_cache, torch_device
from speaker_evidence import remember_basis

ANCHOR_TOL = 0.15      # seconds; both models must agree this closely
MIN_BLOCK = 20.0       # grow a block to at least this before cutting at an
                       # anchor — a short block is as constrained as the
                       # per-segment windows this exists to escape
MAX_BLOCK = 30.0       # ...and no longer than this (wav2vec2 memory)


def load_alt(path):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return [(w["t0"], w["t1"], w["w"]) for w in data["words"]]


def flat_words(segments):
    out = []
    for i, seg in enumerate(segments):
        for w in seg.get("words", []):
            word = (w.get("word") or w.get("w") or "").strip()
            if word:
                out.append((i, w, word))
    return out


def pick_anchors(words, alt_words):
    """Indices into `words` whose time both models agree on."""
    ours = [txfmt._norm_words(w)[0] if txfmt._norm_words(w) else "" for _i, _w, w in words]
    theirs, times = [], []
    for t0, _t1, w in alt_words:
        toks = txfmt._norm_words(w)
        if toks:
            theirs.append(toks[0])
            times.append(t0)

    agree = []
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(
            a=ours, b=theirs, autojunk=False).get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            i = i1 + k
            mine = words[i][1].get("start")
            if isinstance(mine, (int, float)) and abs(times[j1 + k] - mine) <= ANCHOR_TOL:
                agree.append(i)
    return agree


def blocks(words, agree):
    """(start index, end index, t0, t1) blocks, cut only at anchors.

    A block must be long enough for a word to actually move within it. Cutting
    at every anchor would rebuild the very constraint this is escaping.
    """
    if len(agree) < 2:
        return []
    out = []
    lo = agree[0]
    t_lo = words[lo][1]["start"]
    for i in agree[1:]:
        t = words[i][1]["start"]
        if t - t_lo >= MIN_BLOCK:
            out.append((lo, i, t_lo, t))
            lo, t_lo = i, t
    tail_end = words[-1][1].get("end") or t_lo + 0.1
    out.append((lo, len(words), t_lo, max(t_lo + 0.1, tail_end)))
    # Anything longer than MAX_BLOCK gets split at its nearest interior anchor.
    final = []
    for lo, hi, t0, t1 in out:
        if t1 - t0 <= MAX_BLOCK:
            final.append((lo, hi, t0, t1))
            continue
        inner = [i for i in agree if lo < i < hi]
        cut = min(inner, key=lambda i: abs(words[i][1]["start"] - (t0 + t1) / 2),
                  default=None)
        if cut is None:
            final.append((lo, hi, t0, t1))
        else:
            tc = words[cut][1]["start"]
            final.append((lo, cut, t0, tc))
            final.append((cut, hi, tc, t1))
    return [b for b in final if b[1] > b[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_files", nargs="+")
    ap.add_argument("--alt-suffix", default=".parakeet.json")
    ap.add_argument("--out-suffix", default=".realigned.json")
    ap.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"),
                    default=os.environ.get("TORCH_DEVICE", "auto"))
    ap.add_argument("--install-dir", type=pathlib.Path, default=DEFAULT_INSTALL_DIR)
    ap.add_argument("--force", action="store_true", help="recompute existing results")
    args = ap.parse_args()

    align_model = None
    for name in args.json_files:
        src = pathlib.Path(name)
        out = src.parent / (src.stem + args.out_suffix)
        cached = find_transcript(src.stem, args.install_dir, src.parent)
        if not args.force and (cached or out.exists()):
            print(f"  reusing {cached or out}")
            continue
        data = json.loads(src.read_text(encoding="utf-8"))
        remember_basis(data['segments'])
        alt = load_alt(src.parent / (src.stem + args.alt_suffix))
        if align_model is None:
            import whisperx
            args.device = torch_device(args.device)
            align_model, metadata = whisperx.load_align_model(
                language_code="en", device=args.device)
        audio = whisperx.load_audio(str(src.with_suffix(".wav")))

        words = flat_words(data["segments"])
        agree = pick_anchors(words, alt)
        bl = blocks(words, agree)
        print(f"  {src.stem}: {len(words)} words, {len(agree)} anchors "
              f"({100*len(agree)/max(1,len(words)):.0f}%), {len(bl)} blocks")

        # One synthetic segment per block: the block's whole text, the block's
        # whole audio window. Words may land anywhere inside it.
        payload = [{"start": t0, "end": t1,
                    "text": " ".join(w for _i, _d, w in words[lo:hi])}
                   for lo, hi, t0, t1 in bl]
        result = whisperx.align(payload, align_model, metadata, audio,
                                args.device, return_char_alignments=False)

        # whisperx.align re-segments its output: three blocks in can come back
        # as twenty-three segments. Bucket the returned words by which block's
        # audio window they fall in, then match token order *within* a block.
        # Matching globally instead lets a repeated phrase ("Okay.", "I see.")
        # pair with an occurrence from a different block, which silently
        # scattered words across the file and broke word order outright.
        buckets = [[] for _ in bl]
        edges = [b[2] for b in bl]
        for seg in result["segments"]:
            for w in seg.get("words", []):
                if not isinstance(w.get("start"), (int, float)):
                    continue
                if not txfmt._norm_words(w.get("word") or ""):
                    continue
                k = bisect.bisect_right(edges, w["start"]) - 1
                if 0 <= k < len(bl):
                    buckets[k].append(w)

        fixed = 0
        for (lo, hi, _t0, _t1), got in zip(bl, buckets):
            if not got:
                continue
            mine = []
            for _i, _d, w in words[lo:hi]:
                tok = txfmt._norm_words(w)
                mine.append(tok[0] if tok else "")
            got_tok = [txfmt._norm_words(w["word"])[0] for w in got]
            for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(
                    a=mine, b=got_tok, autojunk=False).get_opcodes():
                if tag != "equal":
                    continue
                for k in range(i2 - i1):
                    entry = words[lo + i1 + k][1]
                    hit = got[j1 + k]
                    entry["start"] = round(float(hit["start"]), 3)
                    entry["end"] = round(float(hit["end"]), 3)
                    if isinstance(hit.get("score"), (int, float)):
                        entry["score"] = round(float(hit["score"]), 3)
                    fixed += 1

        # Word order is a hard invariant: a later word may not start earlier
        # than an earlier one. Anything that would break it keeps its old time.
        last = -1.0
        broke = 0
        for _i, entry, _w in words:
            if entry["start"] < last - 1e-6:
                broke += 1
                entry["start"] = round(last, 3)
                entry["end"] = round(max(entry["end"], last), 3)
            last = entry["start"]
        if broke:
            print(f"    !! {broke} words clamped to keep order")

        for seg in data["segments"]:
            timed = [w for w in seg.get("words", [])
                     if isinstance(w.get("start"), (int, float))]
            if timed:
                seg["start"] = round(min(w["start"] for w in timed), 3)
                seg["end"] = round(max(w["end"] for w in timed), 3)
        data["segments"].sort(key=lambda s: s["start"])

        out.write_text(json.dumps(data), encoding="utf-8")
        print(f"    {fixed}/{len(words)} words re-timed -> {out.name}")
        del audio
        clear_cache(args.device)


if __name__ == "__main__":
    main()

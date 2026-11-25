#!/usr/bin/env python3
"""
score_diarization.py — measure a WhisperX run against hand-marked speaker turns.

You mark 3 minutes of audio by ear: who is talking, when. No words. This
scores any number of WhisperX JSON runs against that, so config changes
(VAD thresholds, normalisation, diarization model) can be compared on numbers
instead of impressions.

GOLD FILE FORMAT — one line per span. Blank lines and #comments ignored.
Times as MM:SS, M:SS.s, or H:MM:SS. Unmarked spans count as silence.

    12:00-12:04    E              single speaker
    12:04-12:06    E+P            simultaneous: either label accepted
    12:22.5-13:32  P ~E           P primary, E interjecting

Two accuracies are reported. LENIENT accepts any speaker listed for a span,
which is the fair test when the run assigns one speaker per segment and the
gold says two people are talking. STRICT wants the primary (first-listed)
speaker, which is what matters for attributing a quote to the right person.

If gold times are absolute recording times but the clip starts later, pass
--gold_offset -SECONDS to shift them onto the clip's timeline.

Cluster IDs (SPEAKER_00 etc.) are mapped to your labels automatically, by
whichever assignment maximises agreement.

Usage:
  python score_diarization.py gold.txt run1.json run2.json ...
  python score_diarization.py gold_1200_1500.txt data/raw/gold3_*.json \
      --gold_offset -720
"""

import argparse
import itertools
import json
import pathlib
import re
import sys

RES = 0.01  # scoring grid, seconds


def parse_time(s):
    parts = s.strip().split(":")
    if not all(p.replace(".", "").isdigit() for p in parts):
        raise ValueError(f"bad time: {s}")
    parts = [float(p) for p in parts]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"bad time: {s}")


def load_gold(path, offset=0.0):
    """
    -> [(start, end, primary, accepted_set)], plus (t0, t1) scored bounds.

    "E+P" and "P ~E" both yield an accepted set of {E, P}; they differ only in
    which speaker is primary, which strict scoring cares about.
    """
    spans = []
    for lineno, raw in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        m = re.match(r"^([\d:.]+)\s*-\s*([\d:.]+)\s+(.+)$", line)
        if not m:
            print(f"  ! gold line {lineno} ignored: {raw!r}", file=sys.stderr)
            continue
        a = parse_time(m.group(1)) + offset
        b = parse_time(m.group(2)) + offset
        labels = m.group(3).replace("+", " ").replace("~", " ").upper().split()
        labels = [x for x in labels if x not in ("END", "-", "SILENCE")]
        if b > a and labels:
            spans.append((a, b, labels[0], set(labels)))

    if not spans:
        sys.exit("no usable spans in gold file")
    spans.sort(key=lambda x: x[0])
    return spans, (spans[0][0], spans[-1][1])


def load_run(path):
    """-> [(start, end, cluster)] merged into contiguous same-speaker turns."""
    segs = json.loads(pathlib.Path(path).read_text()).get("segments", [])
    out = []
    for s in segs:
        spk = s.get("speaker")
        if spk is None or s.get("end") is None:
            continue
        if out and out[-1][2] == spk and s["start"] - out[-1][1] < 0.5:
            out[-1][1] = float(s["end"])
        else:
            out.append([float(s["start"]), float(s["end"]), spk])
    return [tuple(x) for x in out]


def grid(turns, t0, t1):
    """Frame-level label array for a prediction run."""
    n = int((t1 - t0) / RES)
    g = [None] * n
    for a, b, lab in turns:
        i = max(0, int((a - t0) / RES))
        j = min(n, int((b - t0) / RES))
        for k in range(i, j):
            g[k] = lab
    return g


def gold_grids(spans, t0, t1):
    """(primary[], accepted[]) frame arrays; None where nothing is marked."""
    n = int((t1 - t0) / RES)
    prim, acc = [None] * n, [None] * n
    for a, b, p, s in spans:
        i = max(0, int((a - t0) / RES))
        j = min(n, int((b - t0) / RES))
        for k in range(i, j):
            prim[k], acc[k] = p, s
    return prim, acc


def best_mapping(gold_g, pred_g):
    """Map cluster IDs to gold labels by maximising frame agreement."""
    gold_labels = sorted({x for x in gold_g if x})
    clusters = sorted({x for x in pred_g if x})
    if not clusters:
        return {}

    # Overlap counts, then exhaustive assignment (speaker counts are tiny).
    counts = {c: {g: 0 for g in gold_labels} for c in clusters}
    for g, p in zip(gold_g, pred_g):
        if g and p:
            counts[p][g] += 1

    best, best_score = {}, -1
    span = gold_labels if len(gold_labels) >= len(clusters) else \
        gold_labels + [None] * (len(clusters) - len(gold_labels))
    for perm in set(itertools.permutations(span, len(clusters))):
        mapping = dict(zip(clusters, perm))
        score = sum(counts[c][g] for c, g in mapping.items() if g)
        if score > best_score:
            best, best_score = mapping, score
    return best


def score(spans, pred_turns, t0, t1):
    prim_g, acc_g = gold_grids(spans, t0, t1)
    pred_raw = grid(pred_turns, t0, t1)
    mapping = best_mapping(prim_g, pred_raw)
    pred_g = [mapping.get(x) if x else None for x in pred_raw]

    speech = sum(1 for x in prim_g if x)
    if not speech:
        sys.exit("gold has no speech in the scored window")

    strict = sum(1 for g, p in zip(prim_g, pred_g) if g and p and g == p)
    lenient = sum(1 for a, p in zip(acc_g, pred_g) if a and p and p in a)
    confused = sum(1 for a, p in zip(acc_g, pred_g) if a and p and p not in a)
    missed = sum(1 for g, p in zip(prim_g, pred_g) if g and not p)

    # Per-span recall and absorption.
    recalled = absorbed = 0
    for a, b, primary, accepted in spans:
        i, j = int((a - t0) / RES), int((b - t0) / RES)
        window = [x for x in pred_g[max(0, i):max(0, j)] if x]
        if window:
            recalled += 1
            if not (accepted & set(window)):
                absorbed += 1

    # Boundary error: gold turn start -> nearest predicted turn start.
    pred_starts = sorted(s for s, _, _ in pred_turns)
    devs = []
    for a, _, _, _ in spans:
        if pred_starts:
            devs.append(min(abs(a - s) for s in pred_starts))
    devs.sort()
    mae = devs[len(devs) // 2] if devs else float("nan")

    return {
        "mapping": mapping,
        "strict": 100 * strict / speech,
        "lenient": 100 * lenient / speech,
        "confusion": 100 * confused / speech,
        "missed": 100 * missed / speech,
        "turn_recall": 100 * recalled / len(spans),
        "absorbed": absorbed,
        "gold_turns": len(spans),
        "pred_turns": len(pred_turns),
        "boundary_median": mae,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gold")
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--gold_offset", type=float, default=0.0,
                    help="seconds to add to gold times (if gold starts at 0 "
                         "but the excerpt begins later in the recording)")
    args = ap.parse_args()

    spans, (t0, t1) = load_gold(args.gold, args.gold_offset)
    overlaps = sum(1 for *_, a in spans if len(a) > 1)
    print(f"gold: {len(spans)} spans ({overlaps} with overlap), "
          f"{t0:.0f}-{t1:.0f}s ({(t1-t0)/60:.1f} min), speakers "
          f"{sorted({l for *_, a in spans for l in a})}\n")

    hdr = (f"{'run':<28} {'strict%':>8} {'lenient%':>9} {'conf%':>6} "
           f"{'miss%':>6} {'recall%':>8} {'absorb':>7} {'turns':>6} {'bnd_s':>6}")
    print(hdr)
    print("-" * len(hdr))

    results = []
    for path in args.runs:
        pred = load_run(path)
        if not pred:
            print(f"{pathlib.Path(path).stem:<28} (no speaker labels in file)")
            continue
        r = score(spans, pred, t0, t1)
        results.append((path, r))
        print(f"{pathlib.Path(path).stem:<28} {r['strict']:8.1f} "
              f"{r['lenient']:9.1f} {r['confusion']:6.1f} {r['missed']:6.1f} "
              f"{r['turn_recall']:8.1f} {r['absorbed']:7d} "
              f"{r['pred_turns']:6d} {r['boundary_median']:6.2f}")

    if results:
        print(f"\n(gold has {len(spans)} spans)")
        for path, r in results:
            print(f"  {pathlib.Path(path).stem}: {r['mapping']}")
        best = max(results, key=lambda x: x[1]["strict"])
        print(f"\nbest strict accuracy: {pathlib.Path(best[0]).stem} "
              f"({best[1]['strict']:.1f}%)")


if __name__ == "__main__":
    main()
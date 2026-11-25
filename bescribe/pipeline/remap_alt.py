#!/usr/bin/env python3
"""Recompute the second-opinion fields on an existing .tx.json, in place.

build_tx.py rebuilds a transcript from scratch: it writes `"edits": []` and
resets every segment to `unreviewed`. Once a human has started reviewing, that
is destructive, so improvements to the second-opinion machinery have to be
migrated onto the reviewed file rather than regenerated into it.

Only `alt`, `align`, `dt`, `risk`, `flags` and `notes` are touched. Text,
status, word timings and the edit log are left exactly as they are, and the .md
is re-rendered only if it would actually change (it should not — none of these
fields appear in it).

    python pipeline/remap_alt.py ../../md/*.tx.json --alt-dir data/raw
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import txfmt                                              # noqa: E402
from artifacts import raw_artifact
from build_tx import (align_segments, load_alt_words,      # noqa: E402
                      timing_offsets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tx_files", nargs="+")
    ap.add_argument("--alt-dir", default="data/raw",
                    help="where the <stem><suffix> second-opinion files live")
    ap.add_argument("--alt-suffix", default=".parakeet.json")
    ap.add_argument("--budget", type=float, default=txfmt.REVIEW_BUDGET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for name in args.tx_files:
        path = pathlib.Path(name)
        stem = path.name[: -len(".tx.json")]
        alt_path = raw_artifact(stem, args.alt_suffix, args.alt_dir)
        if not alt_path.exists():
            print(f"  !! no second opinion for {stem} at {alt_path}", file=sys.stderr)
            continue

        tx = txfmt.load(path)
        before = {s["id"]: (s["text"], s["status"]) for s in tx["segments"]}
        edits = len(tx.get("edits", []))
        old_md, _ = txfmt.render_md(tx)

        alt_words = load_alt_words(alt_path)
        align_segments(tx["segments"], alt_words)
        timing_offsets(tx["segments"], alt_words)
        flagged = txfmt.flag(tx, budget=args.budget)

        # The whole point of this script: prove we did not touch the review.
        after = {s["id"]: (s["text"], s["status"]) for s in tx["segments"]}
        assert before == after, f"{stem}: review state changed"
        assert len(tx.get("edits", [])) == edits, f"{stem}: edit log changed"
        new_md, _ = txfmt.render_md(tx)
        assert new_md == old_md, f"{stem}: .md would change"

        timing = sum(1 for s in tx["segments"] if "timing" in s.get("flags", []))
        reviewed = sum(1 for s in tx["segments"] if s["status"] != "unreviewed")
        print(f"  {stem}: {flagged} queued, {timing} timing-suspect; "
              f"{edits} edits and {reviewed} reviewed segments preserved"
              + ("  [dry run]" if args.dry_run else ""))
        if not args.dry_run:
            txfmt.save(path, tx)


if __name__ == "__main__":
    main()

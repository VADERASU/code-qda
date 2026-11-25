#!/usr/bin/env python3
"""Compare speaker assignment at original and repaired times, without applying it.

Requires fresh intervals from capture_diarization.py. Cluster identities are
matched to historical machine labels, never to human corrections. Those edits
are imperfect consistency evidence; unchanged labels are NOT ground truth.
Output includes all comparisons and a deterministic, stratified listening set.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import html
import itertools
import json
import math
import os
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from speaker_evidence import assignment


def map_clusters(turns, raw):
    """One-to-one permutation maximizing overlap with ORIGINAL machine words.

    Same mapping for both timestamp conditions and regular/exclusive intervals.
    This removes arbitrary cluster numbering; it does not tune to corrections.
    """
    if not turns:
        raise ValueError('no diarization intervals to compare')
    labels = sorted({t['speaker'] for t in turns})
    lower, upper = min(t['start'] for t in turns), max(t['end'] for t in turns)
    words = [w for s in raw['segments'] for w in s.get('words', [])
             if w.get('speaker') and isinstance(w.get('start'), (int, float))
             and isinstance(w.get('end'), (int, float))
             and w['end'] > lower and w['start'] < upper]
    old_labels = sorted({w['speaker'] for w in words})
    if len(labels) != len(old_labels) or not labels or len(labels) > 7:
        raise ValueError("trial requires 1–7 matching speaker counts; do not guess missing identities")
    weights = defaultdict(float)
    for w in words:
        for t in turns:
            overlap = min(w['end'], t['end']) - max(w['start'], t['start'])
            if overlap > 0:
                weights[t['speaker'], w['speaker']] += overlap
    candidates = sorted(
        ((sum(weights[a, b] for a, b in zip(labels, perm)), perm)
         for perm in itertools.permutations(old_labels)), reverse=True)
    if candidates[0][0] <= 0 or (len(candidates) > 1 and math.isclose(
            candidates[0][0], candidates[1][0], abs_tol=1e-9)):
        raise ValueError("speaker identity mapping is ambiguous")
    return dict(zip(labels, candidates[0][1]))


def analyze(raw, tx, captured, min_shift=0.5, edge_guard=20):
    edits = {e['seg'] for e in tx.get('edits', []) if e.get('field') == 'speaker'}
    mapping = map_clusters(captured['regular'], raw)
    names = tx['speakers']
    window = captured.get('window')
    rows, summary = [], {}
    for mode in ('regular', 'exclusive'):
        turns = [dict(t, speaker=mapping[t['speaker']]) for t in captured[mode]]
        counts = Counter()
        for seg in tx['segments']:
            original = raw['segments'][int(seg['id'][1:]) - 1]
            if seg['orig'].strip() != original['text'].strip():
                raise ValueError(f"segment identity mismatch: {seg['id']}")
            if window and (min(seg['start'], original['start']) < window['start'] + edge_guard
                           or max(seg['end'], original['end']) > window['end'] - edge_guard):
                continue
            old = assignment(turns, original['start'], original['end'])
            new = assignment(turns, seg['start'], seg['end'])
            shift = max(abs(seg['start'] - original['start']),
                        abs(seg['end'] - original['end']))
            changed = old['speaker'] != new['speaker']
            moved = shift > min_shift
            protected = seg['id'] in edits
            # A first trial threshold, fixed before inspecting the outcomes.
            # No suggestion is applied; weak/tied evidence remains in the report.
            supported = (new['speaker'] is not None and new['coverage'] >= 0.5
                         and new['share'] >= 0.6)
            candidate = moved and changed and supported
            current = seg.get('speaker')
            raw_label = original.get('speaker')
            counts['segments'] += 1
            counts['historically_speaker_edited'] += protected
            counts['fresh_old_matches_historical_machine'] += old['speaker'] == raw_label
            counts['moved_over_threshold'] += moved
            counts['timing_changes_label'] += changed
            counts['moved_and_label_changes'] += moved and changed
            counts['supported_timing_changes'] += candidate
            counts['supported_changes_with_reproduced_baseline'] += candidate and old['speaker'] == raw_label
            counts['suggestions_on_unedited_segments'] += candidate and not protected and new['speaker'] != current
            counts['protected_suggestions'] += candidate and protected
            if protected:
                before_match = old['speaker'] == current
                after_match = new['speaker'] == current
                counts['old_agrees_with_current_edit'] += before_match
                counts['new_agrees_with_current_edit'] += after_match
                if candidate:
                    counts['protected_gains_agreement'] += after_match and not before_match
                    counts['protected_loses_agreement'] += before_match and not after_match
                    counts['protected_neither_agrees'] += not before_match and not after_match
            rows.append({
                'mode': mode, 'seg': seg['id'], 'start': seg['start'], 'end': seg['end'],
                'original_start': original['start'], 'original_end': original['end'],
                'shift': round(shift, 3), 'text': seg['text'],
                'historical_machine': names.get(raw_label, raw_label),
                'current': names.get(current, current), 'status': seg.get('status'),
                'speaker_edit_protected': protected,
                'old_assignment': dict(old, speaker=names.get(old['speaker'], old['speaker'])),
                'new_assignment': dict(new, speaker=names.get(new['speaker'], new['speaker'])),
                'candidate': candidate,
                'baseline_reproduced': old['speaker'] == raw_label,
                'suggestion': candidate and not protected and new['speaker'] != current,
            })
        summary[mode] = dict(counts)
    return {'cluster_mapping': mapping, 'min_shift_seconds': min_shift,
            'edge_guard_seconds': edge_guard,
            'min_coverage': 0.5, 'min_share': 0.6, 'summary': summary, 'rows': rows}


def listening_sample(rows):
    groups = defaultdict(list)
    for r in rows:
        if r['mode'] != 'regular':
            continue
        old, new = r['old_assignment']['speaker'], r['new_assignment']['speaker']
        if r['candidate'] and r['speaker_edit_protected']:
            group = ('agrees with edit' if new == r['current'] else
                     'moves away from edit' if old == r['current'] else
                     'neither assignment matches edit')
        elif r['suggestion']:
            group = 'suggested change without a speaker edit'
        elif old == new:
            group = 'unchanged assignment, moved timing' if r['shift'] > 0.5 else 'unchanged control'
        else:
            continue
        groups[group].append(r)
    rng = random.Random(0)
    return [{'group': group, **r} for group, pool in sorted(groups.items())
            for r in rng.sample(pool, min(4, len(pool)))]


def reference_check(raw, tx, captured, reference):
    """Compare labels on IDENTICAL repaired time spans against a separate reference.

    Fit only the anonymous-label permutation on the regular baseline, then freeze
    it across every arm. Do not independently optimize identities for candidates.
    """
    from score_diarization import load_gold, gold_grids, grid, best_mapping, RES

    spans, (start, end) = load_gold(reference)
    window = captured.get('window')
    if window and (start < window['start'] or end > window['end']):
        raise ValueError('reference is outside captured audio')
    rows = analyze(raw, tx, captured, edge_guard=0)['rows']
    primary, accepted = gold_grids(spans, start, end)
    speech = sum(bool(s) for s in primary)
    arms = {}
    for mode in ('regular', 'exclusive'):
        selected = sorted((r for r in rows if r['mode'] == mode), key=lambda r: r['start'])
        for arm in ('old', 'timing_patch', 'all_repaired'):
            turns = []
            for row in selected:
                use_new = (arm == 'all_repaired' or
                           (arm == 'timing_patch' and row['candidate']))
                label = row['new_assignment' if use_new else 'old_assignment']['speaker']
                turns.append((row['start'], row['end'], label))
            arms[f'{mode}_{arm}'] = grid(turns, start, end)
    mapping = best_mapping(primary, arms['regular_old'])
    scores = {}
    for arm, labels in arms.items():
        labels = [mapping.get(s) for s in labels]
        strict = sum(bool(g) and g == p for g, p in zip(primary, labels))
        lenient = sum(bool(g) and p in g for g, p in zip(accepted, labels))
        scores[arm] = {'primary_agreement_percent': round(100 * strict / speech, 3),
                      'any_accepted_speaker_percent': round(100 * lenient / speech, 3)}
    return {'reference': str(reference),
            'reference_sha256': hashlib.sha256(Path(reference).read_bytes()).hexdigest(),
            'window': [start, end], 'reference_speech_seconds': round(speech * RES, 2),
            'fixed_label_mapping': mapping, 'scores': scores,
            'note': 'Imperfect reference; all arms use the same repaired spans. '
                    'timing_patch is a counterfactual label-only arm, not an applied edit. '
                    'Reference boundaries and overlapping-speech notes remain uncertain.'}


def write_review(path, sample, audio):
    escape = html.escape
    audio_url = Path(os.path.relpath(audio.resolve(), path.parent.resolve())).as_posix()
    cards = []
    for row in sample:
        start, end = max(0, row['start'] - 2), row['end'] + 2
        cards.append(f'''<article><h2>{escape(row['group'])}: {escape(row['seg'])}</h2>
<p>{start:.2f}–{end:.2f}s (includes 2 seconds of context)</p>
<audio controls preload="none" src="{escape(audio_url, quote=True)}#t={start:.3f},{end:.3f}"></audio>
<p>{escape(row['text'])}</p><details><summary>Show assignments after listening</summary>
<p>Current: {escape(str(row['current']))}; original-time assignment: {escape(str(row['old_assignment']['speaker']))};
repaired-time assignment: {escape(str(row['new_assignment']['speaker']))}.</p>
<p>Timing movement: {row['shift']}s. Existing speaker edit protected: {row['speaker_edit_protected']}.</p>
</details></article>''')
    path.write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Diarization trial listening sample</title><style>
body{font:17px system-ui;max-width:850px;margin:40px auto;padding:0 20px;line-height:1.5}
article{border-top:1px solid #aaa;padding:18px 0}h2{font-size:19px}audio{width:100%}
</style><h1>Diarization trial: listening sample</h1>
<p>This sample has not been independently listened to. Current labels and edit history
are imperfect reference evidence. Listen before opening the assignments; check unchanged
controls too. These suggestions have not been applied. Audio requires the local recordings.</p>
''' + '\n'.join(cards) + '</html>', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('raw', type=Path)
    ap.add_argument('transcript', type=Path)
    ap.add_argument('turns', type=Path)
    ap.add_argument('--output-dir', type=Path, required=True, help='must not already exist')
    ap.add_argument('--audio', type=Path, required=True, help='local playback audio for listening sample')
    ap.add_argument('--reference', type=Path, help='optional independent hand-marked speaker turns')
    args = ap.parse_args()
    inputs = [args.raw, args.transcript, args.turns]
    loaded = [p.read_bytes() for p in inputs]
    result = analyze(*(json.loads(b) for b in loaded))
    result['inputs'] = {str(p): hashlib.sha256(b).hexdigest() for p, b in zip(inputs, loaded)}
    result['capture'] = {k: v for k, v in json.loads(loaded[2]).items() if k not in ('regular', 'exclusive')}
    if args.reference:
        result['reference_check'] = reference_check(*(json.loads(b) for b in loaded), args.reference)
    result['limitations'] = [
        'Fresh intervals, not the lost historical diarization output; excerpt clustering can differ from a full recording.',
        'Same fresh run and identity mapping used for both timestamp conditions.',
        'Human edits are imperfect evidence; unchanged labels are not ground truth.',
        'No independent listening validation; no accuracy or diarization error rate claimed.',
        'Coverage and overlap share are not calibrated confidence.',
        'All input transcripts, corrections, and annotations remain unchanged.',
    ]
    sample = listening_sample(result['rows'])
    result['listening_sample'] = [{'group': r['group'], 'seg': r['seg']} for r in sample]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    write_review(args.output_dir / 'review.html', sample, args.audio)
    for mode, counts in result['summary'].items():
        print(mode, json.dumps(counts, sort_keys=True))
    print(f"Listening sample: {len(sample)} segments; {args.output_dir / 'review.html'}")


if __name__ == '__main__':
    main()

"""Stored machine evidence and conservative speaker suggestions (stdlib only).

Speaker labels here are cluster IDs, not display names. Suggestions never change
speaker assignments, review status, text, timestamps, or the human edit log.
"""
from collections import defaultdict
import math


def assignment(turns, start, end):
    """Choose greatest duration overlap, abstaining on ties and silence.

    Coverage counts overlapping speech once. Share and coverage measure time
    overlap, not a calibrated probability that the speaker is correct.
    """
    scores = defaultdict(float)
    spans = []
    if end <= start:
        return {"speaker": None, "share": 0, "coverage": 0}
    for turn in turns:
        a, b = max(start, turn['start']), min(end, turn['end'])
        if b > a:
            scores[turn['speaker']] += b - a
            spans.append((a, b))
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return {"speaker": None, "share": 0, "coverage": 0}
    covered, last = 0, start
    for a, b in sorted(spans):
        covered += max(0, b - max(a, last))
        last = max(last, b)
    tied = len(ranked) > 1 and math.isclose(ranked[0][1], ranked[1][1], abs_tol=1e-9)
    return {"speaker": None if tied else ranked[0][0],
            "share": round(ranked[0][1] / sum(scores.values()), 4),
            "coverage": round(covered / (end - start), 4)}


def remember_basis(segments):
    """Retain stable IDs and the timestamps on which machine labels were based."""
    for i, seg in enumerate(segments, 1):
        seg.setdefault('source_id', f's{i:05d}')
        seg.setdefault('speaker_basis', {
            'start': seg['start'], 'end': seg['end'],
            'speaker': seg.get('speaker', 'UNKNOWN'),
        })


def refresh_suggestions(tx):
    protected = {e['seg'] for e in tx.get('edits', [])
                 if e.get('field') in ('speaker', 'speaker-review')}
    turns = (tx.get('diarization') or {}).get('turns', [])
    count = 0
    for seg in tx.get('segments', []):
        seg.pop('speaker_suggestion', None)
        basis = seg.get('speaker_basis')
        if not turns or not basis or seg['id'] in protected:
            continue
        # A changed label with no logged edit may still be a manual correction.
        if seg.get('speaker') != basis.get('speaker'):
            continue
        shift = max(abs(seg['start'] - basis['start']), abs(seg['end'] - basis['end']))
        if shift <= 0.5:
            continue
        old = assignment(turns, basis['start'], basis['end'])
        new = assignment(turns, seg['start'], seg['end'])
        if (new['speaker'] not in tx.get('speakers', {})
                or new['speaker'] in (seg.get('speaker'), old['speaker'])
                or new['coverage'] < 0.5 or new['share'] < 0.6):
            continue
        seg['speaker_suggestion'] = dict(new, shift=round(shift, 3),
                                        reason='timing', start=seg['start'], end=seg['end'])
        count += 1
    return count

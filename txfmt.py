"""txfmt — the .tx.json transcript format: read, render, edit, remap.

A .tx.json file is the source of truth for one interview's transcript. The
.md that code-qda codes against is a *render* of it, regenerated whenever a
segment's text changes. Word timings therefore survive editing, which is what
makes click-a-word-to-hear-it possible.

    {
      "version": 1,
      "audio": "ip1feb102026.16k.opus",      # basename, resolved against AUDIO_DIR
      "duration": 4709.27,
      "models": {...},                        # what produced this, for the record
      "speakers": {"SPEAKER_00": "ERICK"},    # cluster id -> display name;
                                              # MANUAL_nn keys are people a
                                              # human added, not pyannote
      "segments": [
        {"id": "s0001", "speaker": "SPEAKER_00", "start": 8.1, "end": 9.2,
         "text": "I see. Okay.",              # current text; edited in place
         "orig": "I see. Okay.",              # what the ASR said; never changes
         "words": [{"w": "I", "t0": 8.1, "t1": 8.2, "p": 0.91}],
         "logprob": -0.13,                    # Whisper's own segment confidence
         "alt": "I see, okay.",               # second-opinion ASR over this span
         "risk": 0.42, "flags": ["disagree"], # computed by flag()
         "status": "unreviewed" | "ok" | "edited"}
      ],
      "edits": [                              # append-only; the audit trail AND
        {"ts": "...", "seg": "s0001",         # the corrections corpus
         "field": "text" | "speaker" | "speaker-review", # absent on older entries,
         "before": "...", "after": "...",     # which are all text edits
         "by": "rolando", "method": "human"}
      ]
    }

Segments, not turns, are the unit of edit: a turn is a display-time merge of
consecutive same-speaker segments, and merging is what the old pipeline did
irreversibly. Stdlib only — app.py depends on this.
"""

import datetime
import difflib
import hashlib
import json
import re
from pathlib import Path

VERSION = 1

PAUSE_THRESHOLD = 5.0   # seconds of gap before a pause marker is rendered
MIN_SEG_WORDS = 3       # below this, a low logprob means nothing
REVIEW_BUDGET = 0.15    # flag this fraction of segments, worst first

# Patterns that are probably a mishearing but are also plausible English, so
# they are never substituted — only surfaced for a human to judge with the
# audio in front of them.
SUSPECT = {
    r"\b(?:lighter|ladder|slider)\b": "LiDAR?",
    r"\bdiscourse\b": "DA scores?",
    r"\bshankler\b": "Chandler?",
    r"\bwebinar\b": "VLM?",
    r"\bthank you for watching\b": "hallucination in silence?",
    r"\bnuisance\b": "nuScenes?",
    r"\brepl\b": "repo?",
    r"\bros ?(?:bank|back)\b": "rosbag?",
    r"\b(?:whimo|whmo|wimo|weimo)\b": "Waymo?",
    r"\b(?:eagle|legal|eco) vehicle\b": "ego vehicle?",
    r"\b(?:hanging|hugging) place\b": "Hugging Face?",
    r"\bzo+ey? ?de(?:p|pth)s?\b": "ZoeDepth?",
    r"\b(?:bad|bev) fusion\b": "BEVFusion?",
}


# --- io ---------------------------------------------------------------------


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, tx):
    Path(path).write_text(
        json.dumps(tx, indent=1, ensure_ascii=False), encoding="utf-8")


class MarkdownConflict(ValueError):
    """The Markdown contains changes absent from the saved transcript."""

    def __init__(self, path, contents):
        self.overwrite_token = hashlib.sha256(contents.encode("utf-8")).hexdigest()
        super().__init__(
            f"Regenerating {path.name} will overwrite your Markdown edits with "
            "the saved transcript. Save a copy of your work elsewhere before "
            "continuing. You will need to reapply those corrections manually.")


def check_markdown(tx_path, expected, overwrite_token=None):
    """Require confirmation for the current contents before overwriting edits."""
    path = Path(tx_path)
    md_path = path.with_name(path.name.removesuffix(".tx.json") + ".md")
    if md_path.exists():
        contents = md_path.read_text(encoding="utf-8")
        if contents != expected:
            conflict = MarkdownConflict(md_path, contents)
            if overwrite_token != conflict.overwrite_token:
                raise conflict
            return True
    return False


def hms(seconds):
    s = int(seconds)
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def dur(seconds):
    s = int(seconds)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def speaker_name(tx, cluster_id):
    return (tx.get("speakers") or {}).get(cluster_id, cluster_id)


# --- flagging ---------------------------------------------------------------
#
# What NOT to use: WhisperX's per-word `score`. It is a wav2vec2 forced-
# alignment posterior, not an ASR confidence — short function words score near
# zero when they are perfectly correct. Flagging on it selected 37% of one
# interview, ranked by how many articles a sentence contained.
#
# What is used instead, in descending order of how well it predicts an actual
# error: disagreement with a second ASR model over the same span; Whisper's own
# segment avg_logprob, ranked *within the file* because recordings differ by
# more than segments within one recording do; and a hand-kept suspect list.


def _norm_words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def similarity(text_a, text_b):
    """Symmetric word-level agreement, 0.0-1.0. Used for repeat detection."""
    a, b = _norm_words(text_a), _norm_words(text_b)
    if not a or not b:
        return 0.0
    matched = sum(block.size for block in
                  difflib.SequenceMatcher(a=a, b=b, autojunk=False)
                  .get_matching_blocks())
    return (2.0 * matched) / (len(a) + len(b))


# Words too common to carry meaning. An unmatched one of these is noise; an
# unmatched content word is a candidate mishearing.
STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does for from had has
have he her here his how i if in is it its just like me my no not of on or out
she should so some that the their them then there these they this to up was we
were what when where which who will with would you your yeah yes um uh okay
really very about got kind sort thing things going
""".split())

DISAGREE_MISS = 0.4     # fraction of a segment's words the other model missed
DISAGREE_WORDS = 2      # ...or this many unmatched content words

# How far the second model's placement of the same words may sit from our own
# before the segment's timing is called into question. Forced alignment mostly
# agrees with an independent model to within 30ms across a 90-minute recording,
# so a whole second apart is not drift — it is one of them being wrong about
# where the words are, and playing the segment then lands on the wrong audio.
TIMING_DRIFT = 1.0      # ...taken over the whole segment
TIMING_RUN_DRIFT = 0.5  # ...or sustained across a run of consecutive words,
TIMING_RUN_WORDS = 3    #    which is what tells drift from a one-word spike
TIMING_WORDS = 2        # too few matched words to trust the comparison


def timing_drift(seg):
    """How far this segment's audio position is disputed, in seconds.

    Read from seg["drift"], computed by the pipeline over word pairs the two
    models actually agreed on. Returns None when there was nothing to compare
    or nothing is wrong.
    """
    drift = seg.get("drift") or {}
    dt = drift.get("dt")
    return dt if isinstance(dt, (int, float)) else None


def align_disagreement(seg):
    """(miss_rate, unmatched content words) from the stored global alignment.

    The alignment itself is computed once per file by build_tx.py against the
    whole word stream, NOT segment by segment against a time window. That
    matters: when the two models genuinely disagree over a stretch, one falls
    behind the other by seconds, and a time-windowed comparison then flags
    every *correct* segment after it until they resync. Measured on ip1, that
    cascade produced three flags where one was warranted. Sequence alignment
    has no such failure mode — it matches on order, not on the clock.

    Returns (None, []) when there is nothing to compare.
    """
    align = seg.get("align")
    if not align or not align.get("n"):
        return None, []
    if align["n"] < MIN_SEG_WORDS:
        return None, []
    return align["miss"] / align["n"], list(align.get("words") or [])


def suspect_notes(text):
    return [note for pattern, note in SUSPECT.items()
            if re.search(pattern, text or "", flags=re.IGNORECASE)]


def _repeat_ids(segments, window=4):
    """Segments that repeat a near-identical neighbour.

    Whisper loops: on low-information audio the decoder latches onto a phrase
    and re-emits it for minutes. The repeats are individually fluent, so no
    confidence signal catches them — but they are trivially visible as text.
    """
    flagged = set()
    for i, seg in enumerate(segments):
        words = _norm_words(seg.get("text"))
        if len(words) < MIN_SEG_WORDS:
            continue
        for other in segments[max(0, i - window):i]:
            if similarity(seg.get("text"), other.get("text")) > 0.85:
                flagged.add(id(seg))
                flagged.add(id(other))
                break
    return flagged


def flag(tx, budget=REVIEW_BUDGET):
    """Compute risk/flags per segment. Returns the number flagged.

    Ranking is within-file: the queue is "the worst `budget` of this
    recording", which is the shape a reviewer's time budget actually has. A
    fixed logprob cutoff cannot do that — across these interviews the median
    segment logprob ranges from -0.11 to -0.22, so any absolute threshold
    flags everything in the worst recording and nothing in the best.
    """
    segments = tx.get("segments", [])
    if not segments:
        return 0

    scored = [s for s in segments
              if isinstance(s.get("logprob"), (int, float))
              and len(_norm_words(s.get("orig") or s.get("text"))) >= MIN_SEG_WORDS]
    ordered = sorted(scored, key=lambda s: s["logprob"])
    # rank 0.0 = worst logprob in this file, 1.0 = best
    rank = {id(s): (i / max(1, len(ordered) - 1)) for i, s in enumerate(ordered)}

    repeats = _repeat_ids(segments)

    for seg in segments:
        text = seg.get("text") or ""
        notes = suspect_notes(text)
        dis, odd_words = align_disagreement(seg)

        parts, flags = [], []
        if id(seg) in rank:
            parts.append(1.0 - rank[id(seg)])
        if dis is not None:
            parts.append(dis)
            # Two ways to disagree that matter: the other model missed most of
            # the segment, or it missed specific content words. The second
            # catches a one-word substitution in a long sentence, which barely
            # moves a rate.
            if dis > DISAGREE_MISS or len(odd_words) >= DISAGREE_WORDS:
                flags.append("disagree")
                seg["odd"] = odd_words[:6]
        if id(seg) in repeats:
            flags.append("repeat")
        if timing_drift(seg) is not None:
            flags.append("timing")
        if seg.get("speaker_suggestion"):
            flags.append("speaker")
        seg["risk"] = round(sum(parts) / len(parts), 4) if parts else 0.0
        seg["notes"] = notes
        seg["flags"] = flags

    # Take the worst `budget` by risk, then always add suspect-term hits and
    # hard disagreements however they ranked.
    ranked = sorted(segments, key=lambda s: -s.get("risk", 0.0))
    cutoff = max(1, int(round(len(segments) * budget)))
    queued = {id(s) for s in ranked[:cutoff]}
    for seg in segments:
        if seg.get("notes") or {"disagree", "repeat", "timing", "speaker"} & set(seg.get("flags", [])):
            queued.add(id(seg))

    for seg in segments:
        if id(seg) in queued:
            if "queue" not in seg["flags"]:
                seg["flags"].insert(0, "queue")
        seg.setdefault("status", "unreviewed")
    return sum(1 for s in segments if "queue" in s.get("flags", []))


def queue(tx):
    """Flagged segments, worst first — segments already reviewed drop out."""
    return sorted(
        (s for s in tx.get("segments", [])
         if "queue" in s.get("flags", []) and
         (s.get("status") == "unreviewed" or s.get("speaker_suggestion"))),
        key=lambda s: -s.get("risk", 0.0))


# --- rendering --------------------------------------------------------------


def chunks(tx):
    """The .md as an ordered list of (kind, key, text).

    kind "seg" carries a segment id and is the only text a human edits; kind
    "lit" is scaffolding (headers, speaker labels, pause markers, blank lines)
    derived from segment metadata. Rendering to a string and mapping character
    offsets both work off this one list, so they cannot drift apart.
    """
    out = [("lit", "head", f"# {tx.get('title', 'transcript')}\n\n")]
    prev_end = None
    prev_speaker = None
    for seg in tx.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        gap = seg["start"] - prev_end if prev_end is not None else 0.0
        new_turn = (prev_end is None
                    or seg.get("speaker") != prev_speaker
                    or gap >= PAUSE_THRESHOLD)
        if new_turn:
            if prev_end is not None:
                out.append(("lit", "turn-end", "\n\n"))
                if gap >= PAUSE_THRESHOLD:
                    out.append(("lit", "pause", f"*[pause — {dur(gap)}]*\n\n"))
            label = speaker_name(tx, seg.get("speaker", "UNKNOWN"))
            out.append(("lit", "label",
                        f"**[{hms(seg['start'])}] {label}:** "))
        else:
            out.append(("lit", "join", " "))
        out.append(("seg", seg["id"], text))
        prev_end, prev_speaker = seg["end"], seg.get("speaker")
    out.append(("lit", "tail", "\n"))
    return out


def render_md(tx):
    """-> (markdown, {segment id: (start, end)} character spans)."""
    parts, spans, pos = [], {}, 0
    for kind, key, text in chunks(tx):
        if kind == "seg":
            spans[key] = (pos, pos + len(text))
        parts.append(text)
        pos += len(text)
    return "".join(parts), spans


# --- editing ----------------------------------------------------------------


def find_segment(tx, seg_id):
    for seg in tx.get("segments", []):
        if seg["id"] == seg_id:
            return seg
    return None


def apply_edit(tx, seg_id, new_text, author="", method="human"):
    """Record a correction. Returns the edit, or None if nothing changed.

    The edit log is append-only and is two things at once: the audit trail an
    IRB protocol wants, and the (hypothesis, truth) corpus that feeds keyterm
    biasing and, eventually, a fine-tune. Neither works if a correction is
    applied silently in place.
    """
    seg = find_segment(tx, seg_id)
    if seg is None:
        return None
    before = seg.get("text") or ""
    after = (new_text or "").strip()
    if after == before:
        return None
    seg["text"] = after
    seg["status"] = "edited"
    # Word timings described the ASR hypothesis. Keep them for playback (the
    # span is still right) but mark them as no longer aligned to the text.
    if after != (seg.get("orig") or ""):
        seg["words_stale"] = True
    else:
        seg.pop("words_stale", None)
    edit = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "seg": seg_id,
        # Which property was corrected. Entries written before speaker editing
        # existed have no "field" and are all text edits, so read it as
        # edit.get("field", "text") — the (hypothesis, truth) corpus that feeds
        # keyterm biasing must not pick up speaker changes as transcript pairs.
        "field": "text",
        "t0": seg["start"],
        "t1": seg["end"],
        "before": before,
        "after": after,
        "by": author,
        "method": method,
    }
    tx.setdefault("edits", []).append(edit)
    return edit


def add_speaker(tx, name):
    """Register a speaker who is not in the diarization. Returns their key.

    Diarization can miss a person entirely — someone who speaks twice in ninety
    minutes may never get a cluster of their own. Manual keys are minted in
    their own MANUAL_nn namespace so they are never confused with something
    pyannote actually found.
    """
    name = (name or "").strip()
    if not name:
        return None
    speakers = tx.setdefault("speakers", {})
    for key, existing in speakers.items():
        if existing.casefold() == name.casefold():
            return key
    n = 0
    while f"MANUAL_{n:02d}" in speakers:
        n += 1
    key = f"MANUAL_{n:02d}"
    speakers[key] = name
    return key


def set_speaker(tx, seg_id, cluster_id, author="", method="human"):
    """Reassign one segment to a different speaker. Returns the edit, or None.

    Only this segment moves. A whole cluster that carries the wrong name is a
    different fix — rename it in tx["speakers"] — and doing it one segment at a
    time would be both tedious and a lie about what went wrong.
    """
    seg = find_segment(tx, seg_id)
    if seg is None or cluster_id not in (tx.get("speakers") or {}):
        return None
    before = seg.get("speaker")
    if cluster_id == before:
        return None
    seg["speaker"] = cluster_id
    seg["status"] = "edited"
    clear_speaker_suggestion(seg)
    edit = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "seg": seg_id,
        "field": "speaker",
        "t0": seg["start"],
        "t1": seg["end"],
        "before": speaker_name(tx, before),
        "after": speaker_name(tx, cluster_id),
        "by": author,
        "method": method,
    }
    tx.setdefault("edits", []).append(edit)
    return edit


def mark_reviewed(tx, seg_id, status="ok", author=""):
    seg = find_segment(tx, seg_id)
    if seg is None:
        return False
    if status == 'ok' and seg.get('speaker_suggestion'):
        review_speaker(tx, seg_id, 'keep', seg['speaker_suggestion'], author=author)
    seg["status"] = status
    return True


def review_speaker(tx, seg_id, action, expected, author=""):
    """Resolve a suggestion explicitly; reject a stale or conflicting decision."""
    seg = find_segment(tx, seg_id)
    suggestion = (seg or {}).get('speaker_suggestion') or {}
    target = expected.get('speaker') if isinstance(expected, dict) else None
    protected = any(e.get('seg') == seg_id and e.get('field') in ('speaker', 'speaker-review')
                    for e in tx.get('edits', []))
    if (not seg or action not in ('accept', 'keep') or protected or not target
            or target not in tx.get('speakers', {})
            or any(suggestion.get(k) != expected.get(k) for k in ('speaker', 'start', 'end'))
            or suggestion.get('start') != seg.get('start')
            or suggestion.get('end') != seg.get('end')):
        raise ValueError('This speaker suggestion changed. Reload the transcript before deciding.')
    if action == 'accept':
        edit = set_speaker(tx, seg_id, target, author=author)
    else:
        edit = {
            'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'seg': seg_id, 'field': 'speaker-review', 'action': 'keep',
            't0': seg['start'], 't1': seg['end'],
            'before': speaker_name(tx, seg['speaker']),
            'after': speaker_name(tx, seg['speaker']),
            'suggested': speaker_name(tx, target), 'by': author, 'method': 'human',
        }
        tx.setdefault('edits', []).append(edit)
    clear_speaker_suggestion(seg)
    return edit


def clear_speaker_suggestion(seg):
    seg.pop('speaker_suggestion', None)
    if 'flags' in seg:
        seg['flags'] = [f for f in seg['flags'] if f != 'speaker']


# --- annotation remapping ---------------------------------------------------


def remap_annotations(annotations, old_spans, new_spans, old_md, new_md,
                      changed_segments):
    """Move code annotations onto the re-rendered .md.

    Annotations are character offsets into the .md, so any transcript edit
    shifts every offset after it. Segment spans are the anchors: an annotation
    inside an unchanged segment moves with that segment exactly. One that
    overlaps edited text cannot be moved honestly, so it is marked `stale`
    instead of being silently relocated onto words nobody chose.

    Returns (annotations, moved, stale).
    """
    changed = set(changed_segments)
    ordered = sorted(old_spans.items(), key=lambda kv: kv[1][0])
    moved = stale = 0

    def locate(offset):
        """-> (segment id, offset within segment) for an offset in old_md."""
        for seg_id, (start, end) in ordered:
            if start <= offset <= end:
                return seg_id, offset - start
        return None, None

    for ann in annotations:
        start, end = ann.get("start"), ann.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        # Already correct? Cheapest and most common case after a no-op render.
        if old_md == new_md:
            continue
        a_seg, a_off = locate(start)
        b_seg, b_off = locate(end)
        if a_seg is None or b_seg is None:
            ann["stale"] = True
            stale += 1
            continue
        if a_seg in changed or b_seg in changed:
            ann["stale"] = True
            stale += 1
            continue
        if a_seg not in new_spans or b_seg not in new_spans:
            ann["stale"] = True
            stale += 1
            continue
        new_start = new_spans[a_seg][0] + a_off
        new_end = new_spans[b_seg][0] + b_off
        if new_md[new_start:new_end] != old_md[start:end]:
            ann["stale"] = True
            stale += 1
            continue
        if (new_start, new_end) != (start, end):
            ann["start"], ann["end"] = new_start, new_end
            moved += 1
        ann.pop("stale", None)
    return annotations, moved, stale

# Verifying transcripts against audio

Design notes for the audio-linked verification view: what the keyboard controls
are, how the highlight is kept in sync with the audio, and why the pipeline
makes the choices it does. For getting started, see the
[README](../README.md).

## The review view

If a transcript has a `<name>.tx.json` beside it (produced by
`bescribe/pipeline/build_tx.py`), the file list shows a ♪ and the file view
gets a **Verify against audio** button.
The ♪ is dark blue when the playback audio is available on this computer and
gray when it is missing. Hover over the note to see its status.

The view is the **whole interview in time order**, with the ~15% of segments
most likely to contain an error tinted amber and marked on a timeline strip
across the top. You move between the flagged ones by jumping, not by filtering.

That is deliberate. An error is usually only recognisable from its
surroundings — whether "common filter" should be "Kalman filter" is settled by
the sentence before it — and a list sorted worst-first strands every snippet
away from the conversation it belongs to. Playing one then runs on into a
segment from somewhere else entirely. So risk ranking decides *which* segments
are flagged and prints as a `#n` badge on each; it never decides where they
appear.

| | |
|---|---|
| **Next ⟩** / **n**, **⟨ Previous** / **p** | next / previous segment in the selected group (Needs review, Edited, or All) |
| the timeline strip | click to seek; ticks are flags, grey once handled |
| click a word | seek there and play |
| ▶ on a row | replay that segment |
| **loop** | repeat the current segment until you turn it off |
| double-click a word | select that word for retyping, and play from it |
| double-click elsewhere in the text, or **E** | correct it in place |
| click the speaker name, or **S** | reassign that segment to another speaker |
| **✓**, or Enter outside the editor | confirm as correct, and play the next segment in the selected group |
| Enter / Esc in the text editor | save / cancel the correction |
| ↑ ↓ | previous / next segment (adjacent, not next flag) |
| space | play / pause |
| `[` `]` | slower / faster (0.5×–1.5×) |

### Staying in sync with the audio

Two things pull the highlight away from what you hear, and both are handled.

A segment plays with a little lead-in so its first phoneme is not clipped, but
that pad is clamped to the actual silence before it rather than being a fixed
150 ms — 29% of segments in this corpus have a predecessor ending within 150 ms
(10th-percentile gap: 40 ms), so a constant pad reaches back into the previous
speaker and you hear their tail before your text starts. Same clamp on the way
out, against the next segment's first word.

The playhead is read on every animation frame, not on the media element's
`timeupdate` event. Measured in Chrome with audio playing, `timeupdate` fires
every **266 ms** against `requestAnimationFrame`'s **17 ms** — so a word used to
stay lit up to a quarter-second after it was spoken, and a looping segment ran a
quarter-second into the next one before rewinding.

The Opus render is not a source of drift: `ffprobe` reports `start_time=0` and a
duration within 6.5 ms of the source WAV (Opus end-frame padding), so timings
taken from the WAV land correctly on it.

What is left is forced alignment slipping — and the cause is structural.
WhisperX aligns each segment *inside that segment's own time window*:

```python
f1 = int(t1 * SAMPLE_RATE); f2 = int(t2 * SAMPLE_RATE)
waveform_segment = audio[:, f1:f2]          # whisperx/alignment.py
```

So forced alignment can only redistribute words within a window Whisper already
chose. When Whisper's timestamp is wrong the words cannot escape it. On ip1,
s00019-s00020 were packed into 65.9-75.4 s while the speech they transcribe runs
to 88 s, leaving 12.5 seconds of continuous audio with no word assigned to it,
and a segment that played ten seconds from where its words were spoken.

`pipeline/realign.py` fixes this by re-aligning between **anchors** — words
where WhisperX and the second model place the same token within 150 ms. The two
agree to a 30 ms median across 90 minutes, so where they agree the time is not
in question; everything between two anchors is re-aligned as one block, and a
word can move anywhere inside it. The second model chooses boundaries only. It
is *not* a better clock: by the fraction of word onsets landing in silence it is
worse (19.0% against WhisperX's 8.1%), and an earlier attempt to re-time
everything onto it made three of six files worse. That approach was abandoned.

Re-alignment is a proposal, not a verdict. `pipeline/apply_timings.py` scores
every segment's old and new placement by how many of its word onsets land on a
frame containing speech, and keeps the new times only if they are not
substantially worse — it rejects a drop of more than a third, or any move into a
stretch that is under 30% speech. Unrefereed, re-alignment moved ip3's "Why do
you create the mask?" 47 s into a region that is 4.8% speech. With the referee,
roughly 4-8 segments per file are rejected and the rest accepted.

The result leaves 96% of words alone (median movement 3 ms) and moves the ~4%
that were wrong. Both cases with independent ground truth come out right: ip1's
s00020 moves +10.4 s to 82.8 s, where the energy profile shows the speech, and
s00274 moves +1.04 s to 23:15, which a human confirmed by ear.

Timings are carried onto a reviewed transcript by `apply_timings.py`, never by
re-running `build_tx.py`, which would reset the edit log. Any segment that moves
more than 0.5 s and had been reviewed goes back to `unreviewed`: a judgement made
while the wrong audio was playing is not evidence about that segment. Its text is
left alone — being back in the queue already forces a second look, and a fix like
"Rust bags" -> "rosbags" is right whatever was in the speakers. `--revert-text`
restores the machine's wording too.

### The second opinion line

The `2nd:` line under a segment is the other model's words *for that segment's
words*, taken from the same alignment. It used to be a ±3 s time slice, which
is wider than most segments (median duration ~2 s): the median alt line ran
**2.6× the length of its own segment and 40% ran over 3×**, so it routinely
showed the neighbours' speech and read as an offset. Now the median is 1.0×
and nothing exceeds 3×.

Diarization sometimes puts one utterance on the wrong voice cluster. Clicking
the speaker name moves **that segment alone**, which splits the surrounding
turn in the `.md` — the text and word timings are untouched, so playback and
click-to-hear keep working. `someone else…` names a person pyannote never gave
a cluster to at all; they get a `MANUAL_nn` key so a hand-added speaker is
never mistaken for something the model found. A whole cluster wearing the wrong
name is a different mistake and wants a rename in `tx["speakers"]`, not eight
hundred of these.

Speaker changes go in the same append-only edit log as text corrections but are
tagged `"field": "speaker"`. Read the log as `edit.get("field", "text")` —
entries written before this existed have no `field` and are all text edits —
so the (hypothesis, truth) corpus for keyterm biasing never picks up a
diarization fix as a transcript pair.

New recordings retain the original speaker intervals, the timestamps used to
assign each segment, and machine speaker labels for individual words. Timing
repairs reuse those intervals to suggest a different speaker; they never change
the current speaker automatically. Words keep their original machine labels as
evidence, even when a human changes the segment's speaker.

When a suggestion is available, the row shows **Check speaker** with **Use
suggested speaker** and **Keep current speaker**. Listen before choosing. Accepting
uses the normal speaker-edit log and annotation remapping. Keeping records a
`"field": "speaker-review"` decision; confirming the row with ✓ also keeps its
current speaker. These decisions and previous speaker corrections are protected
from future suggestions. Pending suggestions appear in **Needs review**, including
on previously reviewed rows, without changing their saved review status.

Suggestions require a timestamp movement over 0.5 seconds, a changed assignment,
at least 50% speech coverage, and a 60% share of speaker overlap. Ties and sparse
evidence abstain. These measures are not speaker-confidence probabilities.
Older transcripts without saved intervals still work but cannot produce these
suggestions. Do not rebuild reviewed transcripts to obtain the missing evidence.

Confirming and correcting update the row where it stands: nothing is removed
from the list and nothing is re-sorted. ✓ then advances to the next segment
in the selected group, so the review loop is listen, judge, ✓ — the only movement
is that deliberate one, and it stops at the last matching segment rather than
wrapping back to the top. The scope
buttons (`Needs review` / `Edited` / `All`) choose what ⟨ ⟩ steps between and
carry the remaining counts; they do not hide anything. Otherwise the page only
scrolls when playback carries the transcript off screen — and never while you
are typing a correction or picking a speaker.

`build_tx.py` builds a transcript from scratch — `"edits": []`, every segment
`unreviewed` — so re-running it with `--install` over a transcript you have
been correcting would destroy that work, and the `.tx.json` is the only place
it lives. It now refuses, naming what would be lost. To carry a pipeline
improvement onto a reviewed file, use `pipeline/remap_alt.py`, which recomputes
only the second-opinion fields and asserts that text, status, the edit log and
the rendered `.md` all come out unchanged.

### Switching without losing your place

The two views are two renderings of one interview, so switching between them
does not move you. Both scroll inside the same pane, and both can say where you
are as a character offset into the `.md`: the coding view reads it off its own
text nodes, and each segment carries the `span` the server rendered the `.md`
from. Going in, the line at the top of the reading area picks the segment the
list opens on — anchored, not played, and made the current segment so **⟩**
steps on from where you were reading rather than from the top of the file.
Coming back, the topmost row hands its span to the coding view, which puts that
text on the same line of the screen. A correction re-renders the whole `.md`
and moves every later offset, so the edit response carries the new spans back
with it.

Landing on the right line is not enough on its own. The same passage is a
paragraph of prose on one side and a stack of timed rows on the other, and
nothing about the two says they are the same words — so whichever view you
arrive in washes the segment you arrived on in blue for a second and a half,
the row on one side and the words themselves on the other. The bar names the
timestamp you picked up at, which is the one label both views print.

Both bars — the file bar with **Verify against audio**, the playback bar with
**Back to coding** — are pinned to the top of the pane for the same reason. A control you
have to scroll to the top of a ninety-minute transcript to reach has already
lost you your place before you press it.

Corrections are written to the `.tx.json` edit log, never to the `.md` directly.
The server re-renders the `.md` and moves existing code annotations onto it in
the same request. An annotation whose own text you edited is marked `stale`
rather than silently relocated onto words nobody chose — the coding view still
shows it, and you re-check it.

### Audio

If you have the original recording and a saved transcript, follow the
[playback setup instructions](../README.md#reviewing-an-existing-transcript-with-matching-audio).
`make prepare AUDIO=...` reuses the transcript and creates the playback file
with ffmpeg; model installation and a token are unnecessary for this case.

Audio is not in this repo and should not be. Put a browser-playable render in
`md/audio/`, or point the app at approved storage:

```
QDA_AUDIO_DIR=/path/to/irb/storage python3 app.py
```

Symlinks into that storage work. Without audio, the view still opens and text
editing still works — only playback is unavailable. Reopen **Verify against
audio** after adding the file so the app checks for it again.

`bescribe/pipeline/transcribe.sh` writes a 32 kbps Opus render (~10 MB per 90
minutes) into `bescribe/data/raw/` alongside its JSON; copying or symlinking
those into `md/audio/` is the last step of that pipeline. See
[bescribe/README.md](../bescribe/README.md) for the whole path from a recording to
a verifiable transcript.

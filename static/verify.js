// verify.js — audio-linked transcript verification.
//
// The transcript here is a view of <name>.tx.json, not of the .md: every
// segment keeps the word timings WhisperX aligned, so a click on a word can
// seek to that word. Edits POST back to the server, which re-renders the .md
// and moves code annotations onto it — this module never writes the .md.
//
// The list is always the whole interview in time order. Risk ranking decides
// *which* segments get flagged, never where they appear: a review list sorted
// worst-first strands each snippet with no neighbours, and audio played from
// one runs on into a segment that is somewhere else entirely. Flags are found
// by jumping (⟨ ⟩ / n p), so you keep the conversation around them.

import {BASE, apiFetch, apiPost, apiUrl, sessionInfo, toSignIn} from './api.js';
import {PAST, markPlace, placeAt, readingLine} from './place.js';
import {esc} from './state.js';

const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5];
// Most a segment's playback may reach outside its own word timings, so the
// first phoneme is not clipped off the front. It is a ceiling, not a constant:
// see bounds().
const SEG_PAD = 0.15;
const SCOPES = [
  {id: 'queue', label: 'Needs review', match: s => (s.flags || []).includes('queue') && (s.status === 'unreviewed' || s.speaker_suggestion)},
  {id: 'edited', label: 'Edited', match: s => s.status === 'edited'},
  {id: 'all', label: 'All', match: () => true}
];

const view = {
  tx: null,
  file: null,
  scope: 'queue',     // what ⟨ ⟩ and n/p step between
  loop: false,
  speedIndex: 2,
  activeId: null,     // segment the playhead is inside
  playingId: null,    // segment whose ▶ is currently showing as pause
  boundedId: null,    // segment playback is confined to, when looping
  editingId: null,
  speakerId: null,    // segment whose speaker picker is open
  rows: new Map(),    // segment id -> row element
  ticks: new Map(),   // segment id -> tick element on the timeline
  byId: new Map(),
  rank: new Map(),    // segment id -> 1-based rank by risk, for flagged only
  order: new Map(),   // segment id -> position, for neighbour lookups
  words: new Map(),   // segment id -> [{el, t0, t1}], rebuilt per render
  dirty: false        // an edit landed, so the .md needs reloading on exit
};

let audio = null;
let root = null;
let onExit = () => {};
let session = 0; // Ignore responses from a transcript we have already left.

// --- helpers ----------------------------------------------------------------

function hms(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, '0');
  return `${h ? `${h}:` : ''}${mm}:${String(sec).padStart(2, '0')}`;
}

function speakerName(clusterId) {
  return (view.tx?.speakers || {})[clusterId] || clusterId;
}

function segments() {
  return view.tx?.segments || [];
}

function duration() {
  return (canPlay() && Number.isFinite(audio.duration) && audio.duration)
    || view.tx?.duration || 0;
}

function canPlay() {
  return audio && !root.hidden && view.tx?.audioAvailable && audio.hasAttribute('src');
}

function resetAudio() {
  view.boundedId = null;
  if (audio) {
    audio.pause();
    audio.removeAttribute('src');
    // Removing src alone leaves the previous media resource playable.
    audio.load();
  }
  if (frame) {
    cancelAnimationFrame(frame);
    frame = 0;
  }
  highlightWord(null, 0);
  markPlaying();
  clockSecond = -1;
  root.querySelector('#vf-playpause').textContent = '▶';
  updateClock();
}

function updatePlaybackControls() {
  for (const button of root.querySelectorAll('#vf-playpause, #vf-loop, #vf-speed, .vf-play')) {
    button.disabled = !canPlay();
  }
  root.querySelector('#vf-track').setAttribute('aria-disabled', String(!canPlay()));
}

function isFlagged(segment) {
  return (segment.flags || []).includes('queue');
}

// Segments the jump controls step between, in time order.
function scopeList() {
  const scope = SCOPES.find(s => s.id === view.scope) || SCOPES[0];
  return segments().filter(scope.match);
}

function counts() {
  const all = segments();
  const out = {};
  for (const scope of SCOPES) {
    out[scope.id] = all.filter(scope.match).length;
  }
  return out;
}

// --- playback ---------------------------------------------------------------

// Where a segment's playback actually starts and stops. A fixed 150ms lead-in
// sounds wrong on a third of this corpus: 29% of segments have a predecessor
// ending within 150ms of them (10th percentile gap: 40ms), so a constant pad
// reaches back into the previous speaker and you hear their tail before your
// text begins. Clamp to the real silence on each side instead.
function bounds(segment) {
  const list = segments();
  const i = view.order.get(segment.id) ?? -1;
  const prev = i > 0 ? list[i - 1] : null;
  const next = i >= 0 && i + 1 < list.length ? list[i + 1] : null;
  const before = prev ? segment.start - prev.end : segment.start;
  const after = next ? next.start - segment.end : SEG_PAD;
  return {
    from: Math.max(0, segment.start - Math.min(SEG_PAD, Math.max(0, before))),
    to: segment.end + Math.min(SEG_PAD, Math.max(0, after))
  };
}

function play(segment, {bound = true, scroll = true} = {}) {
  if (!segment || view.byId.get(segment.id) !== segment) {
    return;
  }
  setActive(segment.id, scroll ? 'center' : false);
  if (!canPlay()) {
    return;
  }
  view.boundedId = bound ? segment.id : null;
  audio.currentTime = bounds(segment).from;
  audio.play().catch(() => {});
}

function seekTo(time, segment) {
  if (!canPlay()) {
    return;
  }
  view.boundedId = segment ? segment.id : null;
  audio.currentTime = Math.max(0, time);
  audio.play().catch(() => {});
}

function togglePlay() {
  if (!canPlay()) {
    return;
  }
  if (audio.paused) {
    audio.play().catch(() => {});
  } else {
    audio.pause();
  }
}

function segmentAt(time) {
  const list = segments();
  let lo = 0;
  let hi = list.length - 1;
  let found = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].start <= time) {
      found = list[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found && time <= found.end + 0.5 ? found : null;
}

// The playhead is read on every animation frame, not on `timeupdate`. Measured
// in Chrome with audio actually playing, `timeupdate` fires every 266ms — so a
// word stayed lit up to a quarter-second after it had been spoken, and a
// looping segment ran a quarter-second into the next one before rewinding.
// requestAnimationFrame is 17ms, below the threshold where either is audible.
let frame = 0;

function pump() {
  frame = 0;
  tick();
  if (audio && !audio.paused) {
    frame = requestAnimationFrame(pump);
  }
}

function startPump() {
  if (!frame) {
    frame = requestAnimationFrame(pump);
  }
}

let ticking = false;

function tick() {
  // pause() below fires a `pause` event, which is bound back to tick(). Guard
  // the re-entry rather than relying on that event being asynchronous.
  if (ticking) {
    return;
  }
  ticking = true;
  try {
    tickOnce();
  } finally {
    ticking = false;
  }
}

function tickOnce() {
  if (!canPlay()) {
    return;
  }
  const time = audio.currentTime;
  const bounded = view.boundedId ? view.byId.get(view.boundedId) : null;

  if (bounded && time >= bounds(bounded).to) {
    if (view.loop) {
      audio.currentTime = bounds(bounded).from;
      return;      // stay on this segment; this is the "again and again" case
    }
    view.boundedId = null;   // cleared before pausing, not after
    audio.pause();
  }

  const current = bounded || segmentAt(time);
  if (current && current.id !== view.activeId) {
    setActive(current.id, bounded ? false : 'follow');
  }
  highlightWord(current, time);
  markPlaying();
  updateClock();
}

// Is the row far enough out of view that following the playhead is a help
// rather than a shove? Scrolling a row that is already on screen is what makes
// the page feel like it is yanking itself around under you.
function needsScroll(row) {
  const box = row.getBoundingClientRect();
  const top = (root.querySelector('.vf-header')?.getBoundingClientRect().bottom) || 0;
  return box.top < top + 8 || box.bottom > window.innerHeight - 8;
}

// mode: false (never scroll) | 'follow' (only if off screen) | 'center'.
function setActive(segmentId, mode) {
  if (view.activeId && view.rows.has(view.activeId)) {
    view.rows.get(view.activeId).classList.remove('vf-active');
  }
  view.activeId = segmentId;
  const row = view.rows.get(segmentId);
  if (!row) {
    return;
  }
  row.classList.add('vf-active');
  // Never move the page out from under someone who is typing a correction.
  if (!mode || view.editingId || view.speakerId) {
    return;
  }
  if (mode === 'center') {
    row.scrollIntoView({block: 'center', behavior: 'smooth'});
  } else if (needsScroll(row)) {
    row.scrollIntoView({block: 'center', behavior: 'smooth'});
  }
}

// Word spans and their times, read from the DOM once per render rather than
// once per frame.
function wordsOf(segmentId) {
  let list = view.words.get(segmentId);
  if (!list) {
    const row = view.rows.get(segmentId);
    if (!row) {
      return [];
    }
    list = [...row.querySelectorAll('.vf-word')].map(el => ({
      el, t0: Number(el.dataset.t0), t1: Number(el.dataset.t1)
    }));
    view.words.set(segmentId, list);
  }
  return list;
}

let lit = null;

// The row button doubles as the transport for whatever is playing: while a
// segment is sounding, its ▶ becomes ❚❚ and stops it. Without this the only
// indication that audio is running is the bar at the top of the page, which is
// nowhere near the row you are actually looking at.
function markPlaying() {
  const id = audio && !audio.paused ? view.activeId : null;
  if (id === view.playingId) {
    return;
  }
  for (const [segId, glyph, on] of [[view.playingId, '▶', false], [id, '❚❚', true]]) {
    const button = segId && view.rows.get(segId)?.querySelector('.vf-play');
    if (button) {
      button.textContent = glyph;
      button.classList.toggle('vf-play-on', on);
      button.title = on ? 'Pause' : `Play this segment (loop is ${view.loop ? 'on' : 'off'})`;
    }
  }
  view.playingId = id;
}

function highlightWord(segment, time) {
  let next = null;
  if (segment) {
    for (const word of wordsOf(segment.id)) {
      if (time >= word.t0 && time <= word.t1) {
        next = word.el;
        break;
      }
    }
  }
  if (lit === next) {
    return;
  }
  if (lit) {
    lit.classList.remove('vf-now');
  }
  if (next) {
    next.classList.add('vf-now');
  }
  lit = next;
}

function setSpeed(index) {
  view.speedIndex = Math.max(0, Math.min(SPEEDS.length - 1, index));
  if (audio) {
    audio.playbackRate = SPEEDS[view.speedIndex];
  }
  const button = root.querySelector('#vf-speed');
  if (button) {
    button.textContent = `${SPEEDS[view.speedIndex]}×`;
  }
}

let clockSecond = -1;

function updateClock() {
  const total = duration();
  const now = canPlay() ? audio.currentTime : 0;
  const clock = root.querySelector('#vf-clock');
  // Runs 60 times a second now, and this text changes once a second.
  if (clock && Math.floor(now) !== clockSecond) {
    clockSecond = Math.floor(now);
    clock.textContent = `${hms(now)} / ${hms(total)}`;
  }
  const head = root.querySelector('#vf-head');
  if (head) {
    head.style.left = `${total ? Math.min(100, (now / total) * 100) : 0}%`;
  }
}

// --- keeping your place -----------------------------------------------------
// Every segment carries `span`: where its text sits in the rendered .md. That
// is the coding view's coordinate system too, so it is what the two views
// trade when you switch (see place.js).

function readingLineHere() {
  return readingLine(root.querySelector('.vf-header'));
}

// The segment a .md offset falls in — or, when it lands on scaffolding the
// segments do not own (a speaker label, a pause marker), the one that starts
// just after it.
function segmentAtOffset(offset) {
  let last = null;
  for (const segment of segments()) {
    if (!segment.span) {
      continue;
    }
    if (offset <= segment.span[1]) {
      return segment;
    }
    last = segment;
  }
  return last;
}

// The .md span of the topmost row still on screen: where the reader is, and
// which words the coding view should mark when it gets there.
function readingSpanHere() {
  const list = segments();
  if (!list.length || !view.rows.size) {
    return null;
  }
  const y = readingLineHere();
  // Rows are laid out in time order, so their bottoms only grow down the page.
  // See PAST: the row that ends on the reading line is the one above this.
  let lo = 0;
  let hi = list.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const row = view.rows.get(list[mid].id);
    if (!row) {
      return null;
    }
    if (row.getBoundingClientRect().bottom > y + PAST) {
      hi = mid;
    } else {
      lo = mid + 1;
    }
  }
  // A segment whose text is empty is not in the .md at all and has no span;
  // the place is the next one that does.
  for (let i = lo; i < list.length; i++) {
    if (list[i].span) {
      return list[i].span;
    }
  }
  return null;
}

// Land on the segment the coding view was showing, without playing it: the
// list opens where you were reading, and ⟩ / n step on from there.
function openAt(segment) {
  const row = segment && view.rows.get(segment.id);
  if (!row) {
    return;
  }
  setActive(segment.id, false);
  placeAt(row.getBoundingClientRect().top, readingLineHere(), root.querySelector('#vf-list'));
  // Measured after the scroll: the mark is drawn in content coordinates, but
  // the row has to be where it ends up before those are read off it.
  markPlace([row.getBoundingClientRect()]);
}

// --- navigation -------------------------------------------------------------

// Jump to the next/previous segment in the current scope, by time. Anchored on
// the playhead rather than the list index so it stays sensible after the scope
// changes under you.
function jump(delta) {
  const list = scopeList();
  if (!list.length) {
    flash(`No segments in “${SCOPES.find(s => s.id === view.scope).label}”. Choose another group to continue.`);
    return;
  }
  const here = view.activeId ? view.byId.get(view.activeId)?.start ?? -1 : -1;
  const target = delta > 0
    ? list.find(s => s.start > here)
    : [...list].reverse().find(s => s.start < here);
  if (!target) {
    const label = SCOPES.find(s => s.id === view.scope).label;
    flash(`${delta > 0 ? 'Last' : 'First'} segment in “${label}”`);
    return;
  }
  play(target);
}

function step(delta) {
  const list = segments();
  if (!list.length) {
    return;
  }
  const index = list.findIndex(s => s.id === view.activeId);
  const target = list[Math.max(0, Math.min(list.length - 1, index + delta))]
    || list[0];
  play(target);
}

// --- editing ----------------------------------------------------------------

function confirmOverwrite(message) {
  return new Promise(resolve => {
    const dialog = document.createElement('dialog');
    dialog.className = 'vf-overwrite';
    dialog.setAttribute('aria-label', 'Overwrite Markdown edits?');
    dialog.innerHTML = `<form method="dialog">
      <h3>Overwrite Markdown edits?</h3><p></p>
      <div><button value="cancel" autofocus>Cancel</button>
      <button value="overwrite">Overwrite and continue</button></div>
    </form>`;
    dialog.querySelector('p').textContent = message;
    dialog.addEventListener('keydown', event => event.stopPropagation());
    dialog.addEventListener('close', () => {
      const confirmed = dialog.returnValue === 'overwrite';
      dialog.remove();
      resolve(confirmed);
    }, {once: true});
    document.body.append(dialog);
    dialog.showModal();
  });
}

async function postEdit(segmentId, body, editSession = session) {
  if (editSession !== session) return null;
  const response = await apiPost(
    `/api/tx-edit?name=${encodeURIComponent(view.file)}`,
    {seg: segmentId, ...body}
  );
  if (editSession !== session) return null;
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    if (editSession !== session) return null;
    if (response.status === 409 && error.overwriteToken) {
      if (await confirmOverwrite(error.error)) {
        return postEdit(segmentId, {...body, overwriteToken: error.overwriteToken}, editSession);
      }
      if (editSession !== session) return null;
      flash('Cancelled — no files were changed. Save a copy before trying again.', true);
      return null;
    }
    flash(error.error || 'Save failed', true);
    return null;
  }
  const result = await response.json();
  if (editSession !== session) return null;
  const segment = view.byId.get(segmentId);
  if (segment && result.segment) {
    segment.text = result.segment.text;
    segment.status = result.segment.status;
    segment.speaker = result.segment.speaker;
    segment.speaker_suggestion = result.segment.speakerSuggestion;
    segment.flags = result.segment.flags || segment.flags;
    segment.words_stale = result.segment.wordsStale;
  }
  if (result.speakers && view.tx) {
    view.tx.speakers = result.speakers;   // a newly named person may be here
  }
  // A correction re-renders the whole .md, so every offset after it moves —
  // and one of them is the place this view hands back on the way out.
  if (result.spans) {
    for (const seg of segments()) {
      seg.span = result.spans[seg.id] || null;
    }
  }
  view.dirty = true;
  if (result.stale) {
    flash(`Saved — ${result.stale} code annotation${result.stale > 1 ? 's' : ''} on this text need re-checking`, true);
  } else if (result.edit) {
    flash(result.moved ? `Saved — ${result.moved} annotations shifted` : 'Saved');
  }
  return result;
}

// Where in `segment.text` the browser's own double-click selection landed.
// Taken from the live selection rather than from word indices, so it reads the
// same on a segment whose word spans are gone (edited, or timings stale) as on
// one that still has them. The body is either those spans joined by single
// spaces or the bare text, so its text is segment.text — if it ever is not,
// give up rather than drop the caret into the middle of some other word.
function selectedRange(body, segment) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount || body.textContent !== segment.text) {
    return null;
  }
  const range = selection.getRangeAt(0);
  if (!body.contains(range.startContainer) || !body.contains(range.endContainer)) {
    return null;
  }
  const before = document.createRange();
  before.selectNodeContents(body);
  before.setEnd(range.startContainer, range.startOffset);
  const from = before.toString().length;
  return {from, to: from + range.toString().length};
}

// `select` is a {from, to} range in segment.text to put the caret on; without
// one the caret goes to the end, which is where you want it when the edit was
// opened from the keyboard or the button rather than at a particular word.
function startEdit(segment, select) {
  const editSession = session;
  const row = view.rows.get(segment.id);
  if (!row || view.editingId === segment.id) {
    return;
  }
  view.editingId = segment.id;
  const body = row.querySelector('.vf-body');
  const area = document.createElement('textarea');
  area.className = 'vf-edit';
  area.value = segment.text;
  area.rows = Math.max(2, Math.ceil(segment.text.length / 90));
  body.replaceChildren(area);
  area.focus();
  const end = area.value.length;
  area.setSelectionRange(
    Math.min(select ? select.from : end, end),
    Math.min(select ? select.to : end, end)
  );

  const finish = async (commit) => {
    if (editSession !== session || view.editingId !== segment.id) {
      return;
    }
    view.editingId = null;
    const next = area.value.trim();
    if (commit && next && next !== segment.text) {
      const result = await postEdit(segment.id, {text: next});
      if (editSession !== session) return;
      if (!result) {
        view.editingId = segment.id;
        area.focus();
        return;
      }
    }
    renderRow(segment);
  };

  area.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    } else if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      finish(true);
    }
  });
  area.addEventListener('blur', () => finish(true));
}

// Diarization assigns a segment to a voice cluster; when it puts one utterance
// on the wrong cluster, this moves that segment alone. A whole cluster wearing
// the wrong name is a different mistake — that is a rename in tx["speakers"],
// not eight hundred of these.
const NEW_SPEAKER = '\u0000new';

function startSpeakerEdit(segment) {
  const editSession = session;
  const row = view.rows.get(segment.id);
  if (!row || view.speakerId === segment.id) {
    return;
  }
  view.speakerId = segment.id;
  const cell = row.querySelector('.vf-spk');
  const select = document.createElement('select');
  select.className = 'vf-spk-pick';
  for (const [key, name] of Object.entries(view.tx?.speakers || {})) {
    const option = document.createElement('option');
    option.value = key;
    option.textContent = name;
    option.selected = key === segment.speaker;
    select.appendChild(option);
  }
  const add = document.createElement('option');
  add.value = NEW_SPEAKER;
  add.textContent = 'someone else…';
  select.appendChild(add);
  cell.replaceChildren(select);
  select.focus();

  const finish = async (commit) => {
    if (editSession !== session || view.speakerId !== segment.id) {
      return;
    }
    view.speakerId = null;
    const picked = select.value;
    if (commit && picked === NEW_SPEAKER) {
      // Someone pyannote never gave a cluster to. Rare, but it happens to
      // people who speak twice in ninety minutes.
      const name = (window.prompt('Name of the speaker:') || '').trim();
      if (name) {
        await postEdit(segment.id, {newSpeaker: name});
      }
    } else if (commit && picked && picked !== segment.speaker) {
      await postEdit(segment.id, {speaker: picked});
    }
    if (editSession === session) renderRow(segment);
  };

  select.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    }
  });
  select.addEventListener('change', () => finish(true));
  select.addEventListener('blur', () => finish(false));
}

// Confirm and move on — the review loop is listen, judge, ✓, listen. The row
// updates where it stands rather than vanishing out of the list, so the only
// movement is the deliberate one to the next flag.
async function confirmSegment(segment) {
  if (!await postEdit(segment.id, {status: 'ok'})) return;
  renderRow(segment);
  // Anchor the jump on what was just confirmed, not on wherever the playhead
  // drifted to — ✓ on a row you clicked should advance from that row.
  setActive(segment.id, false);
  const remaining = counts()[view.scope];
  if (view.scope === 'queue' && !remaining) {
    flash('Queue empty — nothing left flagged on this interview');
    return;
  }
  jump(1);
}

function flash(message, sticky = false) {
  const element = root.querySelector('#vf-flash');
  if (!element) {
    return;
  }
  element.textContent = message;
  element.title = message;      // the bar keeps it to one line; this is the rest
  element.classList.toggle('vf-flash-warn', sticky);
  clearTimeout(element._timer);
  element._timer = setTimeout(() => {
    element.textContent = '';
    element.classList.remove('vf-flash-warn');
  }, sticky ? 8000 : 2000);
}

// --- rendering --------------------------------------------------------------

function wordsHtml(segment) {
  const words = segment.words || [];
  // After an edit the stored words describe the old text, so showing them as
  // clickable would be a lie about which word is which. The segment still
  // seeks correctly as a whole.
  if (!words.length || segment.words_stale || segment.text !== segment.orig) {
    return esc(segment.text);
  }
  return words.map(w => {
    if (typeof w.t0 !== 'number') {
      return esc(w.w);
    }
    // Word confidence is shown, never used to flag: it is an alignment
    // posterior, and function words score near zero while being correct.
    const faint = typeof w.p === 'number' && w.p < 0.25 ? ' vf-faint' : '';
    return `<span class="vf-word${faint}" data-t0="${w.t0}" data-t1="${w.t1}">${esc(w.w)}</span>`;
  }).join(' ');
}

function flagsHtml(segment) {
  const chips = [];
  const rank = view.rank.get(segment.id);
  if (rank) {
    chips.push(`<span class="vf-chip vf-chip-rank" title="Ranked ${rank} of ${view.rank.size} on this interview by how likely the transcript is to be wrong. Ordering is not used to lay the list out — it only decides what gets flagged.">#${rank}</span>`);
  }
  if ((segment.flags || []).includes('disagree')) {
    // Name the disputed words when we have them — that is what tells you
    // whether this is worth listening to before you press play.
    const odd = (segment.odd || []).join(', ');
    chips.push(`<span class="vf-chip vf-chip-dis" title="The second ASR model did not produce ${odd ? 'these words: ' + esc(odd) : 'most of this text'}">2nd model differs${odd ? ': ' + esc(odd) : ''}</span>`);
  }
  const drift = segment.drift;
  if ((segment.flags || []).includes('timing') && drift) {
    // The text may be right while the clock is wrong: forced alignment can
    // slip, and then ▶ plays the wrong few seconds of audio. The slip often
    // starts part-way through a segment, so this points at where the displaced
    // words are, not at the segment's start. It offers the other model's
    // position rather than picking a winner — nothing here knows which of the
    // two is correct.
    const at = Math.max(0, drift.at + drift.dt);
    const part = drift.n < (segment.words || []).length ? `its last ${drift.n} words` : 'these words';
    chips.push(`<button class="vf-chip vf-chip-time" data-at="${at}" title="The second model puts ${part} ${Math.abs(drift.dt).toFixed(1)}s ${drift.dt > 0 ? 'later' : 'earlier'}, at ${hms(at)}. If the audio here does not match the text, our alignment slipped — click to listen there instead.">also try ${hms(at)}</button>`);
  }
  if ((segment.flags || []).includes('repeat')) {
    chips.push('<span class="vf-chip vf-chip-rep" title="Near-identical to a nearby segment — Whisper repetition loop">repeat</span>');
  }
  for (const note of segment.notes || []) {
    chips.push(`<span class="vf-chip vf-chip-sus" title="Known mishearing pattern">${esc(note)}</span>`);
  }
  if (segment.status === 'edited') {
    chips.push('<span class="vf-chip vf-chip-ok">edited</span>');
  } else if (segment.status === 'ok') {
    chips.push('<span class="vf-chip vf-chip-ok">confirmed</span>');
  }
  return chips.join('');
}

function rowHtml(segment) {
  const alt = segment.alt && segment.alt !== segment.text
    ? `<div class="vf-alt" title="What the second ASR model heard">2nd: ${esc(segment.alt)}</div>`
    : '';
  const chips = flagsHtml(segment);
  const suggested = segment.speaker_suggestion;
  const speakerReview = suggested ? `<div class="vf-speaker-review">
    <span>Check speaker: ${esc(speakerName(suggested.speaker))}? Listen before deciding.</span>
    <button data-speaker-decision="accept">Use suggested speaker</button>
    <button data-speaker-decision="keep">Keep current speaker</button>
  </div>` : '';
  return `
    <button class="vf-play" ${canPlay() ? '' : 'disabled'} title="Play this segment (loop is ${view.loop ? 'on' : 'off'})">▶</button>
    <div class="vf-meta">
      <span class="vf-time">${hms(segment.start)}</span>
      <span class="vf-spk" title="Wrong speaker? Click to reassign this segment (S)" tabindex="0">${esc(speakerName(segment.speaker))}</span>
    </div>
    <div class="vf-content">
      <div class="vf-body">${wordsHtml(segment)}</div>
      ${alt}
      ${speakerReview}
      ${chips ? `<div class="vf-chips">${chips}</div>` : ''}
    </div>
    <div class="vf-actions">
      <button class="vf-edit-btn" title="Edit this text (E)">edit</button>
      <button class="vf-ok-btn" title="Confirm as correct (Enter)">✓</button>
    </div>`;
}

function rowClass(segment) {
  const classes = ['vf-row'];
  if (isFlagged(segment)) {
    classes.push('vf-flag');
  }
  if (segment.status !== 'unreviewed') {
    classes.push('vf-done');
  }
  if (segment.id === view.activeId) {
    classes.push('vf-active');
  }
  return classes.join(' ');
}

function renderRow(segment) {
  const row = view.rows.get(segment.id);
  if (!row) {
    return;
  }
  row.innerHTML = rowHtml(segment);
  row.className = rowClass(segment);
  view.words.delete(segment.id);
  lit = null;
  if (view.playingId === segment.id) {
    view.playingId = null;      // the fresh markup shows ▶ again
    markPlaying();
  }
  const tick = view.ticks.get(segment.id);
  if (tick) {
    tick.classList.toggle('vf-tick-done', segment.status !== 'unreviewed');
  }
  // The row stays put — yanking it out from under the cursor mid-correction is
  // hostile — but the counts must tell the truth about what is left.
  renderScopes();
}

function renderList() {
  const list = root.querySelector('#vf-list');
  view.rows.clear();
  view.words.clear();
  lit = null;
  view.playingId = null;

  // One innerHTML for ~1k segments and ~10k word spans: building them as
  // separate elements is measurably slower and buys nothing, since every
  // handler below is delegated on the container.
  list.innerHTML = segments().map(segment =>
    `<div class="${rowClass(segment)}" data-id="${esc(segment.id)}">${rowHtml(segment)}</div>`
  ).join('');
  for (const row of list.querySelectorAll('.vf-row')) {
    view.rows.set(row.dataset.id, row);
  }
  renderTrack();
  renderScopes();
}

// The timeline is the answer to "where am I in the interview": flagged
// segments as ticks across the whole duration, with the playhead on top.
function renderTrack() {
  const track = root.querySelector('#vf-track');
  const total = duration();
  view.ticks.clear();
  if (!total) {
    track.hidden = true;
    return;
  }
  track.hidden = false;
  track.innerHTML =
    segments().filter(isFlagged).map(s =>
      `<i class="vf-tick${s.status !== 'unreviewed' ? ' vf-tick-done' : ''}" data-id="${esc(s.id)}" style="left:${(s.start / total) * 100}%" title="${hms(s.start)}"></i>`
    ).join('') + '<i id="vf-head" class="vf-head" style="left:0%"></i>';
  for (const tick of track.querySelectorAll('.vf-tick')) {
    view.ticks.set(tick.dataset.id, tick);
  }
}

function renderScopes() {
  const tabs = root.querySelector('#vf-tabs');
  const n = counts();
  tabs.replaceChildren();
  for (const scope of SCOPES) {
    const button = document.createElement('button');
    button.className = 'vf-tab';
    button.classList.toggle('vf-tab-on', view.scope === scope.id);
    button.title = `Use Previous / Next to visit ${scope.label.toLowerCase()} segments (${n[scope.id]}). All transcript rows stay visible.`;
    button.setAttribute('aria-pressed', String(view.scope === scope.id));
    button.textContent = `${scope.label} (${n[scope.id]})`;
    button.onclick = () => {
      view.scope = scope.id;
      renderScopes();
    };
    tabs.appendChild(button);
  }
}

function chrome() {
  return `
    <div class="vf-header">
    <div class="vf-bar">
      <button id="vf-playpause" title="Play / pause (space)">▶</button>
      <span id="vf-clock" class="vf-clock">0:00 / 0:00</span>
      <button id="vf-loop" title="Repeat the current segment until you turn this off (L)">loop</button>
      <button id="vf-speed" title="Playback speed ( [ and ] )">1×</button>
      <span class="vf-jump">
        <button id="vf-prev" title="Previous segment in the selected group (P)">⟨ Previous</button>
        <button id="vf-next" title="Next segment in the selected group (N)">Next ⟩</button>
      </span>
      <div id="vf-tabs" class="vf-tabs"></div>
      <span id="vf-flash" class="vf-flash"></span>
      <button id="vf-exit" title="Return to coding; saved changes are kept">Back to coding</button>
    </div>
    <div id="vf-track" class="vf-track" title="The whole interview. Ticks are flagged segments; click to seek."></div>
    </div>
    <section class="vf-hint" aria-label="How to review this transcript">
      <ol>
        <li><strong>Listen:</strong> click ▶ beside a row to play that segment, or click a word to play from there.</li>
        <li><strong>Correct:</strong> double-click the text or click <b>edit</b>. Press <kbd>Enter</kbd> to save or <kbd>Esc</kbd> to cancel. Click a speaker name to change it.</li>
        <li><strong>Confirm:</strong> click <b>✓</b> to mark the segment correct and play the next one in the selected group.</li>
      </ol>
      <details>
        <summary>Navigation and keyboard shortcuts</summary>
        <p><b>Needs review</b> visits flagged segments awaiting review; <b>Edited</b> visits corrected segments; <b>All</b> visits every segment.
          Choose a group, then use <b>Previous</b> / <b>Next</b>. The full transcript stays visible in time order.
          A count of <b>0</b> means there are no segments in that group.</p>
        <p><kbd>N</kbd> / <kbd>P</kbd>: next / previous in the selected group.
          <kbd>↓</kbd> / <kbd>↑</kbd>: next / previous transcript row.
          <kbd>Space</kbd>: play / pause. <b>loop</b> repeats the current segment; click it again to stop repeating.
          <b>1×</b> changes playback speed.</p>
        <p>Saved edits and confirmations are kept automatically. <b>Back to coding</b> returns to the coding view.</p>
      </details>
    </section>
    <div id="vf-noaudio" class="vf-noaudio" hidden></div>
    <div id="vf-list" class="vf-list"></div>`;
}

function onKeyDown(event) {
  if (!root || root.hidden || !view.tx || view.editingId || view.speakerId) {
    return;
  }
  const tag = (event.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    return;
  }
  if (tag === 'summary' && (event.key === 'Enter' || event.key === ' ')) {
    return; // Let the help disclosure open without confirming or playing a row.
  }
  if (event.target.closest('[data-speaker-decision]')) return;
  const active = view.activeId ? view.byId.get(view.activeId) : null;
  switch (event.key) {
    case ' ':
      event.preventDefault();
      togglePlay();
      break;
    case 'ArrowDown':
      event.preventDefault();
      step(1);
      break;
    case 'ArrowUp':
      event.preventDefault();
      step(-1);
      break;
    case 'n':
    case 'N':
      event.preventDefault();
      jump(1);
      break;
    case 'p':
    case 'P':
      event.preventDefault();
      jump(-1);
      break;
    case 'l':
    case 'L':
      toggleLoop();
      break;
    case '[':
      setSpeed(view.speedIndex - 1);
      break;
    case ']':
      setSpeed(view.speedIndex + 1);
      break;
    case 'r':
    case 'R':
      if (active) {
        play(active, {scroll: false});
      }
      break;
    case 's':
    case 'S':
      if (active) {
        event.preventDefault();
        startSpeakerEdit(active);
      }
      break;
    case 'e':
    case 'E':
      if (active) {
        event.preventDefault();
        startEdit(active);
      }
      break;
    case 'Enter':
      if (active) {
        event.preventDefault();
        confirmSegment(active);
      }
      break;
    default:
      break;
  }
}

function toggleLoop() {
  view.loop = !view.loop;
  root.querySelector('#vf-loop').classList.toggle('vf-on', view.loop);
}

// --- entry points -----------------------------------------------------------

export function initVerify(container, exitHandler) {
  root = container;
  onExit = exitHandler;
  root.innerHTML = chrome();
  root.hidden = true;

  audio = new Audio();
  audio.preload = 'metadata';
  // `timeupdate` still fires (on seek, and while paused); it is the coarse
  // backstop. `pump` is what keeps the highlight and the loop bounds tight.
  audio.addEventListener('timeupdate', tick);
  audio.addEventListener('seeked', tick);
  // A media element can't see status codes. If audio stops loading, the
  // likeliest cause when hosted is a session that ended: check, and go to
  // sign-in rather than leave the player silently dead.
  audio.addEventListener('error', async () => {
    if (!audio.getAttribute('src')) return;
    const me = await sessionInfo();
    if (me.signedOut) toSignIn();
    else flash('Audio could not be loaded.', true);
  });
  audio.addEventListener('loadedmetadata', () => {
    if (!canPlay()) return;
    // The real duration only arrives now, and the ticks are positioned against
    // it — a .tx.json duration off by a second would slide every tick.
    if (view.tx) {
      renderTrack();
    }
    clockSecond = -1;
    updateClock();
  });
  audio.addEventListener('play', () => {
    if (!canPlay() || audio.paused) return;
    root.querySelector('#vf-playpause').textContent = '❚❚';
    startPump();
    markPlaying();
  });
  audio.addEventListener('pause', () => {
    root.querySelector('#vf-playpause').textContent = '▶';
    tick();      // land the highlight on where the audio actually stopped
    markPlaying();
  });

  root.querySelector('#vf-playpause').onclick = togglePlay;
  root.querySelector('#vf-loop').onclick = toggleLoop;
  root.querySelector('#vf-speed').onclick = () =>
    setSpeed((view.speedIndex + 1) % SPEEDS.length);
  root.querySelector('#vf-prev').onclick = () => jump(-1);
  root.querySelector('#vf-next').onclick = () => jump(1);
  root.querySelector('#vf-exit').onclick = () => closeVerify();

  const track = root.querySelector('#vf-track');
  track.onclick = event => {
    if (!canPlay()) return;
    const total = duration();
    if (!total) {
      return;
    }
    const box = track.getBoundingClientRect();
    const time = ((event.clientX - box.left) / box.width) * total;
    seekTo(time, null);
    const segment = segmentAt(time);
    if (segment) {
      setActive(segment.id, 'center');
    }
  };

  const list = root.querySelector('#vf-list');
  list.onclick = event => {
    const row = event.target.closest('.vf-row');
    const segment = row && view.byId.get(row.dataset.id);
    if (!segment) {
      return;
    }
    const speakerDecision = event.target.closest('[data-speaker-decision]');
    if (speakerDecision && segment.speaker_suggestion) {
      const editSession = session;
      row.querySelectorAll('[data-speaker-decision]').forEach(button => button.disabled = true);
      postEdit(segment.id, {speakerDecision: {
        ...segment.speaker_suggestion, action: speakerDecision.dataset.speakerDecision
      }}, editSession).catch(() => {
        if (editSession === session) flash('Could not save speaker decision. Please retry.', true);
      }).finally(() => {
        if (editSession === session) renderRow(segment);
      });
    } else if (event.target.closest('.vf-play')) {
      if (segment.id === view.playingId) {
        audio.pause();
      } else {
        play(segment, {scroll: false});
      }
    } else if (event.target.closest('.vf-edit-btn')) {
      startEdit(segment);
    } else if (event.target.closest('.vf-ok-btn')) {
      confirmSegment(segment);
    } else if (event.target.closest('.vf-spk')) {
      startSpeakerEdit(segment);
    } else if (event.target.closest('.vf-chip-time')) {
      // Unbounded: the point is to hear what is actually around that moment.
      seekTo(Number(event.target.closest('.vf-chip-time').dataset.at), null);
      setActive(segment.id, false);
    } else {
      const word = event.target.closest('.vf-word');
      if (word) {
        seekTo(Number(word.dataset.t0), segment);
      }
      setActive(segment.id, false);
    }
  };
  list.ondblclick = event => {
    const row = event.target.closest('.vf-row');
    const segment = row && view.byId.get(row.dataset.id);
    if (!segment || !event.target.closest('.vf-body')) {
      return;
    }
    // Double-clicking a word does here what it does in any text: selects that
    // word, ready to be retyped. It also plays from that word, because the
    // reason you are about to retype it is that you need to hear it again —
    // and the first of the two clicks has already seeked there anyway.
    const word = event.target.closest('.vf-word');
    if (word) {
      seekTo(Number(word.dataset.t0), segment);
    }
    startEdit(segment, selectedRange(row.querySelector('.vf-body'), segment));
  };

  document.addEventListener('keydown', onKeyDown);
}

export async function openVerify(fileName, atOffset = null) {
  const openedSession = ++session;
  view.tx = null;
  resetAudio();
  view.file = fileName;
  root.hidden = false;
  root.querySelector('#vf-list').textContent = 'Loading transcript…';
  root.querySelector('#vf-tabs').replaceChildren();
  root.querySelector('#vf-track').hidden = true;
  root.querySelector('#vf-noaudio').hidden = true;
  updatePlaybackControls();
  let tx;
  try {
    tx = await apiFetch(`/api/tx?name=${encodeURIComponent(fileName)}`);
  } catch (error) {
    if (openedSession === session) {
      root.querySelector('#vf-list').textContent = 'Could not load this transcript. Go back to coding and try again.';
    }
    return;
  }
  if (openedSession !== session) return;
  view.tx = tx;
  view.byId = new Map((view.tx.segments || []).map(s => [s.id, s]));
  view.order = new Map((view.tx.segments || []).map((s, i) => [s.id, i]));
  view.words.clear();
  view.rows.clear();
  view.ticks.clear();
  view.activeId = null;
  view.boundedId = null;
  view.editingId = null;
  view.speakerId = null;
  view.playingId = null;
  view.dirty = false;
  view.scope = 'queue';

  // Risk still ranks the flagged segments — it just labels them instead of
  // ordering them, so the triage signal survives a chronological layout.
  view.rank = new Map(
    (view.tx.segments || [])
      .filter(isFlagged)
      .sort((a, b) => (b.risk || 0) - (a.risk || 0))
      .map((s, i) => [s.id, i + 1])
  );

  const banner = root.querySelector('#vf-noaudio');
  if (view.tx.audioAvailable) {
    audio.src = apiUrl(`/api/audio?name=${encodeURIComponent(view.tx.audio)}`);
    audio.playbackRate = SPEEDS[view.speedIndex];
    banner.hidden = true;
  } else if (BASE) {
    // Hosted: the audio lives on the server, set up by whoever runs it.
    banner.hidden = false;
    banner.innerHTML = `
      <strong>Audio isn't available for this transcript.</strong> You can still edit the transcript, but playback is unavailable.
      Ask the project's admin to add <code>${esc(view.tx.audio || '(no audio filename set)')}</code> to the project's audio folder.`;
  } else {
    banner.hidden = false;
    banner.innerHTML = `
      <strong>Audio is missing on this computer.</strong> You can still edit the transcript, but playback is unavailable.
      <details>
        <summary>Set up audio playback</summary>
        <p>This transcript needs the playback file <code>${esc(view.tx.audio || '(no audio filename set)')}</code>.</p>
        <ul>
          <li><b>Have that playback file?</b> Put it in <code>md/audio/</code> inside the project folder.
            If you started the app with <code>QDA_AUDIO_DIR</code>, put it in that folder instead.</li>
          <li><b>Have the original recording?</b> Open a terminal in the project folder and run
            <code>make prepare AUDIO="/path/to/original-recording.m4a"</code>, replacing the example path with your recording's path.
            The recording's basename must match the saved transcript: for example, <code>interview-01.m4a</code>
            matches <code>interview-01.tx.json</code>. This reuses the saved transcript and creates playback audio using ffmpeg.</li>
        </ul>
        <p>Then reopen <b>Verify against audio</b> to check for the file again.
          Model installation and a Hugging Face token are only needed for new transcriptions.</p>
      </details>`;
  }

  root.hidden = false;
  renderList();
  updatePlaybackControls();
  clockSecond = -1;
  updateClock();
  const left = counts().queue;
  const queue = left
    ? `${left} flagged — press ⟩ or n for the next one`
    : 'Nothing flagged for review';
  // The timestamp is the one label both views print, so it is what ties the
  // marked row back to the paragraph you just left.
  const landing = atOffset == null ? null : segmentAtOffset(atOffset);
  flash(landing ? `picked up at ${hms(landing.start)} · ${queue}` : queue);
  // Last, and after the message: the flash can wrap the bar onto a second line
  // and push the whole list down under whatever was just anchored to it.
  openAt(landing);
}

// `keepPlace` is false when the coding view is about to show a different file:
// there is no place to hand back, and the offsets belong to the old transcript.
export function closeVerify({keepPlace = true, notify = true} = {}) {
  session += 1;
  // Measured before hiding, while the rows still have boxes.
  const at = keepPlace ? readingSpanHere() : null;
  root.hidden = true;
  view.tx = null;
  resetAudio();
  updatePlaybackControls();
  const wasDirty = view.dirty;
  view.dirty = false;
  if (notify) onExit(wasDirty, at);
}

export function isVerifying() {
  return root && !root.hidden;
}

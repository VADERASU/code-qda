// Recordings: the project's uploaded audio that no transcript plays yet, and
// transcribing them on the server. The server runs one job at a time; while
// any of this project's jobs is waiting or running, the list refreshes.
import {apiFetch, apiPost} from './api.js';
import {h} from './dom.js';

const POLL_MS = 4000;
const FIRST_POLL_MS = 1500;   // soon after Start, so the job is seen starting
let handlers = {onDone: () => {}, openFile: () => {}};
let timer = null;
let lastData = null;
const seen = new Map();       // job id -> the status it had when last shown
const picking = new Set();    // recordings whose speaker picker is open
const errors = new Map();     // recording -> the last refusal, shown in its row

export function initRecordings(options) {
  handlers = {...handlers, ...options};
}

export async function loadRecordings() {
  let data;
  try {
    data = await apiFetch('/api/recordings');
  } catch {
    return;
  }
  show(data);
}

async function send(path, body, name) {
  const response = await apiPost(path, body);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    errors.set(name, data.error || `That didn't work (${response.status}).`);
    if (lastData) show(lastData);
    return;
  }
  errors.delete(name);
  picking.delete(name);
  show(data);
  if (timer) schedule(true, FIRST_POLL_MS);
}

function picker(recording, previous) {
  const select = h('select', {'aria-label': `How many people speak in ${recording.name}`},
    ...Array.from({length: 10}, (_, i) => h('option', {value: i + 1}, String(i + 1))));
  select.value = String(previous?.speakers || 2);
  return [
    h('label', {}, 'Speakers ', select),
    h('button', {type: 'button', onclick: () => send('/api/transcribe',
      {audio: recording.name, speakers: Number(select.value)}, recording.name)}, 'Start'),
    h('button', {type: 'button', class: 'link', onclick: () => {
      picking.delete(recording.name);
      show(lastData);
    }}, 'Cancel'),
    h('span', {class: 'recording-hint'},
      'How many people talk in the recording? A wrong number can merge two people into one speaker.')
  ];
}

function statusLine(recording, transcribe) {
  const job = recording.job;
  const line = h('div', {class: 'recording-status'});
  const cancel = () => h('button', {type: 'button',
    onclick: () => send('/api/transcribe/cancel', {id: job.id}, recording.name)}, 'Cancel');
  if (job?.status === 'queued') {
    line.append(job.ahead ? `Waiting: ${job.ahead} ahead` : 'Waiting to start', cancel());
  } else if (job?.status === 'running') {
    line.append(`${job.stage || 'Transcribing'}…`, cancel());
  } else if (job?.status === 'done') {
    line.append('Transcribed: ', h('button', {type: 'button', class: 'link',
      onclick: () => handlers.openFile(job.transcript)}, job.transcript));
  } else if (picking.has(recording.name)) {
    line.append(...picker(recording, job));
  } else {
    if (job?.status === 'failed') line.append(h('span', {class: 'recording-error'}, `Failed: ${job.error}`));
    if (job?.status === 'cancelled') line.append('Cancelled.');
    line.append(h('button', {type: 'button', disabled: !transcribe.available,
      title: transcribe.available ? 'Make a transcript from this recording on the server' : '',
      onclick: () => {
        picking.add(recording.name);
        show(lastData);
      }}, job && job.status !== 'cancelled' ? 'Try again' : 'Transcribe'));
  }
  if (errors.has(recording.name)) line.append(h('span', {class: 'recording-error'}, errors.get(recording.name)));
  return line;
}

function show(data) {
  lastData = data;
  const box = document.getElementById('recordings');
  const recordings = data.recordings || [];
  const transcribe = data.transcribe || {};
  box.replaceChildren();
  box.hidden = false;
  if (!recordings.length) {
    if (transcribe.local) {
      box.hidden = true;
    } else {
      box.append(h('p', {class: 'recordings-hint'}, 'Upload a recording to transcribe it here.'));
    }
    schedule(false);
    return;
  }
  box.append(h('h4', {}, 'Recordings'));
  if (!transcribe.available) {
    box.append(h('p', {class: 'recordings-hint'}, transcribe.local
      ? 'To transcribe here, run make prepare AUDIO=… in a terminal.'
      : `Transcription isn't set up on this server yet. It needs ${transcribe.problems.join(' and ')}.`));
  }
  let active = false;
  const finished = [];
  for (const recording of recordings) {
    const job = recording.job;
    if (job) {
      const before = seen.get(job.id);
      if (job.status === 'done' && (before === 'queued' || before === 'running')) finished.push(job);
      seen.set(job.id, job.status);
      if (job.status === 'queued' || job.status === 'running') active = true;
    }
    box.append(h('div', {class: 'recording'},
      h('span', {class: 'recording-name', title: recording.name}, recording.name),
      statusLine(recording, transcribe)));
  }
  schedule(active);
  if (finished.length) handlers.onDone(finished);
}

function schedule(active, delay = POLL_MS) {
  clearTimeout(timer);
  timer = active ? setTimeout(() => {
    if (document.visibilityState === 'visible') loadRecordings();
    else schedule(true);
  }, delay) : null;
}

import {apiUrl} from './api.js';
import {offlineFile, offlineFiles, updateOfflineFile} from './offline-store.js';

class CodingRequestError extends Error {
  constructor(status) {
    super(`Coding request failed (${status})`);
    this.status = status;
  }
}

export function canUseCachedCoding(error) {
  return error instanceof TypeError || error instanceof SyntaxError
    || error.name === 'TimeoutError' || error.status >= 500;
}

async function requestCoding(path, data) {
  if (!data && !navigator.onLine) throw new TypeError('Offline');
  const options = {signal: AbortSignal.timeout(8000)};
  if (data) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json'};
    options.body = JSON.stringify(data);
  }
  const response = await fetch(apiUrl(path), options);
  if (!response.ok) throw new CodingRequestError(response.status);
  return response.json();
}

function recover(record, draft) {
  record.recovery = [...(record.recovery || []), draft];
}

export function createOfflineCoding(owner, project, onChange) {
  const read = name => offlineFile(owner, project, name);
  const list = () => offlineFiles(owner, project);
  const edit = (name, change) => updateOfflineFile(owner, project, name, change);
  const path = name => `/api/codes?name=${encodeURIComponent(name)}`;
  let running;
  let timer;
  let runAgain = false;
  let delay = 1500;

  async function open(name) {
    const cached = await read(name).catch(() => undefined);
    let snapshot;
    try {
      snapshot = await requestCoding(`${path(name)}&text=1`);
    } catch (error) {
      if (canUseCachedCoding(error)) {
        const current = await read(name).catch(() => undefined);
        if (current?.snapshot) return {record: current, offline: true};
      }
      throw error;
    }
    if (snapshot.owner !== owner) throw new CodingRequestError(403);
    const record = await edit(name, current => {
      if (!current.snapshot || current.snapshot.rev === cached?.snapshot?.rev
          && current.snapshot.textRev === cached?.snapshot?.textRev) {
        current.snapshot = snapshot;
      }
      return current;
    }).catch(() => null);
    return {record: record || {name, snapshot}, offline: false, cached: record !== null};
  }

  async function queue(name, base, previousVersion, latest) {
    const state = await edit(name, record => {
      const pending = record.pending;
      if (pending && pending.latest.version !== previousVersion) {
        recover(record, {...base, latest});
        return {recovery: true, collision: true};
      }
      const acknowledged = record.ack?.version === previousVersion ? record.ack : null;
      record.pending = pending
        ? {...pending, latest}
        : {...base, ...acknowledged, latest};
      return {recovery: Boolean(record.recovery)};
    });
    if (!state.collision) sync();
    return state;
  }

  async function send(name) {
    const draft = await edit(name, record => {
      const pending = record.pending;
      if (!pending) return;
      pending.attempt ||= pending.latest;
      return pending;
    });
    if (!draft) return;
    const {attempt, content, rev, textRev, codeTreeBase} = draft;
    let data;
    try {
      data = await requestCoding(path(name), {
        owner, rev, textRev, codeTreeBase, codeTree: attempt.codeTree, annotations: attempt.annotations
      });
    } catch (error) {
      if (error.status !== 409) throw error;
      const record = await edit(name, current => {
        if (current.pending?.attempt?.version !== attempt.version) return;
        recover(current, current.pending);
        delete current.pending;
        delete current.ack;
        return current;
      });
      if (record) await onChange(name, {type: 'conflict', record});
      return;
    }
    if (!data.ok || typeof data.rev !== 'string') throw new Error('Invalid save response');
    const record = await edit(name, current => {
      if (current.pending?.attempt?.version !== attempt.version) return;
      current.snapshot = {owner, version: 2, content, textRev, rev: data.rev,
        annotations: data.annotations, codeTree: data.codeTree};
      const ack = {version: attempt.version, rev: data.rev, codeTreeBase: attempt.codeTree};
      current.ack = ack;
      if (current.pending.latest.version === attempt.version) {
        delete current.pending;
      } else {
        current.pending = {...current.pending, ...ack, attempt: null};
      }
      return current;
    });
    if (record) await onChange(name, {type: 'saved', record, attempt});
  }

  async function flush() {
    for (const {name, pending} of await list()) {
      if (!pending) continue;
      try {
        await send(name);
      } catch (error) {
        const retry = canUseCachedCoding(error);
        await onChange(name, {type: retry ? 'waiting' : 'error', error});
        if (retry) return true;
      }
    }
    return false;
  }

  function sync(force = false) {
    if (running) {
      runAgain = true;
      return running;
    }
    clearTimeout(timer);
    timer = null;
    if (!force && !navigator.onLine) return;
    const lock = `code-qda-sync:${project}`;
    const run = navigator.locks ? navigator.locks.request(lock, flush) : flush();
    running = run.then(async waiting => {
      if (waiting) {
        timer = setTimeout(sync, delay);
        delay = Math.min(delay * 2, 30000);
      } else {
        delay = 1500;
        // New edits may have arrived after the list was read or during a send.
        const records = await list();
        runAgain ||= records.some(({pending}) => pending && !pending.attempt);
      }
    }).catch(() => {
      // Closing, signing out, or denying storage can invalidate a transaction.
    }).finally(() => {
      running = null;
      if (runAgain && !timer) timer = setTimeout(sync, 0);
      runAgain = false;
    });
    return running;
  }

  return {open, queue, sync, list, read};
}

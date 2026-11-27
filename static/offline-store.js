// Keep project data separate from the public application shell. All file
// transactions check the current account, including writes from older tabs.
let database;

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function openDatabase() {
  if (!database) {
    database = Promise.resolve().then(() => {
      const request = indexedDB.open('code-qda-offline', 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('session');
        request.result.createObjectStore('files');
      };
      return requestResult(request);
    }).catch(error => {
      database = undefined;
      throw error;
    });
  }
  return database;
}

async function transact(mode, action) {
  const db = await openDatabase();
  const transaction = db.transaction(['session', 'files'], mode);
  const complete = new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onabort = () => reject(transaction.error);
  });
  try {
    const value = await action(transaction.objectStore('session'), transaction.objectStore('files'));
    await complete;
    return value;
  } catch (error) {
    await complete.catch(() => {});
    throw error;
  }
}

export function offlineSession() {
  return transact('readonly', session => requestResult(session.get('active')));
}

export function rememberSession(me, project) {
  return transact('readwrite', async (session, files) => {
    const previous = await requestResult(session.get('active'));
    if (previous?.email !== me.email) {
      files.clear();
    } else {
      const projects = new Set(me.projects.map(entry => entry.slug));
      for (const key of await requestResult(files.getAllKeys())) {
        if (!projects.has(key[0])) files.delete(key);
      }
    }
    session.put({...me, lastProject: project || previous?.lastProject}, 'active');
  });
}

export function clearOfflineSession() {
  return transact('readwrite', (session, files) => {
    session.clear();
    files.clear();
  });
}

export function clearOfflineForOtherAccount(email) {
  return transact('readwrite', async (session, files) => {
    const active = await requestResult(session.get('active'));
    if (active && active.email !== email) {
      session.clear();
      files.clear();
    }
  });
}

export function hasPendingOfflineEdits() {
  return transact('readonly', async (_session, files) => {
    const records = await requestResult(files.getAll());
    return records.some(record => record.pending || record.recovery);
  });
}

function projectTransaction(owner, project, mode, action) {
  return transact(mode, async (session, files) => {
    const me = await requestResult(session.get('active'));
    if (me?.email !== owner || !me.projects.some(entry => entry.slug === project)) {
      throw new Error('Offline session changed');
    }
    return action(files);
  });
}

export function offlineFiles(owner, project) {
  const keys = IDBKeyRange.bound([project], [project, []]);
  return projectTransaction(owner, project, 'readonly', files => requestResult(files.getAll(keys)));
}

export function offlineFile(owner, project, name) {
  return projectTransaction(owner, project, 'readonly', files => requestResult(files.get([project, name])));
}

export function updateOfflineFile(owner, project, name, edit) {
  const key = [project, name];
  return projectTransaction(owner, project, 'readwrite', async files => {
    const record = await requestResult(files.get(key)) || {name};
    const result = edit(record);
    if (result !== undefined) files.put(record, key);
    return result;
  });
}

// Upload transcripts and audio into the current project, one file per
// request. The server never replaces a file; it says why it refused one, and
// that message is shown as is. XMLHttpRequest, not fetch, for upload progress:
// audio files can take minutes.
import {apiUrl, toSignIn} from './api.js';

function send(file, onProgress) {
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', apiUrl(`/api/upload?file=${encodeURIComponent(file.name)}`));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* empty or not JSON */ }
      resolve({status: xhr.status, body});
    };
    xhr.onerror = () => resolve({status: 0, body: {}});
    xhr.send(file);
  });
}

export function initUpload({onUploaded}) {
  const button = document.getElementById('upload-btn');
  const input = document.getElementById('upload-input');
  const status = document.getElementById('upload-status');
  if (!button || !input) return;

  function show(message, error = false) {
    status.textContent = message;
    status.classList.toggle('error', error);
    status.hidden = !message;
  }

  button.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    const files = [...input.files];
    input.value = '';
    if (!files.length) return;
    button.disabled = true;
    const problems = [];
    const added = [];
    try {
      for (const [index, file] of files.entries()) {
        const counter = files.length > 1 ? ` (${index + 1} of ${files.length})` : '';
        show(`Uploading ${file.name}${counter}…`);
        const {status: code, body} = await send(file, fraction => {
          if (fraction < 1) show(`Uploading ${file.name}${counter}… ${Math.round(fraction * 100)}%`);
        });
        if (code === 401) {
          toSignIn();
          return;
        }
        if (code === 200 && body.ok) added.push(body);
        else if (code === 413 && !body.error) problems.push(`${file.name} is too large to upload.`);
        else problems.push(body.error || `${file.name} could not be uploaded (${code || 'no connection'}).`);
      }
    } finally {
      button.disabled = false;
    }
    const audio = added.filter(item => item.kind === 'audio').length;
    const parts = [];
    if (added.length) parts.push(`Added ${added.map(item => item.name).join(', ')}.`);
    if (audio) parts.push('Audio plays with a .tx.json transcript that names it.');
    show([...parts, ...problems].join(' '), problems.length > 0);
    if (added.length) await onUploaded(added.filter(item => item.kind === 'transcript').map(item => item.name));
  });
}

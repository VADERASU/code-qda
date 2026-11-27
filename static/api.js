// Every request to the app's data goes through here.
//
// Hosted, the page lives at /p/<project>/ and so does its API; locally both
// live at the root. BASE is that prefix, so the rest of the code can keep
// writing '/api/...'. A 401 means the session ended (signed out elsewhere,
// expired, or revoked): go to sign-in and come back here afterwards.

export const BASE = (window.location.pathname.match(/^\/p\/[a-z0-9-]+(?=\/)/) || [''])[0];

export function apiUrl(path) {
  return BASE + path;
}

export function toSignIn() {
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
  // Never settles: the page is going away, and callers must not carry on.
  return new Promise(() => {});
}

export async function apiFetch(path, options) {
  const response = await fetch(apiUrl(path), options);
  if (response.status === 401) return toSignIn();
  if (!response.ok) {
    const error = new Error(response.status);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

// Always a JSON body, even an empty one: the server refuses state-changing
// requests that are not application/json, which is what keeps another site
// from submitting a form to this one.
export async function apiPost(path, data = {}) {
  const response = await fetch(apiUrl(path), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  if (response.status === 401) return toSignIn();
  return response;
}

export async function sessionInfo() {
  try {
    return await requestSession();
  } catch {
    return {mode: 'local'};
  }
}

export async function requestSession() {
  const response = await fetch('/auth/me', {signal: AbortSignal.timeout(8000)});
  if (response.status === 401) return {mode: 'hosted', signedOut: true};
  if (response.status >= 500) throw new Error(response.status);
  if (!response.ok) return {mode: 'local'};
  return response.json();
}

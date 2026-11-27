// The signed-in bar at the top of the sidebar: who you are, which project
// you are in (and a way to switch), and Sign out. Hosted mode only — local
// mode has no accounts and shows nothing.
import {BASE, sessionInfo, toSignIn} from './api.js';
import {esc} from './state.js';

export async function initSession() {
  const bar = document.getElementById('session-bar');
  if (!bar) return;
  const me = await sessionInfo();
  if (me.mode !== 'hosted') return;
  if (me.signedOut) {
    toSignIn();
    return;
  }
  const slug = BASE.replace(/^\/p\//, '');
  const projects = Array.isArray(me.projects) ? me.projects : [];
  const current = projects.find(project => project.slug === slug);
  if (current) document.title = `${current.name} · code-qda`;

  const options = projects.map(project =>
    `<option value="${esc(project.slug)}"${project.slug === slug ? ' selected' : ''}>${esc(project.name)}</option>`
  ).join('');
  bar.innerHTML = `
    <label class="session-project">
      <span class="visually-hidden">Project</span>
      <select id="session-project" aria-label="Project">${options}</select>
    </label>
    ${current?.sandbox ? '<p class="session-note">Only you can see your sandbox.</p>' : ''}
    <div class="session-who">
      <span title="${esc(me.email)}">${esc(me.handle || me.email)}</span>
      <a href="/">Projects</a>
      ${me.admin ? '<a href="/admin">Admin</a>' : ''}
      <button type="button" id="session-signout">Sign out</button>
    </div>`;
  bar.hidden = false;

  bar.querySelector('#session-project').addEventListener('change', event => {
    window.location.assign(`/p/${encodeURIComponent(event.target.value)}/`);
  });
  bar.querySelector('#session-signout').addEventListener('click', async () => {
    try {
      await fetch('/auth/logout', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
    } finally {
      window.location.assign('/login');
    }
  });
}

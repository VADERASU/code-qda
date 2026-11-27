// The signed-in landing page: this account's projects and sandbox, creating
// a project, and managing the members of the projects it owns.
import {h} from './dom.js';

let me = null;
let openMembers = new Set();    // slugs whose member list is expanded

function showError(message) {
  const el = document.getElementById('error');
  el.textContent = message || '';
  el.hidden = !message;
}

function showStatus(message) {
  const el = document.getElementById('status');
  el.textContent = message || '';
  el.hidden = !message;
}

async function post(path, data) {
  showError('');
  let response;
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
  } catch {
    showError('Could not reach the server.');
    return null;
  }
  if (response.status === 401) {
    window.location.assign('/login');
    return null;
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    showError(body.error || `Request failed (${response.status}).`);
    return null;
  }
  me = body;
  render();
  return body;
}

function membersPanel(project) {
  const list = h('ul', {class: 'members'});
  for (const email of project.members) {
    const isMe = email === me.email;
    list.append(h('li', {}, h('span', {}, email, isMe ? h('span', {class: 'muted'}, ' (you, owner)') : null),
      isMe ? null : h('button', {type: 'button', class: 'danger',
        onclick: () => post('/projects/api/members', {slug: project.slug, email, member: false})}, 'Remove')));
  }
  const input = h('input', {type: 'email', placeholder: 'their email', required: true,
    'aria-label': `Add someone to ${project.name}`});
  const add = h('form', {class: 'add-member', onsubmit: async event => {
    event.preventDefault();
    if (await post('/projects/api/members', {slug: project.slug, email: input.value, member: true})) {
      input.value = '';
    }
  }}, input, h('button', {type: 'submit'}, 'Add'));
  const details = h('details', {class: 'project-members', open: openMembers.has(project.slug)},
    h('summary', {}, `Members (${project.members.length})`),
    list, add,
    h('p', {class: 'muted'}, 'Only people with an account here can be added. To bring in someone new, ask an admin to invite them.'));
  details.addEventListener('toggle', () => {
    if (details.open) openMembers.add(project.slug);
    else openMembers.delete(project.slug);
  });
  return details;
}

function render() {
  document.getElementById('who').textContent = me.email;
  document.getElementById('admin-link').hidden = !me.admin;
  const list = document.getElementById('projects');
  list.replaceChildren();
  for (const project of me.projects || []) {
    const note = project.sandbox ? 'only you can see it; try things out here'
      : project.owned ? 'you own it' : null;
    const item = h('li', {},
      h('a', {href: `/p/${encodeURIComponent(project.slug)}/`}, project.name,
        note ? h('span', {class: 'muted project-note'}, ` · ${note}`) : null));
    if (project.owned) item.append(membersPanel(project));
    list.append(item);
  }
}

async function signOut() {
  try {
    await fetch('/auth/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
  } finally {
    window.location.assign('/login');
  }
}

document.getElementById('create-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type=submit]');
  showStatus('');
  button.disabled = true;
  try {
    const result = await post('/projects/api/create', {name: form.elements.name.value.trim()});
    if (result?.created) {
      form.reset();
      openMembers.add(result.created);
      render();
      const created = (me.projects || []).find(p => p.slug === result.created);
      showStatus(`Created ${created ? created.name : result.created}. Add people under Members, `
        + 'then open the project to upload transcripts.');
    }
  } finally {
    button.disabled = false;
  }
});

async function init() {
  document.getElementById('signout').addEventListener('click', signOut);
  const response = await fetch('/auth/me');
  if (response.status === 401) {
    window.location.assign('/login');
    return;
  }
  me = await response.json();
  render();
}

init();

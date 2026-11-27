// The signed-in landing page: this account's projects and sandbox, creating
// a project, and managing the members of the projects it owns.
import {h} from './dom.js';

let me = null;
const openMembers = new Set();
const newProject = document.getElementById('new-project');
const dialog = document.getElementById('new-project-dialog');
const form = document.getElementById('create-form');
const projectName = document.getElementById('project-name');
const cancelCreate = document.getElementById('cancel-create');
const submitCreate = form.querySelector('button[type=submit]');

function showMessage(id, message) {
  const el = document.getElementById(id);
  el.textContent = message;
  el.hidden = !message;
}

async function post(path, data, errorId = 'error') {
  showMessage(errorId, '');
  let response;
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
  } catch {
    showMessage(errorId, 'Could not reach the server.');
    return null;
  }
  if (response.status === 401) {
    window.location.assign('/login');
    return null;
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    showMessage(errorId, body.error || `Request failed (${response.status}).`);
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
  const add = h('form', {class: 'add-member', onsubmit: event => {
    event.preventDefault();
    post('/projects/api/members', {slug: project.slug, email: input.value, member: true});
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
  const projects = (me.projects || []).filter(project => !project.sandbox);
  document.getElementById('no-projects').hidden = projects.length > 0;
  for (const project of projects) {
    const item = h('li', {},
      h('a', {id: `project-${project.slug}`, class: 'project-row', href: `/p/${encodeURIComponent(project.slug)}/`},
        h('span', {class: 'project-icon', 'aria-hidden': 'true'}, h('span', {class: 'project-folder'})),
        h('span', {}, project.name), h('span', {class: 'project-arrow', 'aria-hidden': 'true'})));
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

newProject.addEventListener('click', () => {
  form.reset();
  projectName.setCustomValidity('');
  showMessage('create-error', '');
  dialog.showModal();
});
cancelCreate.addEventListener('click', () => dialog.close());
dialog.addEventListener('cancel', event => {
  if (submitCreate.disabled) event.preventDefault();
});
projectName.addEventListener('input', () => projectName.setCustomValidity(''));

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submitCreate.disabled) return;
  const name = projectName.value.trim();
  if (!name) {
    projectName.setCustomValidity('Enter a project name.');
    projectName.reportValidity();
    return;
  }
  showMessage('status', '');
  showMessage('error', '');
  submitCreate.disabled = cancelCreate.disabled = true;
  submitCreate.textContent = 'Creating…';
  try {
    const result = await post('/projects/api/create', {name}, 'create-error');
    if (result?.created) {
      dialog.close();
      showMessage('status', `Created ${name}.`);
      document.getElementById(`project-${result.created}`)?.focus();
    }
  } finally {
    submitCreate.disabled = cancelCreate.disabled = false;
    submitCreate.textContent = 'Create project';
  }
});

async function init() {
  document.getElementById('signout').addEventListener('click', signOut);
  try {
    const response = await fetch('/auth/me');
    if (response.status === 401) {
      window.location.assign('/login');
      return;
    }
    if (!response.ok) throw new Error(response.status);
    me = await response.json();
    render();
    newProject.disabled = false;
  } catch {
    showMessage('error', 'Could not load your projects. Reload to try again.');
  }
}

init();

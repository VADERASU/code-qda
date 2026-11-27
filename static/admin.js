// Admin page: invites, password resets, accounts, projects and membership.
// Everything is built with DOM APIs (textContent), never by pasting account
// data into HTML strings.

import {h} from './dom.js';

let state = null;

function when(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

function showError(message) {
  const el = document.getElementById('error');
  el.textContent = message || '';
  el.hidden = !message;
  if (message) el.scrollIntoView({block: 'nearest'});
}

async function call(path, data) {
  showError('');
  let response;
  try {
    response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      headers: data === undefined ? {} : {'Content-Type': 'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data)
    });
  } catch {
    showError('Could not reach the server.');
    return null;
  }
  if (response.status === 401) {
    window.location.assign('/login?next=%2Fadmin');
    return null;
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    showError(body.error || `Request failed (${response.status}).`);
    return null;
  }
  state = body;
  render();
  return body;
}

function showLink(label, link) {
  document.getElementById('link-label').textContent = label;
  document.getElementById('link-value').value = link;
  document.getElementById('link-out').hidden = false;
  document.getElementById('link-value').select();
}

function renderInviteProjects() {
  const box = document.getElementById('invite-projects');
  const checked = new Set([...box.querySelectorAll('input:checked')].map(i => i.value));
  box.replaceChildren(h('legend', {}, 'Projects'));
  if (!state.projects.length) {
    box.append(h('p', {class: 'muted'}, 'No projects yet — create one below first.'));
  }
  for (const project of state.projects) {
    box.append(h('label', {class: 'check'},
      h('input', {type: 'checkbox', value: project.slug, checked: checked.has(project.slug)}),
      `${project.name} (${project.slug})`));
  }
}

function renderInvites() {
  const body = document.getElementById('invites');
  body.replaceChildren();
  if (!state.invites.length) {
    body.append(h('tr', {}, h('td', {colspan: 4, class: 'muted'}, 'None')));
  }
  for (const invite of state.invites) {
    body.append(h('tr', {},
      h('td', {}, invite.email),
      h('td', {}, invite.kind === 'reset' ? 'password reset' : invite.admin ? 'invite (admin)' : 'invite'),
      h('td', {}, when(invite.expires)),
      h('td', {class: 'actions'},
        h('button', {type: 'button', class: 'danger',
          onclick: () => call('/admin/api/invite/revoke', {id: invite.id})}, 'Revoke'))));
  }
}

function renderUsers() {
  const body = document.getElementById('users');
  body.replaceChildren();
  if (!state.users.length) {
    body.append(h('tr', {}, h('td', {colspan: 5, class: 'muted'}, 'None')));
  }
  for (const user of state.users) {
    const me = user.email === state.me;
    const role = [h('span', {class: 'tag'}, user.admin ? 'admin' : 'editor')];
    if (user.disabled) role.push(' ', h('span', {class: 'tag off'}, 'disabled'));
    body.append(h('tr', {},
      h('td', {}, user.email, me ? h('span', {class: 'muted'}, ' (you)') : null),
      h('td', {}, user.handle || ''),
      h('td', {}, role),
      h('td', {}, when(user.lastSeen)),
      h('td', {class: 'actions'},
        h('button', {type: 'button', onclick: async () => {
          const result = await call('/admin/api/reset', {email: user.email});
          if (result?.link) showLink(`Password reset link for ${user.email} (valid 24 hours):`, result.link);
        }}, 'Reset link'),
        h('button', {type: 'button', title: 'End all of their current sessions',
          onclick: () => call('/admin/api/user', {email: user.email, revokeSessions: true})}, 'Sign out'),
        me ? null : h('button', {type: 'button', class: user.disabled ? '' : 'danger',
          onclick: () => call('/admin/api/user', {email: user.email, disabled: !user.disabled})},
        user.disabled ? 'Enable' : 'Disable'))));
  }
}

function renderProjects() {
  const box = document.getElementById('projects');
  box.replaceChildren();
  if (!state.projects.length) box.append(h('p', {class: 'muted'}, 'None yet.'));
  for (const project of state.projects) {
    const members = h('ul', {class: 'members'});
    for (const email of project.members) {
      members.append(h('li', {}, email,
        h('button', {type: 'button', class: 'danger',
          onclick: () => call('/admin/api/members', {slug: project.slug, email, member: false})}, 'Remove')));
    }
    if (!project.members.length) members.append(h('li', {class: 'muted'}, 'No members'));
    const input = h('input', {type: 'email', placeholder: 'email to add', 'aria-label': `Add a member to ${project.name}`,
      list: 'known-emails'});
    const add = h('form', {class: 'add-member', onsubmit: async event => {
      event.preventDefault();
      if (await call('/admin/api/members', {slug: project.slug, email: input.value, member: true})) input.value = '';
    }}, input, h('button', {type: 'submit'}, 'Add'));
    box.append(h('div', {class: 'project'},
      h('div', {class: 'project-head'},
        h('strong', {}, project.name, ' ', h('span', {class: 'muted'}, `/p/${project.slug}/`),
          project.owner ? h('span', {class: 'muted'}, ` · created by ${project.owner}`) : null),
        project.members.includes(state.me) ? h('a', {href: `/p/${encodeURIComponent(project.slug)}/`}, 'Open') : null),
      h('details', {class: 'project-storage'},
        h('summary', {}, project.storage === 'managed' ? 'Managed storage' : 'Registered folders'),
        h('p', {}, 'Transcripts ', h('code', {}, project.dataDir)),
        h('p', {}, 'Audio ', h('code', {}, project.audioDir))),
      members, add));
  }
  let datalist = document.getElementById('known-emails');
  if (!datalist) {
    datalist = h('datalist', {id: 'known-emails'});
    document.body.append(datalist);
  }
  datalist.replaceChildren(...state.users.map(user => h('option', {value: user.email})));
}

function renderRoots() {
  const roots = state.roots || {data: [], audio: []};
  const canRegister = roots.data.length > 0 && roots.audio.length > 0;
  document.getElementById('register-project-form').hidden = !canRegister;
  const el = document.getElementById('roots');
  if (!canRegister) {
    el.replaceChildren('Configure transcript and audio roots on the server to register folders here. '
      + 'You can also register them on the server with ', h('code', {}, 'python3 app.py admin project add'), '.');
  } else {
    el.textContent = `Use existing absolute paths. Transcripts must be inside ${roots.data.join(' or ')}. `
      + `Audio must be inside ${roots.audio.join(' or ')}.`;
  }
}

function render() {
  if (!state) return;
  renderInviteProjects();
  renderInvites();
  renderUsers();
  renderProjects();
  renderRoots();
}

document.getElementById('invite-form').addEventListener('submit', async event => {
  event.preventDefault();
  const email = document.getElementById('invite-email').value;
  const projects = [...document.querySelectorAll('#invite-projects input:checked')].map(i => i.value);
  const result = await call('/admin/api/invite', {
    email,
    handle: document.getElementById('invite-handle').value.trim() || null,
    admin: document.getElementById('invite-admin').checked,
    projects
  });
  if (result?.link) {
    showLink(`Invite link for ${result.linkEmail} (valid 7 days):`, result.link);
    event.target.reset();
    renderInviteProjects();
  }
});

document.getElementById('link-copy').addEventListener('click', async () => {
  const input = document.getElementById('link-value');
  try {
    await navigator.clipboard.writeText(input.value);
    document.getElementById('link-copy').textContent = 'Copied';
    setTimeout(() => { document.getElementById('link-copy').textContent = 'Copy'; }, 1500);
  } catch {
    input.select();
  }
});

function bindProjectForm(id, endpoint, successMessage) {
  const form = document.getElementById(id);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value.trim()]));
    const status = document.getElementById('project-status');
    status.hidden = true;
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      if (await call(endpoint, data)) {
        form.reset();
        status.textContent = successMessage;
        status.hidden = false;
      }
    } finally {
      button.disabled = false;
    }
  });
}

bindProjectForm('project-form', '/admin/api/project/create', 'Project created. You can add members in the project list.');
bindProjectForm('register-project-form', '/admin/api/project', 'Project registered. You can add members in the project list.');

call('/admin/api/state');

// The signed-in landing page: the projects this account belongs to.

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

async function init() {
  document.getElementById('signout').addEventListener('click', signOut);
  const response = await fetch('/auth/me');
  if (response.status === 401) {
    window.location.assign('/login');
    return;
  }
  const me = await response.json();
  document.getElementById('who').textContent = me.email;
  document.getElementById('admin-link').hidden = !me.admin;
  const list = document.getElementById('projects');
  for (const project of me.projects || []) {
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = `/p/${encodeURIComponent(project.slug)}/`;
    link.textContent = project.name;
    item.append(link);
    list.append(item);
  }
  document.getElementById('empty').hidden = (me.projects || []).length > 0;
}

init();

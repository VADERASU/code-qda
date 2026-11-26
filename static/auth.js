// Sign-in and invite/reset pages. No inline script: the pages' CSP forbids it.

async function postJson(path, data) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  const body = await response.json().catch(() => ({}));
  return {ok: response.ok, status: response.status, body};
}

function showError(message) {
  const el = document.getElementById('error');
  el.textContent = message;
  el.hidden = !message;
}

function initLogin() {
  const form = document.getElementById('login-form');
  const next = new URLSearchParams(window.location.search).get('next') || '/';
  form.addEventListener('submit', async event => {
    event.preventDefault();
    showError('');
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const result = await postJson('/auth/login', {
        email: form.email.value,
        password: form.password.value,
        next
      });
      if (result.ok) {
        window.location.assign(result.body.next || '/');
        return;
      }
      showError(result.body.error || 'Sign-in failed. Try again.');
      form.password.select();
    } catch {
      showError('Could not reach the server. Check your connection and try again.');
    } finally {
      button.disabled = false;
    }
  });
}

async function initInvite() {
  // The token travels in the URL fragment, which browsers never send to a
  // server or put in a Referer. Take it, then drop it from the address bar
  // and history so it isn't left lying around.
  const token = window.location.hash.slice(1);
  window.history.replaceState(null, '', '/invite');
  const title = document.getElementById('invite-title');
  const lede = document.getElementById('invite-lede');
  const form = document.getElementById('invite-form');

  if (!token) {
    lede.textContent = 'This page needs the full link from your invitation email.';
    return;
  }
  let info;
  try {
    info = await postJson('/auth/invite/info', {token});
  } catch {
    lede.textContent = 'Could not reach the server. Reload to try again.';
    return;
  }
  if (!info.ok) {
    lede.textContent = info.body.error || 'This link is invalid, expired, or already used.';
    const foot = document.createElement('p');
    foot.className = 'foot';
    foot.textContent = 'Ask the person who sent it for a new one.';
    lede.after(foot);
    return;
  }
  const reset = info.body.kind === 'reset';
  title.textContent = reset ? 'Choose a new password' : 'Welcome — set your password';
  lede.textContent = reset
    ? 'Your old password stops working, and you are signed out everywhere else.'
    : `You'll appear in transcript edit history as “${info.body.handle}”.`;
  form.email.value = info.body.email;
  form.hidden = false;
  form.password.focus();

  form.addEventListener('submit', async event => {
    event.preventDefault();
    showError('');
    if (form.password.value.length < 12) {
      showError('Use at least 12 characters.');
      return;
    }
    if (form.password.value !== form.confirm.value) {
      showError("The two passwords don't match.");
      return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const result = await postJson('/auth/invite/accept', {token, password: form.password.value});
      if (result.ok) {
        window.location.assign('/');
        return;
      }
      showError(result.body.error || 'That did not work. Try again.');
    } catch {
      showError('Could not reach the server. Check your connection and try again.');
    } finally {
      button.disabled = false;
    }
  });
}

if (document.body.dataset.page === 'login') initLogin();
if (document.body.dataset.page === 'invite') initInvite();

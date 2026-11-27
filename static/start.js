// A failed module import is remembered for this page, including failed
// dependencies. Give a transient asset failure one fresh page load.
import('./app.js').then(({start}) => {
  if (history.state?.retryingAppLoad) history.replaceState(null, '');
  // An older service worker can still be serving the previous app module.
  if (start) start();
  else document.getElementById('open-status').textContent = 'No transcript open';
}, () => {
  if (navigator.onLine && !history.state?.retryingAppLoad) {
    history.replaceState({retryingAppLoad: true}, '');
    setTimeout(() => location.reload(), 500);
    return;
  }
  document.getElementById('open-status').textContent = 'Could not load the coding workspace.';
  const retry = document.getElementById('retry-open');
  retry.hidden = false;
  retry.onclick = () => location.reload();
});

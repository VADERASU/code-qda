// The server supplies the cache version and the list of public page assets.
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});

self.addEventListener('activate', event => {
  event.waitUntil(Promise.all([
    self.clients.claim(),
    caches.keys().then(keys => Promise.all(keys
      .filter(key => key.startsWith('qda-shell-') && key !== CACHE)
      .map(key => caches.delete(key))))
  ]));
});

async function openPage(request) {
  try {
    const response = await fetch(request, {signal: AbortSignal.timeout(8000)});
    if (response.status < 500) return response;
  } catch {
    // A dropped connection can leave the browser reporting that it is online.
  }
  const cache = await caches.open(CACHE);
  return await cache.match('/offline-shell') || Response.error();
}

self.addEventListener('fetch', event => {
  const {request} = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate'
      && (url.pathname === '/' || /^\/p\/[a-z0-9-]+\/(?:index\.html)?$/.test(url.pathname))) {
    event.respondWith(openPage(request));
  } else if (ASSETS.includes(url.pathname) && url.pathname.startsWith('/static/')) {
    event.respondWith(caches.open(CACHE).then(async cache =>
      await cache.match(url.pathname) || fetch(request)));
  }
});

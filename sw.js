// Service Worker mínimo para PWA install + offline shell.
// No cachea la data (/api/*) para que siempre vea estado fresco.
const SHELL = ['/', '/index.html', '/stats.html', '/config.html', '/manifest.json'];
const CACHE = 'revolv-shell-v4';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // API: siempre red, nunca cache
  if (url.pathname.startsWith('/api/')) return;
  // Otros: red primero, cache fallback (offline)
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then(r => r || caches.match('/index.html')))
  );
});

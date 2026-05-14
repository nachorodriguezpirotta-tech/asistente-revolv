// Service Worker mínimo para PWA install + offline shell.
// No cachea la data (/api/*) para que siempre vea estado fresco.
const SHELL = ['/', '/index.html', '/stats.html', '/config.html', '/manifest.json'];
const CACHE = 'revolv-shell-v8';

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

// PUSH NOTIFICATIONS
self.addEventListener('push', event => {
  let data = { title: 'Asistente Revolv', body: 'Nueva notificación', url: '/' };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      tag: data.tag || 'default',
      data: { url: data.url },
      vibrate: [200, 100, 200],
      requireInteraction: false,
    })
  );
});

// Click en la notificación → abrir/focusear la app
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      // Si la app ya está abierta, focus
      for (const c of list) {
        if (c.url.includes(self.location.origin)) {
          c.focus();
          if (c.navigate) c.navigate(url);
          return;
        }
      }
      // Si no está abierta, abrir
      return self.clients.openWindow(url);
    })
  );
});

// Service Worker para Asistente Revolv (v10).
// Funciones:
//   1. Push notifications (recibe del backend vía VAPID y muestra notif local)
//   2. Click en notif → abre el dashboard del editor/admin correspondiente
//   3. NO cachea nada (fetch va siempre a la red para evitar contenido obsoleto)
//
// IMPORTANTE: el SW v9 anterior era un "kill switch" que se desregistraba
// solo. Por eso pushManager.subscribe fallaba con "no active Service Worker".
// Esta versión queda viva y maneja los eventos push correctamente.

const SW_VERSION = 'v10-push-ready';

self.addEventListener('install', (event) => {
  // Activarse de inmediato sin esperar a que se cierren pestañas viejas
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Limpiar TODO cache previo del kill-switch o versiones anteriores
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    } catch (_) {}
    // Tomar control de todas las pestañas/PWAs abiertas de una sola vez
    await self.clients.claim();
  })());
});

// No interceptar fetch → todo va a la red (sin caché)
self.addEventListener('fetch', () => {});

// ───── PUSH ─────
self.addEventListener('push', (event) => {
  let payload = {};
  if (event.data) {
    try { payload = event.data.json(); }
    catch (_) {
      try { payload = { title: 'Asistente Revolv', body: event.data.text() }; }
      catch (__) { payload = { title: 'Asistente Revolv', body: 'Tenés algo nuevo' }; }
    }
  }
  const title = payload.title || 'Asistente Revolv';
  const options = {
    body: payload.body || '',
    icon: payload.icon || '/icon-192.png',
    badge: payload.badge || '/icon-192.png',
    tag: payload.tag || 'revolv-notif',
    data: {
      url: payload.url || '/',
      sentAt: Date.now(),
    },
    requireInteraction: false,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

// ───── CLICK EN NOTIF ─────
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // Si hay una pestaña abierta de este origin → enfocarla y navegar
    for (const c of clients) {
      try {
        if (c.url.includes(self.location.origin)) {
          await c.focus();
          if ('navigate' in c) {
            try { await c.navigate(targetUrl); } catch (_) {}
          }
          return;
        }
      } catch (_) {}
    }
    // Si no había ninguna pestaña → abrir una nueva
    if (self.clients.openWindow) {
      await self.clients.openWindow(targetUrl);
    }
  })());
});

// SW KILL SWITCH (v9): este SW se autodestruye + limpia todos los caches.
// Cuando el browser detecte cambio respecto del SW viejo, instala este,
// que mata todo cache y se desregistra. Próxima carga: todo de red, limpio.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    // 1. Borrar TODOS los caches
    const keys = await caches.keys();
    await Promise.all(keys.map(k => caches.delete(k)));
    // 2. Desregistrar este SW
    await self.registration.unregister();
    // 3. Recargar todas las pestañas/PWAs activas
    const clients = await self.clients.matchAll({ type: 'window' });
    for (const c of clients) {
      try { c.navigate(c.url); } catch (_) {}
    }
  })());
});

// No interceptar nada — todo va a la red
self.addEventListener('fetch', () => {});

// VN Edge Service Worker — offline support + asset caching
const CACHE_NAME = 'vnedge-v1';
const STATIC_ASSETS = [
  '/',
  '/static/css/design-system.css',
  '/static/css/components.css',
  '/static/css/layout.css',
  '/static/js/api.js',
  '/static/js/core.js',
  '/static/js/app.js',
  '/static/js/admin.js',
  '/static/logo-white.jpg',
  '/static/logo-dark.jpg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API requests: network-first, fall back to cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets: cache-first
  if (url.pathname.startsWith('/static/') || url.pathname === '/') {
    event.respondWith(
      caches.match(event.request).then(cached =>
        cached || fetch(event.request).then(resp => {
          const cloned = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, cloned));
          return resp;
        })
      )
    );
  }
});

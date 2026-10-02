// Money Mgr web service worker, served at /sw.js so it controls the whole
// app. Pages hold financial data and are sent no-store, so nothing but the
// offline page is cached: every request goes to the network, and a page
// that can't load shows the offline page instead of the browser's error.
const CACHE = 'mmw-v1';
const OFFLINE = '/static/offline.html';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add(OFFLINE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Form posts (uploads, sync) and API calls go straight to the network.
  if (e.request.mode !== 'navigate' || e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
});

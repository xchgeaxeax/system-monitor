/* System Monitor service worker
 * - Caches the dashboard shell for offline launch (standalone/PWA mode).
 * - GET /api/* responses are cached stale-while-revalidate so the panel
 *   still renders (with last-known data) when the network drops.
 * - Never caches 401/403, non-GET, or responses without a body.
 * - Shell is network-first: a server-side dashboard update must be visible
 *   on the next load, not hidden behind a stale cache until the cache
 *   name changes. The cache is the offline fallback only.
 */
const CACHE = 'sysmon-v3';
const SHELL = ['/', '/sw.js', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return; // pass through
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API: network-first, fall back to cache (offline), update cache in bg.
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(req, clone)).catch(() => {});
          }
          return res;
        })
        .catch(() =>
          caches.match(req).then((hit) => hit || Response.error())
        )
    );
    return;
  }

  // Shell: network-first, fall back to cache. Cache-first would pin users to
  // an old dashboard.html after a server upgrade (the page is served from the
  // server with no strong caching, so going to the network is cheap).
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(req, clone)).catch(() => {});
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) => hit || Response.error())
      )
  );
});

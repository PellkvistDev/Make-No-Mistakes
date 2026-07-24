/* Service worker — makes the app installable and available offline.
 *
 * Strategy: NETWORK-FIRST for our own same-origin files, with a cache fallback.
 * A new deploy is therefore picked up on the next load when online, while the
 * app still opens offline from the last-cached shell. (The previous cache-first
 * version could pin a phone to a stale build indefinitely.)
 *
 * SECURITY: only same-origin GETs are ever cached — our own static shell. Calls
 * to the model API and GitHub (cross-origin, and/or non-GET) bypass the SW
 * entirely and are never stored.
 */
const CACHE = "mnm-shell-v3";
const SHELL = [
  "./index.html",
  "./app.js",
  "./agent-core.js",
  "./style.css",
  "./manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return; // API traffic bypasses the SW
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req, { ignoreSearch: true })
          .then((hit) => hit || caches.match("./index.html")) // offline: fall back to the app shell
      )
  );
});

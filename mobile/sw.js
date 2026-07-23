/* Service worker — makes the app installable and available offline.
 *
 * SECURITY: this SW only ever caches the static app shell (our own same-origin
 * files). It NEVER touches requests to the model API or GitHub — those are
 * network-only and their responses (which carry your code and would carry auth)
 * are never stored. Cross-origin and non-GET requests bypass the cache entirely.
 */
const CACHE = "mnm-shell-v1";
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
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  // Only serve our own same-origin GETs from cache. Everything else — every API
  // call — goes straight to the network and is never cached.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  e.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) => hit || fetch(req))
  );
});

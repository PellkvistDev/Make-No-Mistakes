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
const CACHE = "mnm-shell-v7";   // bumped: forces a fresh shell onto phones that
                                // are sitting on an HTTP-cached copy of the old
                                // one. v7: adds the push handlers -- a phone on
                                // v6 has a worker that receives a push and does
                                // nothing with it.
const SHELL = [
  "./index.html",
  "./app.js",
  "./agent-core.js",
  "./style.css",
  "./manifest.webmanifest",
  // Precached so the home-screen icon survives being installed offline, and
  // because a missing icon is not something the app can report.
  "./icon-180.png",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-512.png",
  // Precached so pairing works on a phone that's offline — which is exactly
  // when you're setting one up. Only loaded when the scanner actually opens.
  "./vendor/jsQR.js",
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

/* Web Push.
 *
 * The one thing a suspended PWA can still do. It cannot RUN anything -- WebKit
 * has never shipped Background Sync or Background Fetch and this does not
 * change that -- but the desktop finishing a turn the phone was suspended
 * through is exactly the case where a notification is the whole answer. Before
 * this, you found out by opening the app and looking.
 *
 * The payload arrives encrypted (RFC 8291) and the browser has already
 * decrypted it by the time it reaches here. The push service never saw it.
 */
self.addEventListener("push", (e) => {
  let data = {};
  // A push with no body, or a body that isn't ours, still has to raise
  // something: iOS revokes the subscription of a service worker that receives
  // a push and shows nothing (userVisibleOnly is a promise, not a hint).
  try { data = e.data ? e.data.json() : {}; } catch (err) { data = {}; }
  const title = data.title || "Make No Mistakes";
  e.waitUntil(self.registration.showNotification(title, {
    body: data.body || "Something finished on your desktop.",
    icon: "./icon-192.png",
    badge: "./icon-192.png",
    // Same tag replaces rather than stacks: three finished workers should not
    // leave three notifications to dismiss one at a time.
    tag: data.tag || "mnm",
    data: { chatId: data.chatId || "" },
  }));
});

/* Tapping it should land you where the thing happened, not on a cold start of
 * whatever was open last. An already-running window is focused rather than
 * replaced -- opening a second one would leave the chat you were reading
 * behind in a window you can no longer see. */
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const chatId = (e.notification.data || {}).chatId || "";
  const url = chatId ? `./?chat=${encodeURIComponent(chatId)}` : "./";
  e.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window",
                                               includeUncontrolled: true });
    for (const client of open) {
      if ("focus" in client) {
        if (chatId && "navigate" in client) {
          try { await client.navigate(url); } catch (err) { /* cross-origin */ }
        }
        return client.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return; // API traffic bypasses the SW
  e.respondWith(
    // "no-cache" = always ask the server, using If-None-Match. Without it,
    // fetch() is served straight out of the browser's own HTTP cache while the
    // response is still fresh -- and GitHub Pages sends a max-age -- so
    // network-first quietly degrades into HTTP-cache-first and a new deploy is
    // invisible for the length of that window, restarts included. Revalidating
    // costs one conditional request that answers 304 when nothing changed.
    fetch(req, { cache: "no-cache" })
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

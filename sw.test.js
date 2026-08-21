/* The service worker, driven directly.
 *
 * It decides what reaches the network and what is served from a cache, which
 * is the difference between a deploy arriving and a phone sitting on a stale
 * build across restarts. That decision had no test of any kind.
 *
 * sw.js is a classic worker script, not a module: it registers handlers on a
 * global `self` and expects `caches` and `fetch` to exist. So it is run in a vm
 * context with those stubbed, and the handlers it registers are then called.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ORIGIN = "https://example.test";
const SRC = fs.readFileSync(path.join(__dirname, "sw.js"), "utf8");

/** Load sw.js and return its handlers plus the calls its stubs recorded. */
function loadWorker() {
  const calls = { fetch: [], put: [], match: [], deleted: [] };
  const listeners = {};
  const cache = {
    addAll: async () => {},
    put: async (req, res) => { calls.put.push([req, res]); },
    match: async (req, opts) => { calls.match.push([req, opts]); return undefined; },
  };
  calls.notifications = [];
  calls.focused = [];
  calls.opened = [];
  calls.navigated = [];
  const windows = [];
  const self = {
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting: async () => {},
    registration: {
      showNotification: async (title, opts) => {
        calls.notifications.push([title, opts]);
      },
    },
    clients: {
      claim: async () => {},
      matchAll: async () => windows,
      openWindow: async (url) => { calls.opened.push(url); },
    },
    location: { origin: ORIGIN },
  };
  self.__windows = windows;
  self.__calls = calls;
  const sandbox = {
    self,
    URL,
    caches: {
      open: async () => cache,
      keys: async () => ["mnm-shell-old"],
      delete: async (k) => { calls.deleted.push(k); },
      match: async (req, opts) => { calls.match.push([req, opts]); return undefined; },
    },
    fetch: async (req, init) => {
      calls.fetch.push([req, init]);
      return { ok: true, clone() { return this; } };
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { listeners, calls, sandbox, self };
}

/** Fire the fetch handler for one request and resolve what it responded with. */
async function handleFetch(worker, request) {
  let responded;
  const event = { request, respondWith: (p) => { responded = p; } };
  worker.listeners.fetch(event);
  return responded === undefined ? undefined : await responded;
}

test("fetch: a same-origin GET revalidates instead of using the HTTP cache", async () => {
  const worker = loadWorker();
  await handleFetch(worker, { method: "GET", url: ORIGIN + "/app.js" });

  assert.strictEqual(worker.calls.fetch.length, 1, "the request never reached the network");
  const init = worker.calls.fetch[0][1];
  assert.ok(init && init.cache === "no-cache",
    "network-first has to bypass the browser's HTTP cache, or a fresh response " +
    "is served from it for the whole max-age window and a new deploy is " +
    "invisible across restarts. Got init = " + JSON.stringify(init));
});

test("fetch: the same-origin response is cached for offline use", async () => {
  const worker = loadWorker();
  await handleFetch(worker, { method: "GET", url: ORIGIN + "/app.js" });
  assert.strictEqual(worker.calls.put.length, 1, "nothing was written to the shell cache");
});

test("fetch: cross-origin traffic is not touched at all", async () => {
  const worker = loadWorker();
  const out = await handleFetch(worker, { method: "GET", url: "https://api.github.com/user" });
  assert.strictEqual(out, undefined, "respondWith was called for a cross-origin request");
  assert.strictEqual(worker.calls.fetch.length, 0, "the SW intercepted API traffic");
  assert.strictEqual(worker.calls.put.length, 0, "an API response was written to the cache");
});

test("fetch: a non-GET is not touched either", async () => {
  const worker = loadWorker();
  const out = await handleFetch(worker, { method: "POST", url: ORIGIN + "/app.js" });
  assert.strictEqual(out, undefined, "respondWith was called for a POST");
  assert.strictEqual(worker.calls.put.length, 0, "a POST response was cached");
});

test("fetch: falls back to the cache when the network is gone", async () => {
  const worker = loadWorker();
  worker.sandbox.fetch = async () => { throw new Error("offline"); };
  await handleFetch(worker, { method: "GET", url: ORIGIN + "/app.js" });
  assert.ok(worker.calls.match.length >= 1,
    "offline, the request has to fall back to the cached shell");
});

test("activate: caches from older builds are deleted", async () => {
  const worker = loadWorker();
  let done;
  worker.listeners.activate({ waitUntil: (p) => { done = p; } });
  await done;
  assert.deepStrictEqual(worker.calls.deleted, ["mnm-shell-old"]);
});


/* ------------------------------------------------------------------ push --
 *
 * The desktop finishes turns the phone was suspended through, and the phone
 * was never told -- you found out by opening the app and looking. A push
 * cannot RUN anything on iOS, but it can say so, which is the whole gap.
 */

/** Fire the push handler with a payload and wait for its work. */
async function handlePush(worker, payload) {
  let waited;
  worker.listeners.push({
    data: payload === undefined
      ? null
      : { json: () => { if (payload === "BAD") throw new Error("not json");
                        return payload; } },
    waitUntil: (p) => { waited = p; },
  });
  if (waited) await waited;
}

test("push: the message becomes a notification", async () => {
  const worker = loadWorker();
  await handlePush(worker, { title: "Worker finished", body: "Rewrote the README" });

  assert.strictEqual(worker.calls.notifications.length, 1);
  const [title, opts] = worker.calls.notifications[0];
  assert.strictEqual(title, "Worker finished");
  assert.strictEqual(opts.body, "Rewrote the README");
});

test("push: a body-less push still shows something", async () => {
  // iOS revokes the subscription of a worker that receives a push and shows
  // nothing -- userVisibleOnly is a promise, not a hint. Staying silent to
  // avoid a vague notification costs every LATER notification too.
  const worker = loadWorker();
  await handlePush(worker, undefined);

  assert.strictEqual(worker.calls.notifications.length, 1,
    "a push with no payload must still raise a notification");
});

test("push: an unreadable payload still shows something", async () => {
  const worker = loadWorker();
  await handlePush(worker, "BAD");

  assert.strictEqual(worker.calls.notifications.length, 1);
});

test("push: notifications with the same tag replace rather than stack", async () => {
  const worker = loadWorker();
  await handlePush(worker, { title: "a", tag: "chat-7" });
  const [, opts] = worker.calls.notifications[0];
  assert.strictEqual(opts.tag, "chat-7",
    "three finished workers should not leave three notifications to dismiss");
});

/** Fire notificationclick and wait for its work. */
async function handleClick(worker, data) {
  let waited;
  worker.listeners.notificationclick({
    notification: { data, close: () => {} },
    waitUntil: (p) => { waited = p; },
  });
  if (waited) await waited;
}

test("click: an open window is focused, not replaced", async () => {
  // Opening a second window leaves the chat you were reading in one you can no
  // longer see.
  const worker = loadWorker();
  const focused = [];
  worker.self.__windows.push({
    focus: () => { focused.push(true); },
    navigate: async (url) => { worker.calls.navigated.push(url); },
  });

  await handleClick(worker, { chatId: "c1" });

  assert.strictEqual(focused.length, 1, "the existing window should be focused");
  assert.strictEqual(worker.calls.opened.length, 0, "nothing new should open");
  assert.ok(worker.calls.navigated[0].includes("chat=c1"),
    "it should land on the chat the notification was about");
});

test("click: with nothing open, a window is opened", async () => {
  const worker = loadWorker();
  await handleClick(worker, { chatId: "c2" });

  assert.strictEqual(worker.calls.opened.length, 1);
  assert.ok(worker.calls.opened[0].includes("chat=c2"));
});

test("click: a notification with no chat still opens the app", async () => {
  const worker = loadWorker();
  await handleClick(worker, {});

  assert.strictEqual(worker.calls.opened.length, 1);
  assert.strictEqual(worker.calls.opened[0], "./");
});

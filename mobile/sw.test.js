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
  const self = {
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting: async () => {},
    clients: { claim: async () => {} },
    location: { origin: ORIGIN },
  };
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
  return { listeners, calls, sandbox };
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

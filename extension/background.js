// The extension half of "drive the browser I already have open".
//
// Chrome's DevTools port can only be opened at LAUNCH, so nothing outside the
// browser can attach to a window that is already running. An extension is
// already inside it -- same profile, same logins, same tabs -- so the
// connection runs the other way: the browser dials the app.
//
// WHY A WEBSOCKET, and not a fetch loop: an MV3 service worker is killed after
// 30 seconds idle, and WebSocket activity is the documented thing that resets
// that timer. A polling loop would have the worker dying underneath it every
// half minute.

// Must stay identical to PORTS in glmcode/extension_bridge.py.
const PORTS = [8765, 8766, 8767, 8768, 8769];
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 15000;

let socket = null;
let portIndex = 0;
let retryMs = RETRY_MIN_MS;
let paused = false;

// Frames injected once per frame define globalThis.__mnmAct there; the
// isolated world persists for the life of the frame, so later calls can just
// invoke it. Tracked per tab so a navigation re-injects.
const injected = new Map();

function setBadge(state) {
  const map = {
    on:      { text: "",    color: "#1f9d55", title: "Connected — Make No Mistakes can drive this tab" },
    off:     { text: "!",   color: "#8a8f98", title: "Make No Mistakes isn't running" },
    paused:  { text: "||",  color: "#c98a00", title: "Paused — click to let the app drive again" },
    working: { text: "···", color: "#0a84ff", title: "Make No Mistakes is driving this tab" },
  };
  const s = map[state] || map.off;
  chrome.action.setBadgeText({ text: s.text });
  chrome.action.setBadgeBackgroundColor({ color: s.color });
  chrome.action.setTitle({ title: s.title });
}

function connect() {
  if (paused || socket) return;
  const port = PORTS[portIndex % PORTS.length];
  let ws;
  try {
    ws = new WebSocket(`ws://127.0.0.1:${port}/`);
  } catch (e) {
    scheduleRetry();
    return;
  }
  socket = ws;

  ws.onopen = () => {
    // Every port in the list gets tried in turn, so a port taken by something
    // else costs a second rather than the feature.
    retryMs = RETRY_MIN_MS;
    chrome.alarms.clear("reconnect");
    setBadge("on");
    send({ type: "hello", agent: "mnm-extension", version:
           chrome.runtime.getManifest().version });
  };
  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (!msg || !msg.id) return;
    setBadge("working");
    try {
      const result = await run(msg.command, msg.params || {});
      send({ id: msg.id, result });
    } catch (e) {
      // A failed action is something the model should read and think about,
      // never a dropped socket.
      send({ id: msg.id, error: String((e && e.message) || e) });
    }
    setBadge(paused ? "paused" : "on");
  };
  ws.onclose = () => { socket = null; setBadge(paused ? "paused" : "off"); scheduleRetry(); };
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// An MV3 service worker is killed after 30 seconds idle, and a pending
// setTimeout does NOT survive that. Relying on one meant: install the
// extension before the app is listening, retry for half a minute, get
// terminated, and never reconnect -- the extension sits there dead while the
// app waits for a connection that will never come, and control_chrome quietly
// launches a separate browser instead. An alarm is the one timer that outlives
// the worker; the timeout is kept for the fast retries inside a live worker.
function scheduleRetry() {
  if (paused) return;
  portIndex += 1;
  const wait = retryMs;
  retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
  setTimeout(connect, wait);
  chrome.alarms.create("reconnect", { periodInMinutes: 0.5 });
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "reconnect") connect();
});

// Anything the user does in the browser wakes the worker, so a reconnect
// happens the moment they touch a tab rather than up to half a minute later.
// connect() is a no-op when a socket is already open.
chrome.tabs.onActivated.addListener(() => connect());
chrome.windows.onFocusChanged.addListener(() => connect());

function send(obj) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj));
}

// -- which tab ------------------------------------------------------------ //

// A tab the agent has explicitly switched to. Without this, "work in my
// browser" silently meant "whatever tab is in front right now", so the user
// switching tabs mid-task moved the agent with them.
let pinnedTabId = null;

function tabInfo(t, active) {
  return { id: t.id, title: t.title || "", url: t.url || "",
           active: !!active, windowId: t.windowId,
           usable: !/^(chrome|edge|about|devtools|chrome-extension):/i.test(t.url || "") };
}

async function listTabs() {
  const tabs = await chrome.tabs.query({});
  const focused = (await chrome.tabs.query({ active: true, lastFocusedWindow: true }))[0];
  const driving = pinnedTabId != null ? pinnedTabId : (focused && focused.id);
  return tabs.map((t) => tabInfo(t, t.id === driving));
}

async function activeTab() {
  // A pinned tab wins, so a long task does not follow the user around as they
  // switch tabs. It is dropped as soon as it stops existing.
  if (pinnedTabId != null) {
    try {
      const t = await chrome.tabs.get(pinnedTabId);
      if (t) return t;
    } catch {
      pinnedTabId = null;
    }
  }
  // The window the user is looking at, not the first one they opened. A
  // normal window only: driving a devtools or popup window is never what was
  // meant, and lastFocusedWindow is what "in front of me" actually means.
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tabs.length) tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length) tabs = await chrome.tabs.query({ active: true });
  const tab = tabs[0];
  if (!tab) throw new Error("There is no open tab to drive.");
  if (/^(chrome|edge|about|devtools|chrome-extension):/i.test(tab.url || "")) {
    throw new Error(
      `That tab (${tab.url || "a browser page"}) is a browser page, and ` +
      "extensions are not allowed to touch those. Switch to a normal web " +
      "page, or ask me to navigate somewhere first.");
  }
  return tab;
}

async function ensureInjected(tabId) {
  if (injected.get(tabId)) return;
  await chrome.scripting.executeScript({ target: { tabId }, files: ["page.js"] });
  injected.set(tabId, true);
}

chrome.tabs.onUpdated.addListener((tabId, info) => {
  // A navigation replaces the frame, and with it the isolated world.
  if (info.status === "loading") injected.delete(tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => {
  injected.delete(tabId);
  if (pinnedTabId === tabId) pinnedTabId = null;
});

async function act(tabId, action) {
  await ensureInjected(tabId);
  const [out] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [action],
    func: (a) => globalThis.__mnmAct(a),
  });
  const r = out && out.result;
  if (r && r.error) throw new Error(r.error);
  return r ? r.value : null;
}

function waitForLoad(tabId, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    // Resolving on timeout rather than rejecting: a page that never finishes
    // loading (a long poll, a stuck asset) is still a page the agent can read,
    // and failing the whole navigation there would be wrong.
    const timer = setTimeout(done, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// -- commands ------------------------------------------------------------- //

async function run(command, p) {
  if (paused) throw new Error("Browser control is paused — click the extension icon to resume.");

  if (command === "status") {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const t = tabs[0];
    return { ok: true, url: (t && t.url) || "", title: (t && t.title) || "" };
  }
  if (command === "tabs") return await listTabs();
  if (command === "select_tab") {
    const id = Number(p.id);
    let t;
    try {
      t = await chrome.tabs.get(id);
    } catch {
      throw new Error(`There is no tab ${id} any more. List the tabs again.`);
    }
    pinnedTabId = id;
    // Brought to the front on purpose: the user should be able to SEE which
    // tab the agent is working in, and a background tab throttles timers and
    // rendering in ways that make a page behave differently.
    await chrome.tabs.update(id, { active: true });
    try { await chrome.windows.update(t.windowId, { focused: true }); } catch {}
    injected.delete(id);
    return tabInfo(await chrome.tabs.get(id), true);
  }
  if (command === "new_tab") {
    let url = String(p.url || "").trim();
    if (url && !/^(https?|file|data|about):/i.test(url)) url = "https://" + url;
    const t = await chrome.tabs.create(url ? { url, active: true } : { active: true });
    pinnedTabId = t.id;
    if (url) await waitForLoad(t.id);
    injected.delete(t.id);
    return tabInfo(await chrome.tabs.get(t.id), true);
  }
  if (command === "release") {
    // Back to following the user's active tab.
    pinnedTabId = null;
    return true;
  }

  const tab = await activeTab();

  if (command === "navigate") {
    let url = String(p.url || "").trim();
    if (!/^(https?|file|data|about):/i.test(url)) url = "https://" + url;
    await chrome.tabs.update(tab.id, { url });
    await waitForLoad(tab.id);
    injected.delete(tab.id);
    return true;
  }
  if (command === "back") {
    await chrome.tabs.goBack(tab.id);
    await waitForLoad(tab.id);
    injected.delete(tab.id);
    return true;
  }
  if (command === "screenshot") {
    return await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  }
  if (command === "info") {
    const size = await act(tab.id, { kind: "size" });
    const fresh = await chrome.tabs.get(tab.id);
    return { url: fresh.url || "", title: fresh.title || "", ...size };
  }
  return await act(tab.id, { kind: command, ...p });
}

// -- the toolbar button: a way to say "not right now" ---------------------- //

chrome.action.onClicked.addListener(() => {
  paused = !paused;
  chrome.storage.local.set({ paused });
  if (paused) {
    setBadge("paused");
    if (socket) { try { socket.close(); } catch {} socket = null; }
  } else {
    retryMs = RETRY_MIN_MS;
    connect();
  }
});

chrome.runtime.onStartup.addListener(boot);
chrome.runtime.onInstalled.addListener(boot);

async function boot() {
  const stored = await chrome.storage.local.get("paused");
  paused = !!stored.paused;
  setBadge(paused ? "paused" : "off");
  connect();
}
boot();

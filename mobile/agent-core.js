/* Make No Mistakes — mobile agent core.
 *
 * Runs the agent loop entirely on the phone: the model API is the brain, the
 * GitHub API is the filesystem. No backend, so secrets never leave the device.
 *
 * SECURITY MODEL (the whole point of Path A):
 *  - Your model key + GitHub token are encrypted at rest with AES-GCM, using a
 *    key derived from a PIN via PBKDF2 (210k iters, SHA-256). Only the ciphertext
 *    is stored (IndexedDB/localStorage); the PIN is never stored. A stolen,
 *    unlocked phone still can't read the keys without the PIN.
 *  - Keys are held only in memory while unlocked, and the app locks (drops them)
 *    on background/timeout.
 *  - All traffic is HTTPS to exactly two hosts (the model API + api.github.com),
 *    pinned by a strict Content-Security-Policy in index.html. No third-party
 *    scripts, so there's nothing to inject a key-stealer through.
 *  - The GitHub token should be a FINE-GRAINED token scoped to just the repo(s)
 *    you use, Contents read/write only — least privilege.
 *  - Writes/commits are gated by a confirm callback (default on), so the agent
 *    can't silently push.
 *
 * This file is pure and environment-agnostic (Node for tests, browser at
 * runtime) — it takes `fetch` and `crypto` as capabilities, never touches the
 * DOM, and exports everything for unit testing.
 */
(function (global) {
  "use strict";
  const subtle = () => (global.crypto || globalThis.crypto).subtle;
  const getRandom = (n) => (global.crypto || globalThis.crypto).getRandomValues(new Uint8Array(n));

  // --- base64 (cross-env) --------------------------------------------------
  function bytesToB64(bytes) {
    if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
    let s = ""; for (const b of bytes) s += String.fromCharCode(b); return btoa(s);
  }
  function b64ToBytes(b64) {
    if (typeof Buffer !== "undefined") return new Uint8Array(Buffer.from(b64, "base64"));
    const s = atob(b64); const a = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i); return a;
  }
  const utf8 = (s) => new TextEncoder().encode(s);
  const fromUtf8 = (b) => new TextDecoder().decode(b);

  // --- encrypted secret vault ---------------------------------------------
  const PBKDF2_ITERS = 210000;

  async function deriveKey(pin, salt, extractable) {
    const base = await subtle().importKey("raw", utf8(String(pin)), "PBKDF2", false, ["deriveKey"]);
    return subtle().deriveKey(
      { name: "PBKDF2", salt, iterations: PBKDF2_ITERS, hash: "SHA-256" },
      base, { name: "AES-GCM", length: 256 }, !!extractable, ["encrypt", "decrypt"]);
  }

  // General AES-GCM helpers (used to persist the session under the vault key,
  // and to remember the key for "keep me signed in").
  async function aesEncrypt(obj, key) {
    const iv = getRandom(12);
    const ct = await subtle().encrypt({ name: "AES-GCM", iv }, key, utf8(JSON.stringify(obj)));
    return { v: 1, iv: bytesToB64(iv), ct: bytesToB64(new Uint8Array(ct)) };
  }
  async function aesDecrypt(blob, key) {
    const pt = await subtle().decrypt({ name: "AES-GCM", iv: b64ToBytes(blob.iv) }, key, b64ToBytes(blob.ct));
    return JSON.parse(fromUtf8(pt));
  }
  async function exportRawKey(key) {
    return bytesToB64(new Uint8Array(await subtle().exportKey("raw", key)));
  }
  async function importRawKey(b64, extractable) {
    return subtle().importKey("raw", b64ToBytes(b64), { name: "AES-GCM" }, !!extractable, ["encrypt", "decrypt"]);
  }

  async function encryptVault(secretsObj, pin) {
    if (!pin || String(pin).length < 4) throw new Error("PIN must be at least 4 characters");
    const salt = getRandom(16), iv = getRandom(12);
    const key = await deriveKey(pin, salt);
    const ct = await subtle().encrypt({ name: "AES-GCM", iv }, key, utf8(JSON.stringify(secretsObj)));
    return { v: 1, salt: bytesToB64(salt), iv: bytesToB64(iv), ct: bytesToB64(new Uint8Array(ct)) };
  }

  async function decryptVault(blob, pin) {
    if (!blob || blob.v !== 1) throw new Error("unrecognised vault");
    const key = await deriveKey(pin, b64ToBytes(blob.salt));
    let pt;
    try {
      pt = await subtle().decrypt({ name: "AES-GCM", iv: b64ToBytes(blob.iv) }, key, b64ToBytes(blob.ct));
    } catch (e) {
      throw new Error("Wrong PIN"); // AES-GCM auth failure == wrong key
    }
    return JSON.parse(fromUtf8(pt));
  }

  // --- GitHub client (the "filesystem") -----------------------------------
  function makeGitHub(opts) {
    const { token, owner, repo, branch = "main" } = opts;
    const fetchFn = opts.fetch || global.fetch;
    const API = "https://api.github.com";
    async function gh(method, path, body) {
      const headers = {
        Authorization: "Bearer " + token,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      };
      if (body) headers["Content-Type"] = "application/json";
      const r = await fetchFn(API + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
      if (!r.ok) {
        const t = await r.text().catch(() => "");
        throw new Error("GitHub " + r.status + ": " + t.slice(0, 200));
      }
      return r.status === 204 ? null : r.json();
    }
    return {
      raw: gh,
      async tree() {
        const t = await gh("GET", `/repos/${owner}/${repo}/git/trees/${branch}?recursive=1`);
        return (t.tree || []).filter((e) => e.type === "blob")
          .map((e) => ({ path: e.path, size: e.size || 0 }));
      },
      async getFile(path) {
        const d = await gh("GET", `/repos/${owner}/${repo}/contents/${path}?ref=${branch}`);
        return { text: fromUtf8(b64ToBytes((d.content || "").replace(/\n/g, ""))), sha: d.sha };
      },
      // Binary-safe read: returns the raw base64 untouched. getFile() decodes as
      // UTF-8, which mangles images — anything non-text must come through here.
      // GitHub omits `content` for blobs over ~1MB, so report that plainly.
      async getFileRaw(path) {
        const d = await gh("GET", `/repos/${owner}/${repo}/contents/${path}?ref=${branch}`);
        const b64 = (d.content || "").replace(/\n/g, "");
        if (!b64 && (d.size || 0) > 0) {
          throw new Error(`${path} is ${Math.round((d.size || 0) / 1024)}KB — too large for the contents API (1MB limit).`);
        }
        return { b64, sha: d.sha, size: d.size || 0 };
      },
      async putFile(path, text, message, sha) {
        return gh("PUT", `/repos/${owner}/${repo}/contents/${path}`,
          { message, content: bytesToB64(utf8(text)), branch, sha: sha || undefined });
      },
      async deleteFile(path, message, sha) {
        return gh("DELETE", `/repos/${owner}/${repo}/contents/${path}`, { message, sha, branch });
      },
      // Does the current branch exist? Returns its commit SHA or null.
      async branchSha() {
        try { const r = await gh("GET", `/repos/${owner}/${repo}/git/ref/heads/${branch}`); return r.object.sha; }
        catch { return null; }
      },
      // Create the branch as an ORPHAN (no code history) with a single marker
      // file, so session data lives here without touching main/PRs.
      async createOrphanBranch() {
        const tree = await gh("POST", `/repos/${owner}/${repo}/git/trees`,
          { tree: [{ path: ".mnm", mode: "100644", type: "blob", content: "Make No Mistakes — session state. Do not merge.\n" }] });
        const commit = await gh("POST", `/repos/${owner}/${repo}/git/commits`,
          { message: "Initialize Make No Mistakes state", tree: tree.sha, parents: [] });
        await gh("POST", `/repos/${owner}/${repo}/git/refs`, { ref: `refs/heads/${branch}`, sha: commit.sha });
        return commit.sha;
      },
      async listRepos() {
        const rows = await gh("GET", "/user/repos?per_page=50&sort=pushed&affiliation=owner");
        return (rows || []).map((r) => ({ full_name: r.full_name, default_branch: r.default_branch }));
      },
      async createRepo(name, priv) {
        const r = await gh("POST", "/user/repos", { name, private: !!priv, auto_init: true });
        return { full_name: r.full_name, owner: (r.owner || {}).login, name: r.name,
                 default_branch: r.default_branch || "main" };
      },
      async me() { return gh("GET", "/user"); },
    };
  }

  // --- cross-device session sync ------------------------------------------
  // Every synced chat lives in ONE dedicated private repo (SYNC_REPO_NAME),
  // created automatically the first time sync is switched on. Chats from every
  // project share it, which is what keeps the UI simple: one list, nothing to
  // pick, and a chat syncs even when its project isn't on GitHub.
  //
  // Everything is AES-GCM encrypted under a key derived (PBKDF2) from a SYNC
  // PASSPHRASE that is separate from the on-device PIN — GitHub only ever sees
  // ciphertext, and the passphrase is never stored server-side. sync.json holds
  // the shared salt + a check-blob so any device can derive the same key and
  // verify the passphrase; index.json (encrypted) lists the chats; each chat
  // lives in its own encrypted chats/<id>.json.
  const SYNC_REPO_NAME = "makenomistakes-sync";
  const SYNC_REPO_BRANCH = "main";
  const STATE_BRANCH = "makenomistakes/state";  // legacy per-repo store
  const SYNC_CHECK = "mnm-sync-ok";

  // SAME CHAT, TWO DEVICES: a per-chat lock file (chats/<id>.lock.json,
  // encrypted like everything else) records which device is actively
  // mid-turn on a chat. It's a courtesy, not a guarantee — self-heals via
  // TTL expiry if a device disappears mid-turn, and GitHub's own sha-gated
  // PUT (compare-and-swap) is the only real atomicity primitive here.
  // Mirrors glmcode/syncstore.py's SyncStore lock methods field-for-field
  // so phone and desktop can read/write each other's locks interchangeably.
  const DEVICE_LOCK_TTL_MS = 90000;
  const DEVICE_LOCK_HEARTBEAT_S = Math.floor(DEVICE_LOCK_TTL_MS / 3000);

  function lockedElsewhereError(deviceLabel, sinceMs) {
    const e = new Error(`This chat is active on ${deviceLabel} right now.`);
    e.lockedElsewhere = true;
    e.deviceLabel = deviceLabel;
    e.sinceMs = sinceMs;
    return e;
  }

  // Find (or create) the dedicated private sync repo for the signed-in user.
  // Returns { owner, repo }.
  async function ensureSyncRepo(gh) {
    const me = await gh.me();
    const owner = me && me.login;
    if (!owner) throw new Error("GitHub didn't return your account name.");
    try {
      await gh.raw("GET", `/repos/${owner}/${SYNC_REPO_NAME}`);
      return { owner, repo: SYNC_REPO_NAME };
    } catch (e) { /* 404 — create it below */ }
    try {
      await gh.raw("POST", "/user/repos", {
        name: SYNC_REPO_NAME, private: true, auto_init: true,
        description: "Encrypted Make No Mistakes chat sync (managed by the app).",
      });
    } catch (e) {
      throw new Error("Couldn't create the private sync repo '" + SYNC_REPO_NAME +
        "'. Your token needs Administration: Read and write (to create repos), " +
        "plus Contents: Read and write. (" + (e.message || e) + ")");
    }
    return { owner, repo: SYNC_REPO_NAME };
  }

  // Connect a sync gh-client (branch=STATE_BRANCH) to a passphrase: verifies an
  // existing store or bootstraps a new one. Returns { key, store, created }.
  async function openSync(gh, passphrase) {
    if (!passphrase || String(passphrase).length < 6)
      throw new Error("Sync passphrase must be at least 6 characters");
    let meta = null;
    try { meta = JSON.parse((await gh.getFile("sync.json")).text); } catch (e) { meta = null; }
    if (meta && meta.v === 1) {
      const key = await deriveKey(passphrase, b64ToBytes(meta.salt));
      try {
        if ((await aesDecrypt(meta.check, key)) !== SYNC_CHECK) throw new Error("bad");
      } catch (e) { throw new Error("Wrong sync passphrase"); }
      return { key, store: makeSyncStore(gh, key), created: false };
    }
    // First device: make sure the branch exists, then write sync.json. The
    // dedicated repo is auto_init'd so main already exists; the orphan path
    // only matters for the legacy per-repo store.
    if (!(await gh.branchSha())) await gh.createOrphanBranch();
    const salt = getRandom(16);
    const key = await deriveKey(passphrase, salt);
    const check = await aesEncrypt(SYNC_CHECK, key);
    await gh.putFile("sync.json", JSON.stringify({ v: 1, salt: bytesToB64(salt), check }),
      "Set up Make No Mistakes sync");
    return { key, store: makeSyncStore(gh, key), created: true };
  }

  // The encrypted session store over a sync gh-client + derived key.
  function makeSyncStore(gh, key) {
    async function readIndex() {
      try {
        const f = await gh.getFile("index.json");
        return { data: await aesDecrypt(JSON.parse(f.text), key), sha: f.sha };
      } catch (e) { return { data: { v: 1, chats: [] }, sha: null }; }
    }
    async function writeIndex(chats, sha) {
      const blob = await aesEncrypt({ v: 1, chats }, key);
      return gh.putFile("index.json", JSON.stringify(blob), "Update session index", sha);
    }
    async function fileSha(path) {
      try { return (await gh.getFile(path)).sha; } catch (e) { return null; }
    }
    function lockPath(id) { return `chats/${id}.lock.json`; }
    // { data: live lock or null, sha: the raw file's sha or null }.
    async function readLock(id) {
      let f;
      try { f = await gh.getFile(lockPath(id)); } catch (e) { return { data: null, sha: null }; }
      let data;
      try { data = await aesDecrypt(JSON.parse(f.text), key); } catch (e) { return { data: null, sha: f.sha }; }
      if (!data || (data.expires || 0) < Date.now()) return { data: null, sha: f.sha };
      return { data, sha: f.sha };
    }
    return {
      // Newest-first list of chat summaries (id, title, updated, preview).
      async list() {
        const { data } = await readIndex();
        return (data.chats || []).slice().sort((a, b) => (b.updated || 0) - (a.updated || 0));
      },
      // Full chat object (messages etc.).
      async load(id) {
        const f = await gh.getFile(`chats/${id}.json`);
        return aesDecrypt(JSON.parse(f.text), key);
      },
      // Persist a chat and refresh its index entry. Stamps chat.updated.
      async save(chat) {
        if (!chat || !chat.id) throw new Error("chat needs an id");
        chat.updated = Date.now();
        const path = `chats/${chat.id}.json`;
        const blob = await aesEncrypt(chat, key);
        await gh.putFile(path, JSON.stringify(blob), `Save session ${chat.id}`, await fileSha(path));
        const { data, sha } = await readIndex();
        const chats = (data.chats || []).filter((c) => c.id !== chat.id);
        chats.push({ id: chat.id, title: chat.title || "Untitled",
          updated: chat.updated, preview: chat.preview || "",
          project: chat.project || "", device: chat.device || "" });
        await writeIndex(chats, sha);
        return chat.updated;
      },
      // Delete a chat file and its index entry.
      async remove(id) {
        const path = `chats/${id}.json`;
        const sha = await fileSha(path);
        if (sha) await gh.deleteFile(path, `Delete session ${id}`, sha);
        const { data, sha: isha } = await readIndex();
        await writeIndex((data.chats || []).filter((c) => c.id !== id), isha);
      },
      // --- cross-device lock (see DEVICE_LOCK_TTL_MS note above) ------------
      async checkLock(id) {
        const { data } = await readLock(id);
        return data;
      },
      // Throws lockedElsewhereError() if another live device holds it, unless
      // force is set. Even with force, a genuine write race still reports the
      // true winner rather than silently succeeding.
      async acquireLock(id, deviceId, deviceLabel, force) {
        const { data: current, sha } = await readLock(id);
        if (current && current.device_id !== deviceId && !force) {
          throw lockedElsewhereError(current.label || current.device || "another device",
            current.acquired || 0);
        }
        const now = Date.now();
        const payload = { v: 1, device_id: deviceId, device: deviceLabel, label: deviceLabel,
          acquired: now, expires: now + DEVICE_LOCK_TTL_MS };
        const blob = await aesEncrypt(payload, key);
        try {
          await gh.putFile(lockPath(id), JSON.stringify(blob), `Lock ${id}`, sha);
        } catch (e) {
          const { data: winner } = await readLock(id);
          if (winner && winner.device_id !== deviceId) {
            throw lockedElsewhereError(winner.label || winner.device || "another device",
              winner.acquired || 0);
          }
          throw e;
        }
        return payload;
      },
      // Extends an already-held lock. Returns false once another device has
      // genuinely taken over; fails OPEN (returns true) on a transient write
      // error, since "couldn't confirm" must never be misread as "preempted".
      async renewLock(id, deviceId, deviceLabel) {
        const { data: current, sha } = await readLock(id);
        if (current && current.device_id !== deviceId) return false;
        const now = Date.now();
        const payload = { v: 1, device_id: deviceId, device: deviceLabel, label: deviceLabel,
          acquired: now, expires: now + DEVICE_LOCK_TTL_MS };
        const blob = await aesEncrypt(payload, key);
        try {
          await gh.putFile(lockPath(id), JSON.stringify(blob), `Renew lock ${id}`, sha);
          return true;
        } catch (e) {
          const { data: winner } = await readLock(id);
          return !(winner && winner.device_id !== deviceId);
        }
      },
      async releaseLock(id, deviceId) {
        const { data: current, sha } = await readLock(id);
        if (!sha) return;
        if (current && current.device_id !== deviceId) return;
        try { await gh.deleteFile(lockPath(id), `Unlock ${id}`, sha); } catch (e) { /* best effort */ }
      },
    };
  }

  // --- device handoff ------------------------------------------------------
  // A synced chat carries its whole history between devices, and the two do NOT
  // have the same tools: the desktop has a shell, tests and a browser; the phone
  // has the GitHub API and nothing that executes. The model learns from its own
  // history, so after a handoff it will imitate a turn it "successfully" ran on
  // the other device and call a tool that doesn't exist here — reaching for
  // PowerShell on the phone, or for desktop-only tools after coming back.
  //
  // Rebuilding the system prompt per device isn't enough on its own: the prompt
  // says one thing while a hundred lines of history demonstrate the opposite,
  // and demonstrations win. So on every cross-device open we splice a marker in
  // at the boundary, naming the switch and voiding the earlier calls as examples.
  //
  // Mirrors glmcode/syncstore.py's handoff_note/apply_handoff word for word.
  const HANDOFF_MARKER = "[device-handoff]";
  const DEVICE_FACTS = {
    desktop: "a real machine: you can run commands, tests, servers and a browser here",
    phone: "the phone: there is no shell here, so you cannot run commands, tests or " +
           "servers -- note anything that needs running and it will happen on the desktop",
  };
  function handoffNote(fromDevice, toDevice) {
    const frm = String(fromDevice || "another device").toLowerCase();
    const to = String(toDevice || "").toLowerCase();
    return `${HANDOFF_MARKER} This conversation just moved from the ${frm} to the ${to}. ` +
      `You are now on ${DEVICE_FACTS[to] || to}. ` +
      `Everything above ran on the ${frm}, which has a different set of tools, so earlier ` +
      `turns may show tool calls that do not exist here. Do not copy them: only the tools ` +
      `offered to you now are real. If you need something this device cannot do, say so ` +
      `plainly instead of calling a tool that isn't there.`;
  }
  // Notes about the other device's state, re-derived on every open. Both are
  // stripped before a new one is added, so they can't pile up as a chat moves
  // back and forth describing switches that already happened.
  const DESKTOP_STATE_MARKER = "[desktop-state]";
  const isStaleNote = (m) => m.role === "system" &&
    (String(m.content || "").startsWith(HANDOFF_MARKER) ||
     String(m.content || "").startsWith(DESKTOP_STATE_MARKER));
  // Strip any stale marker, then add one if the device changed. Index 0 (the
  // live system prompt) is left alone — the caller owns it.
  function applyHandoff(messages, fromDevice, toDevice) {
    const out = messages.filter((m, i) => i === 0 || !isStaleNote(m));
    if (fromDevice && toDevice && String(fromDevice).toLowerCase() !== String(toDevice).toLowerCase()) {
      out.push({ role: "system", content: handoffNote(fromDevice, toDevice) });
    }
    return out;
  }

  // What the desktop's checkout looks like, published with the chat by
  // syncstore.session_to_chat. This phone reads the repo over the GitHub API,
  // so work that only exists on the desktop's disk is invisible here — editing
  // on top of it means committing over it. Returns "" when GitHub really is the
  // latest word, so callers can stay silent in the common case.
  function repoStateWarning(state, myBranch) {
    if (!state || typeof state !== "object") return "";
    const bits = [];
    if (state.dirty) bits.push("uncommitted changes");
    const ahead = Number(state.ahead || 0);
    if (ahead > 0) bits.push(`${ahead} commit${ahead === 1 ? "" : "s"} not pushed yet`);
    const theirs = String(state.branch || "");
    const mine = String(myBranch || "");
    const branchDiffers = theirs && mine && theirs !== mine;
    if (!bits.length && !branchDiffers) return "";
    let msg = "";
    if (bits.length) {
      msg += `The desktop has ${bits.join(" and ")} for this project, which GitHub ` +
             `hasn't seen. Files you read here may be older than what's on that machine, ` +
             `so editing them risks committing over that work. Prefer reading and planning; ` +
             `if you must edit, say plainly which files might clash.`;
    }
    if (branchDiffers) {
      msg += `${msg ? " " : ""}The desktop is on branch "${theirs}" while you are reading ` +
             `"${mine}" — you may be looking at different code entirely.`;
    }
    return msg;
  }

  // --- images --------------------------------------------------------------
  const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|avif|svg|ico)$/i;
  function imageMime(path) {
    const m = IMAGE_RE.exec(path || "");
    const ext = (m ? m[1] : "png").toLowerCase();
    if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
    if (ext === "svg") return "image/svg+xml";
    if (ext === "ico") return "image/x-icon";
    return "image/" + ext;
  }

  // --- model client (the "brain") -----------------------------------------
  // The free GLM models are rate-limited, so a "busy" response (HTTP 429, or
  // z.ai's 1305 / 访问量过大) is retried with exponential backoff before failing
  // with a plain-language message.
  function makeModel(opts) {
    const fetchFn = opts.fetch || global.fetch;
    const baseUrl = opts.baseUrl || "https://api.z.ai/api/paas/v4";
    const onRetry = opts.onRetry || (() => {});
    const maxRetries = opts.maxRetries != null ? opts.maxRetries : 3;
    const baseMs = opts.retryBaseMs || 2000;
    const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
    // Fold one streamed delta into the message being assembled. Tool calls
    // arrive in fragments keyed by index (name first, then the arguments JSON
    // a few characters at a time), so they're accumulated rather than replaced.
    function applyDelta(msg, d, onDelta) {
      if (!d) return;
      if (d.content) { msg.content += d.content; if (onDelta) onDelta(d.content); }
      if (!d.tool_calls) return;
      msg.tool_calls = msg.tool_calls || [];
      for (const tc of d.tool_calls) {
        const i = tc.index != null ? tc.index : msg.tool_calls.length;
        if (!msg.tool_calls[i]) msg.tool_calls[i] = { id: "", type: "function", function: { name: "", arguments: "" } };
        const slot = msg.tool_calls[i];
        if (tc.id) slot.id = tc.id;
        if (tc.function) {
          if (tc.function.name) slot.function.name = tc.function.name;
          if (tc.function.arguments) slot.function.arguments += tc.function.arguments;
        }
      }
    }
    // Read an SSE body to completion, feeding text deltas out as they land.
    async function readStream(body, onDelta) {
      const reader = body.getReader();
      const decoder = new TextDecoder();
      const msg = { role: "assistant", content: "" };
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line.startsWith("data:")) continue;      // skip SSE comments/blank lines
          const payload = line.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          let j; try { j = JSON.parse(payload); } catch (e) { continue; }
          applyDelta(msg, j.choices && j.choices[0] && j.choices[0].delta, onDelta);
        }
      }
      if (msg.tool_calls) msg.tool_calls = msg.tool_calls.filter(Boolean);
      return msg;
    }
    return {
      // onDelta (optional) turns on streaming: it's called with each chunk of
      // text as it arrives, and the assembled message is still returned at the
      // end, so callers that don't pass it behave exactly as before. Falls back
      // to a normal read if the server or runtime can't stream.
      async chat(messages, tools, onDelta) {
        for (let attempt = 0; ; attempt++) {
          const wantStream = !!onDelta;
          const r = await fetchFn(baseUrl + "/chat/completions", {
            method: "POST",
            headers: { Authorization: "Bearer " + opts.apiKey, "Content-Type": "application/json" },
            body: JSON.stringify({
              model: opts.model, messages, tools: tools && tools.length ? tools : undefined,
              temperature: 0.6, max_tokens: 4096,
              stream: wantStream || undefined,
            }),
          });
          if (r.ok) {
            if (wantStream && r.body && r.body.getReader) return readStream(r.body, onDelta);
            const j = await r.json();
            return (j.choices && j.choices[0] && j.choices[0].message) || { role: "assistant", content: "" };
          }
          const body = await r.text().catch(() => "");
          const busy = r.status === 429 || /"1305"|访问量过大|rate.?limit/i.test(body);
          if (busy && attempt < maxRetries) {
            const wait = baseMs * Math.pow(2, attempt);   // 2s, 4s, 8s…
            onRetry(attempt + 1, wait);
            await sleep(wait);
            continue;
          }
          if (busy) throw new Error("The free model is busy right now (rate-limited). Wait a few seconds and try again.");
          throw new Error("Model " + r.status + ": " + body.slice(0, 200));
        }
      },
    };
  }

  // --- tools over the GitHub repo -----------------------------------------
  // Read/search happen client-side over fetched file contents; writes are gated
  // by confirmWrite so the agent can never silently commit.
  function makeTools(gh, opts) {
    opts = opts || {};
    const confirmWrite = opts.confirmWrite || (async () => true);
    const onCommit = opts.onCommit || (() => {});
    const cache = new Map();     // path -> {text, sha}
    let treeCache = null;
    async function tree() { if (!treeCache) treeCache = await gh.tree(); return treeCache; }
    async function load(path) {
      if (cache.has(path)) return cache.get(path);
      const f = await gh.getFile(path);
      cache.set(path, f); return f;
    }
    function tokenize(s) {
      const out = [];
      for (const m of String(s).matchAll(/[A-Za-z0-9_]+/g)) {
        out.push(m[0].toLowerCase());
        for (const p of m[0].matchAll(/[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[a-z0-9]+|[A-Z]+/g)) out.push(p[0].toLowerCase());
      }
      return out;
    }
    const BIN = /\.(png|jpg|jpeg|gif|webp|ico|pdf|zip|gz|woff2?|ttf|mp4|mp3|wasm|lock)$/i;
    const IMG = /\.(png|jpe?g|gif|webp|bmp|avif|svg|ico)$/i;

    const api = {
      async list_dir(a) {
        const dir = (a.path || "").replace(/^\.?\/*/, "").replace(/\/$/, "");
        const t = await tree();
        const seen = new Set();
        for (const e of t) {
          if (dir && !e.path.startsWith(dir + "/") && e.path !== dir) continue;
          const rest = dir ? e.path.slice(dir.length + 1) : e.path;
          const top = rest.split("/")[0];
          if (top) seen.add(rest.includes("/") ? top + "/" : top);
        }
        return [...seen].sort().join("\n") || "(empty)";
      },
      async glob(a) {
        const pat = String(a.pattern || "*").replace(/[.+^${}()|[\]\\]/g, "\\$&")
          .replace(/\*\*/g, "").replace(/\*/g, "[^/]*").replace(//g, ".*").replace(/\?/g, ".");
        const rx = new RegExp("(^|/)" + pat + "$");
        const t = await tree();
        return t.filter((e) => rx.test(e.path)).map((e) => e.path).slice(0, 200).join("\n") || "(no matches)";
      },
      async read_file(a) {
        // Decoding an image as UTF-8 yields garbage that reads like a real file
        // and quietly derails the turn. Say so, and point at the tool that works.
        if (IMG.test(a.path || "")) {
          return `${a.path} is an image. read_file would return unusable bytes — ` +
                 `use view_image with this path to see what it actually shows.`;
        }
        const f = await load(a.path);
        const lines = f.text.split("\n");
        return lines.map((l, i) => `${String(i + 1).padStart(4)} | ${l}`).join("\n").slice(0, 12000);
      },
      async grep(a) {
        const rx = new RegExp(a.pattern, a.case_insensitive ? "i" : "");
        const t = await tree();
        const hits = [];
        for (const e of t) {
          if (BIN.test(e.path) || e.size > 300000) continue;
          let f; try { f = await load(e.path); } catch { continue; }
          f.text.split("\n").forEach((l, i) => {
            if (hits.length < 100 && rx.test(l)) hits.push(`${e.path}:${i + 1}: ${l.trim().slice(0, 160)}`);
          });
          if (hits.length >= 100) break;
        }
        return hits.join("\n") || "(no matches)";
      },
      async search_code(a) {
        const q = new Set(tokenize(a.query || ""));
        if (!q.size) return "(empty query)";
        const t = await tree();
        const scored = [];
        for (const e of t) {
          if (BIN.test(e.path) || e.size > 300000) continue;
          let f; try { f = await load(e.path); } catch { continue; }
          const toks = tokenize(f.text);
          const set = new Set(toks);
          let overlap = 0; for (const w of q) if (set.has(w)) overlap++;
          if (overlap) scored.push({ path: e.path, score: overlap / q.size, snippet: f.text.slice(0, 400) });
        }
        scored.sort((x, y) => y.score - x.score);
        return scored.slice(0, 6).map((s) => `${s.path} (${s.score.toFixed(2)})\n${s.snippet}`).join("\n\n")
          || "(no matches)";
      },
      async write_file(a) {
        if (!(await confirmWrite("write", a.path, a.content))) return "User declined to write " + a.path;
        let sha; try { sha = (await gh.getFile(a.path)).sha; } catch { sha = undefined; }
        await gh.putFile(a.path, a.content, a.message || `Update ${a.path}`, sha);
        cache.set(a.path, { text: a.content, sha: undefined }); treeCache = null;
        onCommit(a.path);
        return "Wrote and committed " + a.path;
      },
      async edit_file(a) {
        const f = await load(a.path);
        if (!f.text.includes(a.old_string)) return `old_string not found in ${a.path}; re-read it.`;
        const count = f.text.split(a.old_string).length - 1;
        if (count > 1 && !a.replace_all) return `old_string appears ${count}× in ${a.path}; make it unique or set replace_all.`;
        const next = a.replace_all ? f.text.split(a.old_string).join(a.new_string)
          : f.text.replace(a.old_string, a.new_string);
        if (!(await confirmWrite("edit", a.path, next))) return "User declined to edit " + a.path;
        await gh.putFile(a.path, next, a.message || `Edit ${a.path}`, f.sha);
        cache.set(a.path, { text: next, sha: undefined }); treeCache = null;
        onCommit(a.path);
        return "Edited and committed " + a.path;
      },
    };
    // Delegation: only exposed when the host wires a spawner (main agent only,
    // so sub-agents can't spawn further sub-agents).
    if (opts.spawn) {
      api.spawn_agent = async (a) => String(await opts.spawn(a.task || "", a.context || ""));
    }
    // Vision: describe an attached image via the free vision model, so a text
    // (coding) model can still "see" it. Wired by the host when images exist.
    if (opts.viewImage) {
      api.view_image = async (a) => String(await opts.viewImage(a.name || "", a.question || ""));
    }
    return api;
  }

  const TOOL_SCHEMAS = [
    tool("list_dir", "List files/folders under a directory in the repo.", { path: str("Directory (default root)") }),
    tool("glob", "Find files by glob, e.g. '**/*.js'.", { pattern: str("Glob pattern") }, ["pattern"]),
    tool("read_file", "Read a file (with line numbers).", { path: str("File path") }, ["path"]),
    tool("grep", "Search file contents by regex.", { pattern: str("Regex"), case_insensitive: bool("Case-insensitive") }, ["pattern"]),
    tool("search_code", "Find the most relevant code for a description.", { query: str("What you're looking for") }, ["query"]),
    tool("write_file", "Create or overwrite a file and commit it.", { path: str("Path"), content: str("Full new contents"), message: str("Commit message") }, ["path", "content"]),
    tool("edit_file", "Replace an exact string in a file and commit.", { path: str("Path"), old_string: str("Exact text to replace"), new_string: str("Replacement"), replace_all: bool("Replace all"), message: str("Commit message") }, ["path", "old_string", "new_string"]),
  ];
  function tool(name, description, props, required) {
    return { type: "function", function: { name, description, parameters: { type: "object", properties: props, required: required || [] } } };
  }
  function str(d) { return { type: "string", description: d }; }
  function bool(d) { return { type: "boolean", description: d }; }

  // Advertised only on the main agent's turn (never to sub-agents).
  const SPAWN_SCHEMA = tool("spawn_agent",
    "Delegate one self-contained sub-task to a fresh sub-agent that works on its own and reports back " +
    "(e.g. 'add tests for X', 'refactor Y'). It can read and edit files but cannot spawn further sub-agents. " +
    "Use it to keep big tasks organised; do the work yourself for small ones.",
    { task: str("The self-contained task for the sub-agent"), context: str("Anything it should know: constraints, files, goals") },
    ["task"]);

  // Vision. Routes an image through the vision model for a writeup, so a text
  // (coding) model can still act on it. Works for BOTH images the user attached
  // and image files that live in the repo — read_file can't do the latter,
  // since it decodes as UTF-8 and would return mangled bytes.
  const VIEW_IMAGE_SCHEMA = tool("view_image",
    "Look at an image and get a description of what it actually shows. Works for an image the user " +
    "attached AND for any image file in the repo (png/jpg/gif/webp/svg…) — pass its repo path, e.g. " +
    "'docs/mockup.png'. Use this whenever an image's visual content matters: a screenshot of a bug, a " +
    "design mockup, a diagram, a chart. Do NOT use read_file on an image; it returns unusable bytes.",
    { name: str("The attached image's name, or the image file's path in the repo"),
      question: str("What to look for (optional)") },
    ["name"]);

  // A dead-end "unknown tool: x" leaves the model to guess again. Nearly every
  // real case is a tool it used earlier on the DESKTOP, so name that, and list
  // what it actually has — enough to recover inside the same turn.
  const DESKTOP_ONLY = /^(run_|bash|shell|powershell|pwsh|cmd|exec|terminal|test|pytest|npm|git_|browser|screenshot|check_page|preview_page|open_)/i;
  function unknownTool(name, tools) {
    const have = Object.keys(tools || {}).join(", ") || "(none)";
    const hint = DESKTOP_ONLY.test(name)
      ? `"${name}" doesn't exist on the phone — there's no shell here, so nothing can be run. ` +
        `If you used it earlier in this conversation, that turn ran on the user's desktop. ` +
        `Do the part you can do over the GitHub API and say what still needs running there. `
      : `"${name}" isn't a tool here. `;
    return `ERROR: ${hint}Tools available right now: ${have}.`;
  }

  // --- the agent loop ------------------------------------------------------
  async function runAgent(cfg) {
    const { model, tools, messages, onEvent = () => {}, maxSteps = 24, shouldStop = () => false,
      toolSchemas = TOOL_SCHEMAS } = cfg;
    for (let step = 0; step < maxSteps; step++) {
      if (shouldStop()) { onEvent({ type: "stopped" }); return messages; }
      onEvent({ type: "thinking" });
      let msg;
      // Stream only when the host actually renders deltas, so sub-agents (whose
      // output is summarised, not shown live) keep the cheaper single read.
      const onDelta = cfg.stream ? (t) => onEvent({ type: "delta", text: t }) : undefined;
      try { msg = await model.chat(messages, toolSchemas, onDelta); }
      catch (e) { onEvent({ type: "error", text: e.message }); return messages; }
      messages.push(msg);
      if (!msg.tool_calls || !msg.tool_calls.length) {
        onEvent({ type: "answer", text: msg.content || "" });
        return messages;
      }
      for (const tc of msg.tool_calls) {
        const name = tc.function.name;
        let args = {}; try { args = JSON.parse(tc.function.arguments || "{}"); } catch (e) {}
        onEvent({ type: "tool", name, args });
        let out;
        try { out = tools[name] ? await tools[name](args) : unknownTool(name, tools); }
        catch (e) { out = "ERROR: " + (e && e.message ? e.message : e); }
        onEvent({ type: "tool_result", name, out: String(out) });
        messages.push({ role: "tool", tool_call_id: tc.id, content: String(out).slice(0, 8000) });
      }
    }
    onEvent({ type: "answer", text: "(stopped after the step limit — ask me to continue)" });
    return messages;
  }

  const SYSTEM_PROMPT =
    "You are Make No Mistakes, a coding agent running on the user's PHONE. Your filesystem is a " +
    "GitHub repository reached over the API: you can read, search, and edit files and each write is " +
    "committed. You CANNOT run commands, tests, or servers here — that happens on the user's desktop " +
    "when it next syncs, or via CI. So: make correct, complete, minimal edits; read before you edit; " +
    "prefer search_code/grep to find things; and in your final reply note anything that still needs to " +
    "be run or verified on a real machine. Be concise.\n\n" +
    "THIS CHAT IS SHARED WITH THE USER'S DESKTOP. The same conversation moves between the two, and " +
    "the desktop has tools you do not have here (a shell, tests, a browser). Earlier turns may " +
    "therefore show tool calls that don't exist on this device — treat those as history, not as " +
    "examples to copy. The tools offered to you on this turn are the only ones that are real. When " +
    "something genuinely needs a machine, say so and leave it for the desktop instead of reaching " +
    "for a tool that isn't there.";

  const SUBAGENT_PROMPT =
    "You are a focused sub-agent of Make No Mistakes, working on the user's PHONE against a GitHub repo " +
    "(your filesystem, via the API). You were handed ONE self-contained task. Do it fully: read what you " +
    "need, make correct, minimal edits (each write is committed), then reply with a SHORT report of what " +
    "you changed and anything still to verify on a real machine. You cannot run code or spawn further " +
    "sub-agents. Be concise.";

  const CoreAPI = {
    encryptVault, decryptVault, deriveKey, PBKDF2_ITERS,
    aesEncrypt, aesDecrypt, exportRawKey, importRawKey,
    makeGitHub, makeModel, makeTools, runAgent, TOOL_SCHEMAS, SPAWN_SCHEMA, VIEW_IMAGE_SCHEMA,
    SYSTEM_PROMPT, SUBAGENT_PROMPT,
    openSync, makeSyncStore, ensureSyncRepo,
    SYNC_REPO_NAME, SYNC_REPO_BRANCH, STATE_BRANCH,
    DEVICE_LOCK_TTL_MS, DEVICE_LOCK_HEARTBEAT_S,
    IMAGE_RE, imageMime,
    handoffNote, applyHandoff, HANDOFF_MARKER, repoStateWarning,
    _b64: { bytesToB64, b64ToBytes },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = CoreAPI;
  else global.AgentCore = CoreAPI;
})(typeof self !== "undefined" ? self : this);

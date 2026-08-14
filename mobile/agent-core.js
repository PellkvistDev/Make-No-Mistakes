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

  // --- pairing (set up by scanning the desktop's QR) -----------------------
  // The desktop seals your keys under a short code shown as TEXT, never encoded
  // into the image. So the secrets reach this phone by camera, without touching
  // a network at all, and a screenshot of the QR on its own is useless.
  //
  // Wire layout, matching glmcode/pairing.py exactly:
  //   byte 0     version: 1 = JSON, 2 = deflated JSON
  //   bytes 1..16   PBKDF2 salt
  //   bytes 17..28  AES-GCM iv
  //   rest          ciphertext
  //
  // Version 2 exists because this payload is read by a camera and QR size is
  // what decides whether that works. Deflate roughly halves the JSON — the
  // base URLs and model names repeat heavily, while the keys are random and do
  // not compress at all — taking a realistic pairing code from 113 modules down
  // to 73. Version 1 is still accepted so a token from an older desktop opens.
  const PAIR_WIRE_V = 2, PAIR_WIRE_V_PLAIN = 1;
  const PAIR_SALT_LEN = 16, PAIR_IV_LEN = 12;

  function normalizePairCode(code) {
    return String(code || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  }

  async function inflate(bytes) {
    // Checked separately from the decompression itself: "your browser cannot
    // do this" names something the person can act on, and it would otherwise
    // be reported as a damaged code they would keep rescanning.
    if (typeof DecompressionStream !== "function") {
      throw new Error("This browser is too old to read the set-up code — "
        + "type your keys in by hand, or update it.");
    }
    try {
      const stream = new Blob([bytes]).stream()
        .pipeThrough(new DecompressionStream("deflate"));
      return new Uint8Array(await new Response(stream).arrayBuffer());
    } catch (e) {
      throw new Error("That pairing link is damaged.");
    }
  }

  // The smallest envelope that could possibly be one of ours: version byte,
  // salt, iv and a bare GCM tag, base64'd. Nothing shorter is worth a code
  // prompt, and the length is what tells a bare token apart from the other
  // short strings a camera finds on a desk.
  const PAIR_TOKEN_MIN = Math.ceil((1 + PAIR_SALT_LEN + PAIR_IV_LEN + 16) * 4 / 3);

  /* Pull a pairing token out of whatever was scanned or opened.
   *
   * Two shapes, and the bare one is now what the QR holds. A code containing a
   * URL is one the phone's Camera app will open in Safari, which on iOS is a
   * different storage box from an app installed to the home screen -- so the
   * keys land somewhere that is not the app. The URL form is still read
   * because links minted before that change still exist. */
  function pairTokenFrom(text) {
    const s = String(text || "").trim();
    const m = /[#&]pair=([A-Za-z0-9_-]+)/.exec(s);
    if (m) return m[1];
    return /^[A-Za-z0-9_-]+$/.test(s) && s.length >= PAIR_TOKEN_MIN ? s : "";
  }

  async function openPairToken(token, code, now) {
    let raw;
    try {
      const b64 = String(token || "").replace(/-/g, "+").replace(/_/g, "/");
      raw = b64ToBytes(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
    } catch (e) { throw new Error("That pairing link is damaged."); }
    const version = raw.length ? raw[0] : 0;
    if (raw.length < 1 + PAIR_SALT_LEN + PAIR_IV_LEN + 16
      || (version !== PAIR_WIRE_V && version !== PAIR_WIRE_V_PLAIN)) {
      throw new Error("That pairing link is damaged.");
    }
    const salt = raw.slice(1, 1 + PAIR_SALT_LEN);
    const iv = raw.slice(1 + PAIR_SALT_LEN, 1 + PAIR_SALT_LEN + PAIR_IV_LEN);
    const ct = raw.slice(1 + PAIR_SALT_LEN + PAIR_IV_LEN);
    const key = await deriveKey(normalizePairCode(code), salt);
    let body;
    try {
      body = new Uint8Array(await subtle().decrypt({ name: "AES-GCM", iv }, key, ct));
    } catch (e) {
      // AES-GCM authentication failing IS the wrong-code signal.
      throw new Error("That code doesn't match. Check the six characters on your computer.");
    }
    // Past the AES tag: the code was right and the bytes are intact, so
    // anything wrong from here is not something scanning again would fix.
    // inflate() reports its own failures, which distinguish "this browser
    // can't" from "these bytes are wrong".
    const plain = version === PAIR_WIRE_V ? await inflate(body) : body;
    let data;
    try {
      data = JSON.parse(fromUtf8(plain));
    } catch (e) {
      throw new Error("That pairing link is damaged.");
    }
    if (Number(data.exp || 0) < (now || Date.now())) {
      throw new Error("That pairing code has expired — show a new one on your computer.");
    }
    return data;
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
  /* A sync passphrase nobody has to invent. Mirrors syncstore.make_passphrase
   * -- same alphabet, same shape -- so a code generated on either device looks
   * like the other's and can be copied between them.
   *
   * It was never a password. It is the key the chats are encrypted under, and
   * its only job is to be identical on both devices; pairing already carries
   * it here. Asking for one bought nothing but a chance to pick something weak
   * or mistype it and fork the history into two unreadable halves. */
  const RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  function makeSyncPassphrase() {
    const pick = () => Array.from(getRandom(5))
      .map((b) => RECOVERY_ALPHABET[b % RECOVERY_ALPHABET.length]).join("");
    return [pick(), pick(), pick(), pick()].join("-");
  }

  /* Whether a store already exists, which is what separates "first device,
   * make a key" from "second device, the key is already decided". */
  async function syncStoreExists(gh) {
    try {
      const meta = JSON.parse((await gh.getFile("sync.json")).text);
      return !!(meta && meta.v === 1);
    } catch (e) { return false; }
  }

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
        const data = await aesDecrypt(JSON.parse(f.text), key);
        if (!data.deleted) data.deleted = [];   // indexes written before tombstones
        return { data, sha: f.sha };
      } catch (e) { return { data: { v: 1, chats: [], deleted: [] }, sha: null }; }
    }
    async function writeIndex(chats, sha, deleted) {
      const blob = await aesEncrypt({ v: 1, chats, deleted: deleted || [] }, key);
      return gh.putFile("index.json", JSON.stringify(blob), "Update session index", sha);
    }
    // Deleting has to be a RECORD, not an absence. The device that still has
    // the chat open writes later than the delete by definition, so without a
    // tombstone its next save quietly brings the chat back — and keeps bringing
    // it back after every turn. Ids are uuids and never reused, so a tombstone
    // can simply be permanent (pruned only so the index can't grow forever).
    const TOMBSTONE_TTL_MS = 30 * 24 * 60 * 60 * 1000;
    const tombstones = (idx) => ((idx && idx.deleted) || []);
    const isDeleted = (gone, id) => gone.some((t) => t && t.id === id);
    const pruneTombstones = (gone, now) =>
      gone.filter((t) => Number((t && t.at) || 0) >= (now || Date.now()) - TOMBSTONE_TTL_MS);
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
        const gone = tombstones(data);
        return (data.chats || []).filter((c) => !isDeleted(gone, c.id))
          .sort((a, b) => (b.updated || 0) - (a.updated || 0));
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
        // Another device may have deleted this while we still had it open.
        const pre = await readIndex();
        if (isDeleted(tombstones(pre.data), chat.id)) {
          const err = new Error("That chat was deleted on another device.");
          err.chatDeleted = true;
          throw err;
        }
        const path = `chats/${chat.id}.json`;
        // Merge over whatever is already stored, rather than replacing it.
        //
        // A write only carries the fields the writing device knows about, and
        // the two ends of this store are different programs released
        // separately -- the desktop is routinely older than the phone, or the
        // other way round. Replacing wholesale means the older side silently
        // deletes every field the newer one added, which is how a chat lost the
        // repo it belonged to and how another lost its transcript.
        // Absent means "I have nothing to say about this", not "delete it".
        // Clearing something is still possible by sending it explicitly: the
        // desktop empties `pending` by passing [], which is present and wins.
        let merged = chat, priorSha = null;
        try {
          const f = await gh.getFile(path);
          priorSha = f.sha;
          merged = Object.assign({}, await aesDecrypt(JSON.parse(f.text), key), chat);
        } catch (e) { /* absent, or unreadable under a rotated key: chat wins */ }
        const blob = await aesEncrypt(merged, key);
        await gh.putFile(path, JSON.stringify(blob), `Save session ${chat.id}`, priorSha);
        const { data, sha } = await readIndex();
        const chats = (data.chats || []).filter((c) => c.id !== chat.id);
        chats.push({ id: chat.id, title: chat.title || "Untitled",
          updated: chat.updated, preview: chat.preview || "",
          project: chat.project || "",
          // Just the full_name, not the whole repo object: the index is read in
          // one piece on every list, so it stays small. Empty means the chat has
          // no GitHub repo, which is what lets the list say so up front instead
          // of only finding out once you've tapped it.
          repo: (chat.repo && chat.repo.full_name) || "",
          device: chat.device || "" });
        await writeIndex(chats, sha, tombstones(data));
        return chat.updated;
      },
      // Delete a chat file and its index entry.
      async remove(id) {
        const path = `chats/${id}.json`;
        const sha = await fileSha(path);
        if (sha) await gh.deleteFile(path, `Delete session ${id}`, sha);
        const { data, sha: isha } = await readIndex();
        const gone = tombstones(data).filter((t) => t.id !== id);
        gone.push({ id, at: Date.now() });
        await writeIndex((data.chats || []).filter((c) => c.id !== id), isha,
                         pruneTombstones(gone));
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
  // Work the phone couldn't run, handed to the machine that can. Mirrors
  // syncstore.pending_note word for word.
  const PENDING_MARKER = "[from-your-phone]";
  function pendingNote(items) {
    const lines = [];
    (items || []).forEach((it) => {
      const task = String((it && it.task) || "").trim();
      if (!task) return;
      const why = String((it && it.why) || "").trim();
      lines.push(`${lines.length + 1}. ${task}` + (why ? ` -- ${why}` : ""));
    });
    if (!lines.length) return "";
    return `${PENDING_MARKER} Earlier turns of this chat ran on the phone, which has no shell. ` +
      `These were left for this machine, which does:\n` + lines.join("\n") +
      `\nPick them up if they still make sense -- check the code first, since it may have ` +
      `moved on since. If one no longer applies, say so instead of doing it anyway.`;
  }
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
     String(m.content || "").startsWith(DESKTOP_STATE_MARKER) ||
     String(m.content || "").startsWith(PENDING_MARKER));
  // Strip any stale marker, then add one if the device changed. Index 0 (the
  // live system prompt) is left alone — the caller owns it.
  function applyHandoff(messages, fromDevice, toDevice) {
    const out = messages.filter((m, i) => i === 0 || !isStaleNote(m));
    if (fromDevice && toDevice && String(fromDevice).toLowerCase() !== String(toDevice).toLowerCase()) {
      out.push({ role: "system", content: handoffNote(fromDevice, toDevice) });
    }
    return out;
  }

  const INTERRUPTED_TOOL =
    "ERROR: interrupted before this finished — the app was suspended by the " +
    "operating system mid-call. Assume it did not take effect. Do it again if " +
    "it is still needed.";

  // Make a history that was cut off mid-turn safe to send again.
  //
  // The loop records a tool's result immediately after running it, so a turn
  // killed between those two points leaves an assistant message whose
  // tool_calls have no matching tool reply. That history is not merely untidy:
  // OpenAI-compatible APIs require every tool_call to be answered and reject
  // the request outright, so a resume would fail on its first call and the
  // conversation would be stuck for good.
  //
  // The gaps are filled rather than the assistant message dropped. Dropping it
  // would lose what the model had decided to do, and the reply that goes in its
  // place is true -- the call did not complete -- and tells the model the one
  // thing it cannot otherwise know: whether to repeat the call. It is
  // deliberately "assume it did not take effect", because a tool that ran
  // without its result being recorded is indistinguishable from one that never
  // ran, and every tool here writes whole file contents, so doing it twice
  // lands the same bytes.
  function healInterruptedTurn(messages) {
    const answered = new Set();
    for (const m of messages) {
      if (m && m.role === "tool" && m.tool_call_id) answered.add(m.tool_call_id);
    }
    const out = [];
    for (const m of messages) {
      out.push(m);
      if (!m || m.role !== "assistant" || !Array.isArray(m.tool_calls)) continue;
      for (const tc of m.tool_calls) {
        if (!tc || !tc.id || answered.has(tc.id)) continue;
        // Straight after the message that made the call, so the pairing the
        // API checks for is in the order it expects.
        out.push({ role: "tool", tool_call_id: tc.id, content: INTERRUPTED_TOOL });
        answered.add(tc.id);
      }
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

  // --- context budget ------------------------------------------------------
  // Every turn resends the whole conversation, so an unbounded history walks
  // straight into the model's context limit -- and then EVERY send fails, not
  // just the big one, leaving the chat permanently unusable. Sync makes it
  // worse: a long desktop chat arrives here in full.
  //
  // Trimming happens on TURN boundaries. A user message opens a turn and owns
  // every assistant/tool message until the next one, so whole turns come and go
  // together. That's not tidiness -- an assistant message carrying tool_calls
  // and the tool replies answering it must never be separated, or the API
  // rejects the request outright.
  // Counting characters and dividing is a guess, and a guess is why the limit
  // has to be set well under the model's real window. But the API reports the
  // exact prompt_tokens of every request it answers — so measure the ratio from
  // that instead of assuming it. calibrateRatio() turns one real reading into
  // the divisor for everything after, which is both more accurate and adapts to
  // the content (dense code tokenises very differently from prose).
  const DEFAULT_CHARS_PER_TOKEN = 3.6;   // matches the desktop's starting guess
  const IMAGE_CHARS = 4000;              // an image costs far more than its URL is long
  const RATIO_MIN = 1.5, RATIO_MAX = 8;  // ignore readings outside anything plausible

  function messageChars(messages) {
    let chars = 0;
    for (const m of messages || []) {
      const c = m.content;
      if (typeof c === "string") chars += c.length;
      else if (Array.isArray(c)) {
        for (const part of c) {
          // Counting a base64 data URL by length would read as tens of
          // thousands of tokens and trigger endless pointless trimming.
          if (part && part.type === "image_url") chars += IMAGE_CHARS;
          else if (part && part.text) chars += part.text.length;
        }
      }
      for (const tc of m.tool_calls || []) {
        chars += ((tc.function && tc.function.arguments) || "").length + 24;
      }
    }
    return chars;
  }

  function estimateTokens(messages, charsPerToken) {
    const r = Number(charsPerToken) > 0 ? Number(charsPerToken) : DEFAULT_CHARS_PER_TOKEN;
    return Math.ceil(messageChars(messages) / r);
  }

  // Derive chars-per-token from a request the API actually priced. Returns null
  // for readings that can't be right, so one odd response can't skew the meter.
  function calibrateRatio(messages, promptTokens) {
    const tokens = Number(promptTokens);
    if (!(tokens > 0)) return null;
    const chars = messageChars(messages);
    if (chars <= 0) return null;
    const ratio = chars / tokens;
    if (!(ratio >= RATIO_MIN && ratio <= RATIO_MAX)) return null;
    return ratio;
  }

  // Split into system messages (always kept -- small, and deliberately
  // load-bearing) and whole turns of everything else.
  function splitTurns(messages) {
    const system = [], turns = [];
    let cur = null;
    (messages || []).forEach((m, i) => {
      if (m.role === "system") { system.push(i); return; }
      if (m.role === "user" || !cur) { cur = [i]; turns.push(cur); return; }
      cur.push(i);
    });
    return { system, turns };
  }

  // Drop the oldest whole turns until the rest fits. The newest turn is always
  // kept even if it alone blows the budget -- refusing to send the thing the
  // user just typed would be worse than letting the model complain.
  function trimHistory(messages, budgetTokens) {
    const all = messages || [];
    const { system, turns } = splitTurns(all);
    const at = (idx) => idx.map((i) => all[i]);
    let budget = budgetTokens - estimateTokens(at(system));
    const keptTurns = [];
    for (let t = turns.length - 1; t >= 0; t--) {
      const cost = estimateTokens(at(turns[t]));
      if (keptTurns.length && cost > budget) break;
      budget -= cost;
      keptTurns.push(t);
    }
    const droppedTurns = turns.filter((_, t) => !keptTurns.includes(t));
    if (!droppedTurns.length) return { messages: all, dropped: [], droppedTurns: 0 };
    const drop = new Set(droppedTurns.flat());
    return {
      messages: all.filter((_, i) => !drop.has(i)),
      dropped: at([...drop].sort((a, b) => a - b)),
      droppedTurns: droppedTurns.length,
    };
  }

  // A plain-text rendering of dropped turns, for asking the model to summarise
  // them. Tool output is clipped hard: the point is what happened, not the bytes.
  function historyDigest(messages, perMessage = 600) {
    const out = [];
    for (const m of messages || []) {
      let text = typeof m.content === "string" ? m.content
        : Array.isArray(m.content)
          ? m.content.map((p) => (p && p.text) || (p && p.type === "image_url" ? "[image]" : "")).join(" ")
          : "";
      for (const tc of m.tool_calls || []) {
        text += `\n[called ${(tc.function && tc.function.name) || "tool"}]`;
      }
      text = text.trim();
      if (text) out.push(`${m.role}: ${text.slice(0, perMessage)}`);
    }
    return out.join("\n");
  }

  const COMPACT_PROMPT =
    "Summarise this earlier part of a coding conversation so work can continue without it. " +
    "Keep: what the user is trying to achieve, decisions made and why, files changed and how, " +
    "and anything still unfinished or unverified. Drop pleasantries and tool noise. " +
    "Be specific about file paths and names. Write it as notes, not prose.";

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
    // arrive in fragments (name first, then the arguments JSON a few
    // characters at a time), so they're accumulated rather than replaced;
    // which call a fragment belongs to is worked out below.
    function applyDelta(msg, d, onDelta) {
      if (!d) return;
      if (d.content) { msg.content += d.content; if (onDelta) onDelta(d.content); }
      if (!d.tool_calls) return;
      msg.tool_calls = msg.tool_calls || [];
      for (const tc of d.tool_calls) {
        // WHICH call this delta belongs to. `index` is the OpenAI way and
        // z.ai sends it, but Google's compatibility layer omits it entirely
        // when it issues calls in parallel -- and then arrival order alone
        // splits one call's streamed arguments across several slots, while
        // defaulting to 0 merges several calls into one and concatenates
        // their arguments into {"path":"a.png"}{"path":"b.png"}, which is
        // not JSON and takes the whole turn down with it.
        //
        // An id names a call outright, so it wins wherever there is one: a
        // new id is a new call whatever the index says. A delta carrying
        // neither is a continuation, and extends the call most recently
        // opened. The desktop client keys these the same way on purpose.
        let i;
        if (tc.id) {
          i = msg.tool_calls.findIndex((s) => s && s.id === tc.id);
          if (i < 0) {
            i = tc.index;
            if (i == null || msg.tool_calls[i]) i = msg.tool_calls.length;
          }
        } else if (tc.index != null) {
          i = tc.index;
        } else {
          i = msg.tool_calls.length ? msg.tool_calls.length - 1 : 0;
        }
        if (!msg.tool_calls[i]) msg.tool_calls[i] = { id: "", type: "function", function: { name: "", arguments: "" } };
        const slot = msg.tool_calls[i];
        if (tc.id) slot.id = tc.id;
        if (tc.function) {
          if (tc.function.name) slot.function.name = tc.function.name;
          if (tc.function.arguments) slot.function.arguments += tc.function.arguments;
        }
        // Whatever the provider attached to the call, kept verbatim. Gemini 3
        // returns extra_content.google.thought_signature here and rejects the
        // next request without it ("Function call is missing a
        // thought_signature"), so rebuilding the call from id/name/arguments
        // alone kills tool use a step or two in. The desktop client does the
        // same; these two have to agree, because a chat started on one is
        // continued on the other.
        if (tc.extra_content) {
          slot.extra_content = Object.assign({}, slot.extra_content, tc.extra_content);
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
          // Providers that report usage do it on a trailing chunk. Keep it if
          // it comes; the token meter falls back to an estimate if it doesn't.
          if (j.usage) msg.usage = j.usage;
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
            const m = (j.choices && j.choices[0] && j.choices[0].message) || { role: "assistant", content: "" };
            if (j.usage) m.usage = j.usage;   // exact token counts, when offered
            return m;
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
    // Called with the path just before it is first written, so a caller can
    // capture what was there. A worker needs that to be able to undo itself,
    // and the only moment the previous content is still knowable is here.
    const beforeWrite = opts.beforeWrite || (async () => {});
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
        await beforeWrite(a.path);
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
        await beforeWrite(a.path);
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
    // Hand work to the machine that can actually run it. Saying "run the tests
    // later" in prose loses the intent the moment the reply scrolls away; this
    // travels with the chat and is put in front of the desktop agent on open.
    if (opts.needsDesktop) {
      api.needs_desktop = async (a) =>
        String(await opts.needsDesktop(String(a.task || "").trim(), String(a.why || "").trim()));
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
  /* The worker tools, mirroring glmcode/tools.py CONVERSATIONAL_SCHEMAS exactly.
   *
   * Duplicated rather than fetched, for the same reason SETUP_PRESETS is: this
   * file is loaded by a page with no Python behind it. tests/test_phone_workers.py
   * compares the two, because a description that drifts changes how the model
   * uses the tool without anything failing.
   *
   * These went to the phone because a spoken session there could read and hand
   * off, and nothing else -- so asking it to do work got a note left for the
   * desktop instead of the work. That is the right default for a device that
   * cannot run in the background, and the wrong one to impose on someone who
   * knows the trade and wants the work done anyway.
   */
  const WORKER_SCHEMAS = [
    tool("dispatch_worker",
         "Hand a piece of real work off to a background worker that runs on its own, immediately, WITHOUT blocking the conversation -- so you can keep talking to the user while it works. Use this for ANYTHING that takes real doing: writing or editing code, running commands or tests, searching or analyzing the codebase, multi-step tasks. The worker has the full tool set and works autonomously; it CANNOT ask questions and does NOT see this conversation, so give it a COMPLETE, self-contained mission with all the context and specifics it needs. Returns instantly with a worker id -- do not wait for it; just tell the user out loud you've started on it and carry on. The user will be told out loud when it finishes.",
         {"name": {"type": "string", "description": "Short kebab-case label for this worker, e.g. 'add-dark-mode' or 'fix-login-bug'."}, "task": {"type": "string", "description": "The complete, self-contained mission for the worker, with all context it needs (it can't see this chat)."}}, ["task"]),
    tool("check_workers",
         "Check on the background workers you've dispatched -- what's still running, what finished, and what each one reported. Use this when the user asks how things are going, or before you claim something is done. Returns instantly.",
         {}, []),
    tool("steer_worker",
         "Send a running worker a course-correction or extra instruction WITHOUT stopping it -- use when the user adds to or redirects a task in flight ('also add a dark theme', 'use the other library'). Identify the worker by its id (wk1) or its name.",
         {"worker": {"type": "string", "description": "The worker's id (e.g. 'wk1') or name."}, "message": {"type": "string", "description": "The instruction to send it."}}, ["worker", "message"]),
    tool("stop_worker",
         "Stop a running worker -- use when the user says to cancel or abandon a task in flight. It stops at the next safe point. Identify the worker by its id (wk1) or name.",
         {"worker": {"type": "string", "description": "The worker's id (e.g. 'wk1') or name."}}, ["worker"]),
    tool("worker_changes",
         "Describe exactly what files a finished worker changed (added/edited/deleted). Use when the user asks what a worker did or changed. Identify it by id (wk1) or name.",
         {"worker": {"type": "string", "description": "The worker's id (e.g. 'wk1') or name."}}, ["worker"]),
    tool("revert_worker",
         "Undo a worker's file changes, rolling the project back to how it was right before that worker started. Use when the user says to undo or revert a worker's work. This is destructive, so CONFIRM with the user first. Identify it by id (wk1) or name.",
         {"worker": {"type": "string", "description": "The worker's id (e.g. 'wk1') or name."}}, ["worker"]),
  ];

  const SPAWN_SCHEMA = tool("spawn_agent",
    "Delegate one self-contained sub-task to a fresh sub-agent that works on its own and reports back " +
    "(e.g. 'add tests for X', 'refactor Y'). It can read and edit files but cannot spawn further sub-agents. " +
    "Use it to keep big tasks organised; do the work yourself for small ones.",
    { task: str("The self-contained task for the sub-agent"), context: str("Anything it should know: constraints, files, goals") },
    ["task"]);

  // The phone has no shell, so anything needing one has to travel to the
  // desktop. Recording it beats mentioning it: a line of prose is lost as soon
  // as the reply scrolls away, but this is put in front of the desktop agent
  // the moment the chat opens there.
  const NEEDS_DESKTOP_SCHEMA = tool("needs_desktop",
    "Leave a task for the user's desktop, which has a shell. Use it whenever something needs to be " +
    "RUN or VERIFIED on a real machine — tests, a build, a server, a command — since you cannot do " +
    "any of that here. Record it as you go rather than only mentioning it in your reply; the desktop " +
    "is shown these the moment this chat opens there. One call per task.",
    { task: str("What to run or verify, concretely (e.g. 'run pytest tests/test_sync.py')"),
      why: str("Why it matters (optional) — e.g. what it would confirm") },
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
      takeSteer = () => "", toolSchemas = TOOL_SCHEMAS } = cfg;
    for (let step = 0; step < maxSteps; step++) {
      if (shouldStop()) { onEvent({ type: "stopped" }); return messages; }
      // Steering: a message typed while the turn was already running. Injected
      // BETWEEN steps, never mid-batch, so the model always sees a complete
      // tool round before the new instruction — the same point the desktop
      // picks. Anything still queued when the turn ends is handed back rather
      // than dropped, so a redirect that arrived a moment too late isn't lost.
      const steer = takeSteer();
      if (steer) {
        messages.push({ role: "user", content: steer });
        onEvent({ type: "steered", text: steer });
      }
      onEvent({ type: "thinking" });
      let msg;
      // Stream only when the host actually renders deltas, so sub-agents (whose
      // output is summarised, not shown live) keep the cheaper single read.
      const onDelta = cfg.stream ? (t) => onEvent({ type: "delta", text: t }) : undefined;
      const sentCount = messages.length;
      try { msg = await model.chat(messages, toolSchemas, onDelta); }
      catch (e) { onEvent({ type: "error", text: e.message }); return messages; }
      // The exact prompt_tokens for the messages we just sent — worth far more
      // than any character estimate, so hand it straight to the host.
      if (msg && msg.usage) {
        onEvent({ type: "usage", usage: msg.usage, sent: messages.slice(0, sentCount) });
        delete msg.usage;   // not part of the conversation; don't resend it
      }
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

  /* Providers paired over from the desktop.
   *
   * Scanning again is how an API added on the desktop reaches the phone, so
   * this runs on every scan and not just the first -- which makes MERGING the
   * whole job. Replacing the list would throw away anything set up on the
   * phone itself, and keeping only what is already here would make a re-scan
   * do nothing, which is the feature.
   *
   * Matched on baseUrl rather than name: the name is a label the desktop may
   * relabel (it did, when the primary provider stopped being called "z.ai
   * (free)"), while the URL is what actually identifies an endpoint. */
  function mergeProviders(existing, incoming) {
    const out = (existing || []).map((p) => Object.assign({}, p));
    const at = new Map(out.map((p, i) => [normalizeBase(p.baseUrl), i]));
    for (const inc of incoming || []) {
      if (!inc || !inc.baseUrl) continue;
      const i = at.get(normalizeBase(inc.baseUrl));
      if (i === undefined) {
        out.push(Object.assign({}, inc));
        at.set(normalizeBase(inc.baseUrl), out.length - 1);
        continue;
      }
      // The desktop is the source of truth for what it just sent -- a rotated
      // key has to win. But a field it did not send must not blank one the
      // phone has: an absent key means "nothing to say", not "no key".
      const cur = out[i];
      if (inc.key) cur.key = inc.key;
      if (inc.name) cur.name = inc.name;
      if (inc.models && inc.models.length) cur.models = inc.models.slice();
    }
    return out;
  }

  function normalizeBase(url) {
    return String(url || "").trim().replace(/\/+$/, "").toLowerCase();
  }


  // --- speech to speech (Gemini Live) --------------------------------------
  //
  // The phone has no voice mode at all today, and it is the device where
  // talking is the obvious way in: a keyboard on a phone is the worst part of
  // using this app from one. The Live API fits the phone's hardest constraint
  // exactly -- it is a WebSocket opened by the page, so there is no backend to
  // run, which is the same reason this whole app is Path A.
  //
  // It does NOT change what the phone can do in the background. A live session
  // is a foreground session; iOS suspends the tab and the socket dies with it,
  // the same way a turn does today. Nothing here pretends otherwise.
  //
  // This mirrors glmcode/live.py and the two MUST stay in step: a phone and a
  // desktop pointed at the same model with the same tools should open the same
  // session, and a difference here would show up as one device mishearing
  // tools the other handles. tests/test_live.py pins them against each other.
  const LIVE_WS = "wss://generativelanguage.googleapis.com/ws/"
    + "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent";
  const LIVE_MODEL = "gemini-3.1-flash-live-preview";
  // Fixed by the API, and different in each direction. Swapping them is a
  // chipmunk one way and a drawl the other.
  const LIVE_INPUT_RATE = 16000;
  const LIVE_OUTPUT_RATE = 24000;
  // Everything the JSON Schema subset Gemini accepts does NOT include. A stray
  // key is not ignored: the setup is rejected and no session opens, which from
  // the outside looks exactly like a bad API key.
  const LIVE_SCHEMA_KEYS = ["type", "description", "enum", "items",
                            "properties", "required", "nullable", "format"];

  function liveWsUrl(apiKey) { return LIVE_WS + "?key=" + apiKey; }

  function liveCleanSchema(node) {
    if (!node || typeof node !== "object" || Array.isArray(node)) return node;
    const out = {};
    for (const k of LIVE_SCHEMA_KEYS) {
      if (!(k in node)) continue;
      if (k === "properties" && node[k] && typeof node[k] === "object") {
        out[k] = {};
        for (const [name, sub] of Object.entries(node[k])) out[k][name] = liveCleanSchema(sub);
      } else if (k === "items") {
        out[k] = liveCleanSchema(node[k]);
      } else {
        out[k] = JSON.parse(JSON.stringify(node[k]));
      }
    }
    // An object with no properties is rejected outright, and a function that
    // takes no arguments is an ordinary thing to want.
    if (out.type === "object" && !(out.properties && Object.keys(out.properties).length)) return {};
    return out;
  }

  function liveFunctionDeclarations(schemas) {
    const out = [];
    for (const s of schemas || []) {
      const fn = s && s.type === "function" ? s.function : s;
      if (!fn || !fn.name) continue;
      const decl = { name: fn.name, description: fn.description || "" };
      const params = liveCleanSchema(fn.parameters || {});
      if (params && Object.keys(params).length) decl.parameters = params;
      out.push(decl);
    }
    return out;
  }

  function liveSetup(model, systemPrompt, schemas, opts) {
    opts = opts || {};
    const gen = { responseModalities: ["AUDIO"] };
    if (opts.voice) {
      gen.speechConfig = { voiceConfig: { prebuiltVoiceConfig: { voiceName: opts.voice } } };
    }
    if (opts.language) {
      gen.speechConfig = Object.assign({}, gen.speechConfig, { languageCode: opts.language });
    }
    const setup = {
      model: "models/" + model,
      generationConfig: gen,
      // SIBLINGS of generationConfig, not fields inside it: that block holds
      // only generation parameters. Nested, these are unknown fields, and the
      // server rejects the setup and closes the socket rather than ignoring
      // them -- which reads from here as the connection dropping.
      outputAudioTranscription: {},
      inputAudioTranscription: {},
      systemInstruction: { parts: [{ text: systemPrompt }] },
      contextWindowCompression: { slidingWindow: {} },
      sessionResumption: opts.resumeHandle ? { handle: opts.resumeHandle } : {},
    };
    const decls = liveFunctionDeclarations(schemas);
    if (decls.length) setup.tools = [{ functionDeclarations: decls }];
    return { setup };
  }

  function liveToolResponse(responses) {
    return { toolResponse: { functionResponses: (responses || []).map((r) => ({
      id: r.id || "", name: r.name || "", response: { output: r.output || "" } })) } };
  }

  function liveTextTurn(text) { return { realtimeInput: { text } }; }
  function liveAudioChunk(b64) {
    return { realtimeInput: { audio: { data: b64, mimeType: "audio/pcm;rate=" + LIVE_INPUT_RATE } } };
  }
  function liveAudioStreamEnd() { return { realtimeInput: { audioStreamEnd: true } }; }

  // Float32 at whatever the device gave us -> Int16 at 16kHz, the only rate
  // the API takes. A phone mic commonly runs at 48k, and sending that through
  // unchanged is heard three times too fast.
  function livePcm16(samples, fromRate) {
    const ratio = fromRate / LIVE_INPUT_RATE;
    const out = new Int16Array(Math.floor(samples.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const v = Math.max(-1, Math.min(1, samples[Math.floor(i * ratio)] || 0));
      // Asymmetric on purpose: +1.0 through 0x8000 overflows to -32768, and a
      // loud passage turns to static that sounds like a broken microphone.
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    return new Uint8Array(out.buffer);
  }

  function liveB64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return typeof btoa === "function" ? btoa(s) : Buffer.from(bytes).toString("base64");
  }

  function liveBytes(b64) {
    if (typeof atob !== "function") return new Uint8Array(Buffer.from(b64, "base64"));
    const s = atob(b64);
    const a = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i);
    return a;
  }

  // Spoken, not written. The coding prompt is built for a chat window: it asks
  // for file paths, code blocks and diffs, none of which survive being read
  // out loud. This one is for a conversation you are having while walking.
  const LIVE_VOICE_PROMPT =
    "You are Make No Mistakes, talking with the user out loud on their phone.\n\n" +
    "SPEAK like a person: short sentences, no lists, no markdown, no code blocks, " +
    "no file paths read out character by character. If you need to refer to a file, " +
    "say its name the way a person would ('the sync store', not 'glmcode/syncstore.py').\n\n" +
    "You can read this repository with your tools, and you should — answer from what " +
    "is actually there rather than from memory.\n\n" +
    "You can also CHANGE it. For anything that takes real doing — writing or editing " +
    "code, multi-step work — call dispatch_worker and keep talking: it returns straight " +
    "away and works on its own. Use check_workers before you claim anything is done, " +
    "and steer_worker or stop_worker when the user redirects or cancels. Small, single " +
    "edits you can just make yourself with write_file or edit_file.\n\n" +
    "Workers here run inside this app, so they only make progress while it is open and " +
    "on screen. If the user is about to put the phone away, say so plainly. Never claim " +
    "a worker finished without checking.\n\n" +
    "You have NO shell here. This is a phone: nothing can be run, built, tested or " +
    "served. The moment something needs running, call needs_desktop to leave it for " +
    "the user's computer, then say out loud that you have done so. Do not pretend to " +
    "have run anything.\n\n" +
    "Keep answers to a few sentences unless asked for more. The user cannot skim you.";


  // --- the APIs this app knows how to reach --------------------------------
  //
  // The same catalogue as glmcode/providers.py, trimmed to what a phone needs:
  // where to point, what to call the model, and where the key comes from. The
  // desktop grew this and the phone never got it, so its setup screen still
  // asked for a "z.ai / model API key" and offered a base-URL menu with two
  // z.ai entries in it -- meaning a phone set up by hand could not reach
  // anything else, whatever the rest of the app supported.
  //
  // Duplicated deliberately and pinned by a test: this file is loaded by a
  // page with no Python behind it, and the alternative -- fetching the
  // catalogue from somewhere -- would need a network before you have a key.
  // tests/test_phone_presets.py compares the two.
  //
  // Local providers are absent on purpose. Ollama on a desktop is not
  // reachable from a phone, so offering it here is a menu entry that fails on
  // selection with a connection error and nothing explaining why -- the same
  // reason pairing drops them (see providers_for_phone).
  const SETUP_PRESETS = [
    {
      key: "zai",
      label: "Z.AI",
      baseUrl: "https://api.z.ai/api/paas/v4",
      model: "glm-4.7-flash",
      models: ["glm-4.7-flash"],
      keyUrl: "https://z.ai/manage-apikey/apikey-list",
      note: "GLM coding models. Free tier, no card.",
    },
    {
      key: "google",
      label: "Google AI Studio",
      baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
      model: "gemini-3.5-flash-lite",
      models: ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
               "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash"],
      keyUrl: "https://aistudio.google.com/apikey",
      // The one thing about this option someone might mind and would
      // otherwise only discover afterwards. This app sends source code.
      note: "Gemini, and the only one that can do voice. On the free tier "
        + "Google may train on your prompts.",
    },
    {
      key: "custom",
      label: "Other",
      baseUrl: "",
      model: "",
      models: [],
      keyUrl: "",
      note: "Any OpenAI-compatible endpoint.",
    },
  ];

  function setupPreset(key) {
    return SETUP_PRESETS.find((p) => p.key === key) || null;
  }

  const CoreAPI = {
    encryptVault, decryptVault, deriveKey, PBKDF2_ITERS,
    aesEncrypt, aesDecrypt, exportRawKey, importRawKey,
    makeGitHub, makeModel, makeTools, runAgent, TOOL_SCHEMAS, SPAWN_SCHEMA, VIEW_IMAGE_SCHEMA,
    NEEDS_DESKTOP_SCHEMA, pendingNote, PENDING_MARKER,
    openPairToken, normalizePairCode, pairTokenFrom, PAIR_TOKEN_MIN,
    SYSTEM_PROMPT, SUBAGENT_PROMPT,
    openSync, makeSyncStore, ensureSyncRepo, makeSyncPassphrase, syncStoreExists,
    WORKER_SCHEMAS,
    SYNC_REPO_NAME, SYNC_REPO_BRANCH, STATE_BRANCH,
    DEVICE_LOCK_TTL_MS, DEVICE_LOCK_HEARTBEAT_S,
    IMAGE_RE, imageMime,
    handoffNote, applyHandoff, HANDOFF_MARKER, repoStateWarning,
    healInterruptedTurn, INTERRUPTED_TOOL,
    mergeProviders, normalizeBase,
    SETUP_PRESETS, setupPreset,
    liveWsUrl, liveSetup, liveFunctionDeclarations, liveToolResponse,
    liveTextTurn, liveAudioChunk, liveAudioStreamEnd,
    livePcm16, liveB64, liveBytes,
    LIVE_MODEL, LIVE_INPUT_RATE, LIVE_OUTPUT_RATE, LIVE_VOICE_PROMPT,
    estimateTokens, trimHistory, historyDigest, splitTurns, COMPACT_PROMPT,
    messageChars, calibrateRatio, DEFAULT_CHARS_PER_TOKEN, IMAGE_CHARS,
    _b64: { bytesToB64, b64ToBytes },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = CoreAPI;
  else global.AgentCore = CoreAPI;
})(typeof self !== "undefined" ? self : this);

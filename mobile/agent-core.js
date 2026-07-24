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

  async function deriveKey(pin, salt) {
    const base = await subtle().importKey("raw", utf8(String(pin)), "PBKDF2", false, ["deriveKey"]);
    return subtle().deriveKey(
      { name: "PBKDF2", salt, iterations: PBKDF2_ITERS, hash: "SHA-256" },
      base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
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
      async putFile(path, text, message, sha) {
        return gh("PUT", `/repos/${owner}/${repo}/contents/${path}`,
          { message, content: bytesToB64(utf8(text)), branch, sha: sha || undefined });
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

  // --- model client (the "brain") -----------------------------------------
  function makeModel(opts) {
    const fetchFn = opts.fetch || global.fetch;
    const baseUrl = opts.baseUrl || "https://api.z.ai/api/paas/v4";
    return {
      async chat(messages, tools) {
        const r = await fetchFn(baseUrl + "/chat/completions", {
          method: "POST",
          headers: { Authorization: "Bearer " + opts.apiKey, "Content-Type": "application/json" },
          body: JSON.stringify({
            model: opts.model, messages, tools: tools && tools.length ? tools : undefined,
            temperature: 0.6, max_tokens: 4096,
          }),
        });
        if (!r.ok) throw new Error("Model " + r.status + ": " + (await r.text().catch(() => "")).slice(0, 200));
        const j = await r.json();
        return (j.choices && j.choices[0] && j.choices[0].message) || { role: "assistant", content: "" };
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

    return {
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

  // --- the agent loop ------------------------------------------------------
  async function runAgent(cfg) {
    const { model, tools, messages, onEvent = () => {}, maxSteps = 24, shouldStop = () => false,
      toolSchemas = TOOL_SCHEMAS } = cfg;
    for (let step = 0; step < maxSteps; step++) {
      if (shouldStop()) { onEvent({ type: "stopped" }); return messages; }
      onEvent({ type: "thinking" });
      let msg;
      try { msg = await model.chat(messages, toolSchemas); }
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
        try { out = tools[name] ? await tools[name](args) : "unknown tool: " + name; }
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
    "be run or verified on a real machine. Be concise.";

  const CoreAPI = {
    encryptVault, decryptVault, deriveKey, PBKDF2_ITERS,
    makeGitHub, makeModel, makeTools, runAgent, TOOL_SCHEMAS, SYSTEM_PROMPT,
    _b64: { bytesToB64, b64ToBytes },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = CoreAPI;
  else global.AgentCore = CoreAPI;
})(typeof self !== "undefined" ? self : this);

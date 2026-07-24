/* Node unit tests for the mobile agent core.
 * Run: node --test mobile/agent-core.test.js
 *
 * The security-critical vault is tested first: encrypt→decrypt roundtrip, that a
 * wrong PIN fails (never returns garbage), and that the stored ciphertext leaks
 * no plaintext. Then the GitHub client, tools, and agent loop against fakes.
 */
const test = require("node:test");
const assert = require("node:assert");
const C = require("./agent-core.js");

// ---------------------------------------------------------------- vault --

test("vault: encrypt→decrypt roundtrip recovers the secrets", async () => {
  const secrets = { modelKey: "zai-abc123", githubToken: "ghp_deadbeef", model: "glm-4.6" };
  const blob = await C.encryptVault(secrets, "1234");
  assert.equal(blob.v, 1);
  assert.ok(blob.salt && blob.iv && blob.ct);
  const back = await C.decryptVault(blob, "1234");
  assert.deepEqual(back, secrets);
});

test("vault: wrong PIN throws and never returns plaintext", async () => {
  const blob = await C.encryptVault({ githubToken: "ghp_secret" }, "correct-horse");
  await assert.rejects(() => C.decryptVault(blob, "wrong-pin"), /Wrong PIN/);
});

test("vault: rejects PINs shorter than 4 chars", async () => {
  await assert.rejects(() => C.encryptVault({ a: 1 }, "12"), /at least 4/);
});

test("vault: stored ciphertext contains no plaintext secret", async () => {
  const secret = "ghp_THIS_MUST_NOT_APPEAR";
  const blob = await C.encryptVault({ githubToken: secret }, "1234");
  const serialized = JSON.stringify(blob);
  assert.ok(!serialized.includes(secret), "plaintext leaked into vault blob");
  assert.ok(!serialized.includes("githubToken"), "key name leaked into vault blob");
  // also not recoverable from the raw ciphertext bytes
  const raw = Buffer.from(blob.ct, "base64").toString("latin1");
  assert.ok(!raw.includes(secret));
});

test("vault: each encryption uses a fresh salt and IV", async () => {
  const a = await C.encryptVault({ x: 1 }, "1234");
  const b = await C.encryptVault({ x: 1 }, "1234");
  assert.notEqual(a.salt, b.salt);
  assert.notEqual(a.iv, b.iv);
  assert.notEqual(a.ct, b.ct);
});

test("vault: PBKDF2 iterations meet the security floor", () => {
  assert.ok(C.PBKDF2_ITERS >= 210000);
});

// --------------------------------------------------------------- GitHub --

function fakeFetch(routes) {
  // routes: array of {method, match(path), status?, json?, text?}
  const calls = [];
  const fn = async (url, init) => {
    const method = (init && init.method) || "GET";
    const path = url.replace("https://api.github.com", "");
    calls.push({ method, path, body: init && init.body ? JSON.parse(init.body) : undefined,
                 headers: (init && init.headers) || {} });
    for (const r of routes) {
      if (r.method === method && r.match(path)) {
        const status = r.status || 200;
        return {
          ok: status >= 200 && status < 300, status,
          json: async () => r.json,
          text: async () => r.text || JSON.stringify(r.json || ""),
        };
      }
    }
    return { ok: false, status: 404, json: async () => ({}), text: async () => "not found" };
  };
  fn.calls = calls;
  return fn;
}

const b64 = (s) => Buffer.from(s, "utf8").toString("base64");

test("github: tree lists blobs, getFile decodes base64, putFile sends token", async () => {
  const fetch = fakeFetch([
    { method: "GET", match: (p) => p.includes("/git/trees/"),
      json: { tree: [
        { type: "blob", path: "a.js", size: 10 },
        { type: "tree", path: "src", size: 0 },
        { type: "blob", path: "src/b.js", size: 20 },
      ] } },
    { method: "GET", match: (p) => p.includes("/contents/a.js"),
      json: { content: b64("hello\nworld"), sha: "sha1" } },
    { method: "PUT", match: (p) => p.includes("/contents/a.js"),
      json: { commit: { sha: "newsha" } } },
  ]);
  const gh = C.makeGitHub({ token: "T0KEN", owner: "o", repo: "r", branch: "main", fetch });

  const t = await gh.tree();
  assert.deepEqual(t, [{ path: "a.js", size: 10 }, { path: "src/b.js", size: 20 }]);

  const f = await gh.getFile("a.js");
  assert.equal(f.text, "hello\nworld");
  assert.equal(f.sha, "sha1");

  await gh.putFile("a.js", "new content", "msg", "sha1");
  const put = fetch.calls.find((c) => c.method === "PUT");
  assert.equal(Buffer.from(put.body.content, "base64").toString("utf8"), "new content");
  assert.equal(put.body.branch, "main");
  assert.equal(put.body.sha, "sha1");
  assert.equal(put.headers.Authorization, "Bearer T0KEN");
});

test("github: non-2xx throws with status", async () => {
  const fetch = fakeFetch([
    { method: "GET", match: (p) => p.includes("/user"), status: 401, text: "bad creds" },
  ]);
  const gh = C.makeGitHub({ token: "x", owner: "o", repo: "r", fetch });
  await assert.rejects(() => gh.me(), /GitHub 401/);
});

// ---------------------------------------------------------------- tools --

function toolsOverFiles(files, hooks) {
  // files: {path: text}
  const tree = Object.entries(files).map(([path, text]) => ({ path, size: text.length }));
  const gh = {
    async tree() { return tree; },
    async getFile(path) {
      if (!(path in files)) throw new Error("404 " + path);
      return { text: files[path], sha: "sha-" + path };
    },
    async putFile(path, text) { files[path] = text; return { commit: { sha: "c" } }; },
  };
  return C.makeTools(gh, hooks || {});
}

test("tools: grep finds matching lines with path:line", async () => {
  const tools = toolsOverFiles({
    "a.js": "const x = 1;\nfunction foo() {}\n",
    "b.md": "# title\nfoo bar\n",
    "img.png": "binarybytes",
  });
  const out = await tools.grep({ pattern: "foo" });
  assert.match(out, /a\.js:2:/);
  assert.match(out, /b\.md:2:/);
  assert.ok(!out.includes("img.png"), "binary file should be skipped");
});

test("tools: search_code ranks the relevant file first", async () => {
  const tools = toolsOverFiles({
    "auth/login.js": "function loginUser(username, password) { return checkPassword(); }",
    "util/math.js": "function add(a, b) { return a + b; }",
  });
  const out = await tools.search_code({ query: "user login password check" });
  assert.match(out.split("\n\n")[0], /auth\/login\.js/);
});

test("tools: glob matches by pattern", async () => {
  const tools = toolsOverFiles({ "src/a.js": "x", "src/b.ts": "y", "README.md": "z" });
  const out = await tools.glob({ pattern: "**/*.js" });
  assert.equal(out, "src/a.js");
});

test("tools: edit_file gates on confirmWrite and reports declines", async () => {
  const files = { "a.txt": "hello world" };
  let asked = null;
  const tools = toolsOverFiles(files, { confirmWrite: async (kind, path, next) => { asked = { kind, path, next }; return false; } });
  const out = await tools.edit_file({ path: "a.txt", old_string: "world", new_string: "there" });
  assert.match(out, /declined/);
  assert.equal(files["a.txt"], "hello world", "file must be unchanged when declined");
  assert.equal(asked.kind, "edit");
  assert.equal(asked.next, "hello there");
});

test("tools: edit_file commits when confirmed and calls onCommit", async () => {
  const files = { "a.txt": "hello world" };
  const committed = [];
  const tools = toolsOverFiles(files, { confirmWrite: async () => true, onCommit: (p) => committed.push(p) });
  const out = await tools.edit_file({ path: "a.txt", old_string: "world", new_string: "there" });
  assert.match(out, /Edited and committed/);
  assert.equal(files["a.txt"], "hello there");
  assert.deepEqual(committed, ["a.txt"]);
});

test("tools: edit_file refuses ambiguous old_string without replace_all", async () => {
  const tools = toolsOverFiles({ "a.txt": "a a a" }, { confirmWrite: async () => true });
  const out = await tools.edit_file({ path: "a.txt", old_string: "a", new_string: "b" });
  assert.match(out, /appears 3/);
});

// ------------------------------------------------------------- agent loop --

test("runAgent: executes a tool call then returns the final answer", async () => {
  const seenEvents = [];
  let turn = 0;
  const model = {
    async chat(messages, tools) {
      turn++;
      if (turn === 1) {
        return { role: "assistant", tool_calls: [
          { id: "call1", function: { name: "read_file", arguments: JSON.stringify({ path: "a.txt" }) } },
        ] };
      }
      // second turn: the tool result should now be in the message history
      assert.ok(messages.some((m) => m.role === "tool" && m.tool_call_id === "call1"));
      return { role: "assistant", content: "The file says hello." };
    },
  };
  const tools = { read_file: async (a) => "contents of " + a.path };
  const messages = [{ role: "user", content: "read a.txt" }];
  await C.runAgent({ model, tools, messages, onEvent: (e) => seenEvents.push(e.type) });

  assert.ok(seenEvents.includes("tool"));
  assert.ok(seenEvents.includes("tool_result"));
  assert.ok(seenEvents.includes("answer"));
  assert.equal(messages[messages.length - 1].content, "The file says hello.");
});

test("runAgent: advertises only the given toolSchemas (read-only plan mode)", async () => {
  let seenTools = null;
  const model = { async chat(messages, tools) { seenTools = tools; return { role: "assistant", content: "plan" }; } };
  const readOnly = C.TOOL_SCHEMAS.filter((s) => ["read_file", "grep", "search_code"].includes(s.function.name));
  await C.runAgent({ model, tools: {}, messages: [{ role: "user", content: "plan it" }], toolSchemas: readOnly });
  assert.equal(seenTools.length, 3);
  assert.ok(!seenTools.some((s) => s.function.name === "write_file"), "write tools must not be advertised");
});

test("runAgent: shouldStop halts before calling the model", async () => {
  let called = false;
  const model = { async chat() { called = true; return { role: "assistant", content: "x" }; } };
  const events = [];
  await C.runAgent({ model, tools: {}, messages: [], shouldStop: () => true, onEvent: (e) => events.push(e.type) });
  assert.equal(called, false);
  assert.ok(events.includes("stopped"));
});

test("runAgent: a model error is surfaced as an error event, not a throw", async () => {
  const model = { async chat() { throw new Error("network down"); } };
  const events = [];
  await C.runAgent({ model, tools: {}, messages: [{ role: "user", content: "hi" }], onEvent: (e) => events.push(e) });
  const err = events.find((e) => e.type === "error");
  assert.ok(err && /network down/.test(err.text));
});

test("runAgent: a throwing tool is reported back to the model, loop continues", async () => {
  let turn = 0;
  const model = {
    async chat(messages) {
      turn++;
      if (turn === 1) return { role: "assistant", tool_calls: [
        { id: "c1", function: { name: "boom", arguments: "{}" } } ] };
      const toolMsg = messages.find((m) => m.role === "tool");
      assert.match(toolMsg.content, /ERROR: kaboom/);
      return { role: "assistant", content: "recovered" };
    },
  };
  const tools = { boom: async () => { throw new Error("kaboom"); } };
  const messages = [{ role: "user", content: "go" }];
  await C.runAgent({ model, tools, messages });
  assert.equal(messages[messages.length - 1].content, "recovered");
});

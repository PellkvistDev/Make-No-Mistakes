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

test("session crypto: aesEncrypt/aesDecrypt round-trips under a derived key", async () => {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await C.deriveKey("1234", salt, false);
  const data = { repo: { full_name: "a/b" }, messages: [{ role: "user", content: "hi" }] };
  const blob = await C.aesEncrypt(data, key);
  assert.ok(blob.iv && blob.ct);
  assert.deepEqual(await C.aesDecrypt(blob, key), data);
});

test("session crypto: an exported/imported key still decrypts (keep-signed-in)", async () => {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await C.deriveKey("correcthorse", salt, true); // extractable
  const blob = await C.aesEncrypt({ x: 42 }, key);
  const raw = await C.exportRawKey(key);
  const restored = await C.importRawKey(raw, false);
  assert.deepEqual(await C.aesDecrypt(blob, restored), { x: 42 });
});

test("session crypto: a different key can't decrypt", async () => {
  const k1 = await C.deriveKey("pin-one", crypto.getRandomValues(new Uint8Array(16)), false);
  const k2 = await C.deriveKey("pin-two", crypto.getRandomValues(new Uint8Array(16)), false);
  const blob = await C.aesEncrypt({ secret: 1 }, k1);
  await assert.rejects(() => C.aesDecrypt(blob, k2));
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

test("github: getFileRaw returns bytes intact where getFile would mangle them", async () => {
  // A real PNG header — invalid UTF-8, so the text path corrupts it.
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0xff, 0xfe]);
  const fetch = fakeFetch([
    { method: "GET", match: (p) => p.includes("/contents/logo.png"),
      json: { content: png.toString("base64"), sha: "s", size: png.length } },
  ]);
  const gh = C.makeGitHub({ token: "t", owner: "o", repo: "r", fetch });
  const raw = await gh.getFileRaw("logo.png");
  assert.deepEqual(Buffer.from(raw.b64, "base64"), png, "raw bytes must survive the round trip");
  // and confirm the text path really would have destroyed them
  const viaText = await gh.getFile("logo.png");
  assert.notEqual(Buffer.from(viaText.text, "utf8").toString("base64"), png.toString("base64"));
});

test("github: getFileRaw explains the 1MB contents-API limit instead of returning nothing", async () => {
  const fetch = fakeFetch([
    { method: "GET", match: (p) => p.includes("/contents/big.png"),
      json: { content: "", sha: "s", size: 2 * 1024 * 1024 } },
  ]);
  const gh = C.makeGitHub({ token: "t", owner: "o", repo: "r", fetch });
  await assert.rejects(() => gh.getFileRaw("big.png"), /too large.*1MB/);
});

test("images: imageMime maps extensions (jpg→jpeg, svg→svg+xml)", () => {
  assert.equal(C.imageMime("a/b.png"), "image/png");
  assert.equal(C.imageMime("a/b.JPG"), "image/jpeg");
  assert.equal(C.imageMime("a/b.jpeg"), "image/jpeg");
  assert.equal(C.imageMime("a/b.svg"), "image/svg+xml");
  assert.equal(C.imageMime("a/b.webp"), "image/webp");
  assert.ok(C.IMAGE_RE.test("docs/shot.png"));
  assert.ok(!C.IMAGE_RE.test("src/index.js"));
});

test("tools: read_file redirects to view_image for images instead of returning junk", async () => {
  const fetch = fakeFetch([]);
  const gh = C.makeGitHub({ token: "t", owner: "o", repo: "r", fetch });
  const tools = C.makeTools(gh);
  const out = await tools.read_file({ path: "docs/mockup.png" });
  assert.match(out, /view_image/);
  assert.equal(fetch.calls.length, 0, "must not even fetch the image as text");
});

// ---------------------------------------------------------------- model --

test("model: retries on a 429 (rate limit) then succeeds", async () => {
  let calls = 0;
  const fetch = async () => {
    calls++;
    if (calls < 3) return { ok: false, status: 429, text: async () => '{"error":{"code":"1305"}}' };
    return { ok: true, status: 200, json: async () => ({ choices: [{ message: { role: "assistant", content: "done" } }] }) };
  };
  const retries = [];
  const m = C.makeModel({ apiKey: "k", model: "glm-4.7-flash", fetch, retryBaseMs: 1, onRetry: (n) => retries.push(n) });
  const msg = await m.chat([{ role: "user", content: "hi" }]);
  assert.equal(msg.content, "done");
  assert.deepEqual(retries, [1, 2]);
});

test("model: a persistent rate limit throws a friendly message", async () => {
  const fetch = async () => ({ ok: false, status: 429, text: async () => "访问量过大" });
  const m = C.makeModel({ apiKey: "k", model: "glm-4.7-flash", fetch, retryBaseMs: 1, maxRetries: 2 });
  await assert.rejects(() => m.chat([{ role: "user", content: "hi" }]), /busy right now .rate-limited/);
});

// ------------------------------------------------------ device handoff --
// The desktop and phone don't have the same tools, and the model imitates its
// own history — so moving a chat across has to say so, or it reaches for a
// shell that isn't there. Must stay word-for-word with syncstore.apply_handoff.

test("handoff: the note names both devices and voids the earlier tool calls", () => {
  const note = C.handoffNote("desktop", "phone");
  assert.ok(note.startsWith(C.HANDOFF_MARKER));
  assert.match(note, /desktop/);
  assert.match(note, /phone/);
  assert.match(note, /no shell/);
  assert.match(note, /Do not copy them/);
});

test("handoff: the other direction says the machine can run things", () => {
  assert.match(C.handoffNote("phone", "desktop"), /run commands, tests, servers/);
});

test("handoff: a cross-device open appends a marker and leaves history intact", () => {
  const msgs = [{ role: "system", content: "live prompt" }, { role: "user", content: "hi" }];
  const out = C.applyHandoff(msgs, "desktop", "phone");
  assert.equal(out.length, 3);
  assert.deepEqual(out[0], { role: "system", content: "live prompt" });
  assert.deepEqual(out[1], { role: "user", content: "hi" });
  assert.equal(out[2].role, "system");
  assert.ok(out[2].content.startsWith(C.HANDOFF_MARKER));
});

test("handoff: silent when the device didn't change", () => {
  const msgs = [{ role: "user", content: "hi" }];
  assert.deepEqual(C.applyHandoff(msgs, "phone", "phone"), msgs);
  assert.deepEqual(C.applyHandoff(msgs, "PHONE", "phone"), msgs);
});

test("handoff: markers don't stack when bouncing between devices", () => {
  let m = [{ role: "system", content: "live prompt" }, { role: "user", content: "hi" }];
  m = C.applyHandoff(m, "desktop", "phone");
  m = C.applyHandoff(m, "phone", "desktop");
  m = C.applyHandoff(m, "desktop", "phone");
  const markers = m.filter((x) => String(x.content || "").startsWith(C.HANDOFF_MARKER));
  assert.equal(markers.length, 1);
  assert.match(markers[0].content, /to the phone/);
});

test("handoff: never strips the live system prompt at index 0", () => {
  // Index 0 is the app's own prompt; only stale markers may be removed.
  const m = C.applyHandoff([{ role: "system", content: C.HANDOFF_MARKER + " old" }], "phone", "phone");
  assert.equal(m.length, 1, "index 0 is the caller's slot, never dropped");
});

test("handoff: a chat with no device tag is left alone", () => {
  const msgs = [{ role: "user", content: "hi" }];
  assert.deepEqual(C.applyHandoff(msgs, "", "phone"), msgs);
  assert.deepEqual(C.applyHandoff(msgs, undefined, "phone"), msgs);
});

// The phone reads the repo over the GitHub API, so anything only on the
// desktop's disk is invisible here — editing on top of it means committing
// over it. repoStateWarning is what turns that into a warning.

test("repo state: silent when GitHub really is the latest word", () => {
  assert.equal(C.repoStateWarning({ branch: "main", dirty: false, ahead: 0 }, "main"), "");
  assert.equal(C.repoStateWarning(null, "main"), "");
  assert.equal(C.repoStateWarning({}, "main"), "", "a chat with no published state stays quiet");
});

test("repo state: uncommitted desktop work is called out", () => {
  const w = C.repoStateWarning({ branch: "main", dirty: true, ahead: 0 }, "main");
  assert.match(w, /uncommitted changes/);
  assert.match(w, /committing over that work/);
});

test("repo state: unpushed commits are counted, and pluralised", () => {
  assert.match(C.repoStateWarning({ branch: "main", ahead: 1 }, "main"), /1 commit not pushed/);
  assert.match(C.repoStateWarning({ branch: "main", ahead: 3 }, "main"), /3 commits not pushed/);
});

test("repo state: dirty AND unpushed reads as one sentence", () => {
  const w = C.repoStateWarning({ branch: "main", dirty: true, ahead: 2 }, "main");
  assert.match(w, /uncommitted changes and 2 commits not pushed/);
});

test("repo state: a branch mismatch is its own warning", () => {
  const w = C.repoStateWarning({ branch: "feature-x", dirty: false, ahead: 0 }, "main");
  assert.match(w, /branch "feature-x"/);
  assert.match(w, /"main"/);
  assert.match(w, /different code entirely/);
});

test("repo state: an unknown local branch doesn't invent a mismatch", () => {
  assert.equal(C.repoStateWarning({ branch: "main", dirty: false, ahead: 0 }, ""), "");
});

test("handoff: stale desktop-state notes are cleared alongside the marker", () => {
  const msgs = [
    { role: "system", content: "live prompt" },
    { role: "user", content: "hi" },
    { role: "system", content: "[desktop-state] old news" },
  ];
  const out = C.applyHandoff(msgs, "desktop", "phone");
  assert.ok(!out.some((m) => String(m.content).startsWith("[desktop-state]")));
  assert.equal(out[0].content, "live prompt");
});

test("unknown tool: a desktop-only tool explains itself instead of dead-ending", async () => {
  const model = { async chat() {
    return { role: "assistant", content: "", tool_calls: [
      { id: "1", type: "function", function: { name: "run_command", arguments: '{"cmd":"pytest"}' } }] };
  } };
  const results = [];
  await C.runAgent({ model, tools: { read_file: async () => "x" }, messages: [], maxSteps: 1,
    onEvent: (e) => { if (e.type === "tool_result") results.push(e.out); } });
  assert.match(results[0], /no shell here/);
  assert.match(results[0], /ran on the user's desktop/);
  assert.match(results[0], /read_file/, "and lists what it actually has");
});

test("unknown tool: a plain typo doesn't get the desktop explanation", async () => {
  const model = { async chat() {
    return { role: "assistant", content: "", tool_calls: [
      { id: "1", type: "function", function: { name: "raed_file", arguments: "{}" } }] };
  } };
  const results = [];
  await C.runAgent({ model, tools: { read_file: async () => "x" }, messages: [], maxSteps: 1,
    onEvent: (e) => { if (e.type === "tool_result") results.push(e.out); } });
  assert.doesNotMatch(results[0], /desktop/);
  assert.match(results[0], /read_file/);
});

// ------------------------------------------------------ context budget --
// Every turn resends the whole conversation, so without trimming a long chat
// eventually fails EVERY send, not just the big one. The subtle part is that
// an assistant message carrying tool_calls and the tool replies answering it
// must never be split, or the API rejects the request outright.

const sys = (t) => ({ role: "system", content: t });
const usr = (t) => ({ role: "user", content: t });
const asst = (t) => ({ role: "assistant", content: t });
const callsTool = (id, name) => ({ role: "assistant", content: "",
  tool_calls: [{ id, type: "function", function: { name, arguments: "{}" } }] });
const toolReply = (id, t) => ({ role: "tool", tool_call_id: id, content: t });

test("tokens: estimated from text length at the default ratio", () => {
  assert.equal(C.estimateTokens([usr("a".repeat(360))]), 100);
  assert.equal(C.estimateTokens([]), 0);
});

test("tokens: a calibrated ratio overrides the default guess", () => {
  const msgs = [usr("a".repeat(360))];
  assert.equal(C.estimateTokens(msgs, 3.6), 100);
  assert.equal(C.estimateTokens(msgs, 2), 180, "denser tokenisation, more tokens");
  assert.equal(C.estimateTokens(msgs, 0), 100, "a nonsense ratio falls back to the default");
});

test("tokens: an image is a flat cost, not its data-URL length", () => {
  // A base64 data URL counted by length would read as tens of thousands of
  // tokens and trigger endless pointless trimming.
  const huge = "data:image/png;base64," + "A".repeat(200000);
  const withImage = [{ role: "user", content: [{ type: "image_url", image_url: { url: huge } }] }];
  assert.equal(C.messageChars(withImage), C.IMAGE_CHARS);
});

// Calibration: the API tells us the exact prompt_tokens it charged, so the
// divisor can be measured instead of assumed — which is what lets the limit
// sit near the model's real window instead of well under it.

test("calibrate: derives the real ratio from a priced request", () => {
  const msgs = [usr("a".repeat(1000))];
  assert.equal(C.calibrateRatio(msgs, 250), 4);
  assert.equal(C.calibrateRatio(msgs, 500), 2);
});

test("calibrate: rejects readings that can't be right", () => {
  const msgs = [usr("a".repeat(1000))];
  assert.equal(C.calibrateRatio(msgs, 0), null);
  assert.equal(C.calibrateRatio(msgs, -5), null);
  assert.equal(C.calibrateRatio(msgs, undefined), null);
  assert.equal(C.calibrateRatio([], 100), null, "no content to measure");
  assert.equal(C.calibrateRatio(msgs, 1), null, "1000 chars/token is not plausible");
  assert.equal(C.calibrateRatio(msgs, 900), null, "~1 char/token is not plausible either");
});

test("usage: a non-streamed reply carries the exact token counts", async () => {
  const fetch = async () => ({ ok: true, status: 200,
    json: async () => ({ choices: [{ message: { role: "assistant", content: "hi" } }],
                         usage: { prompt_tokens: 1234, completion_tokens: 7 } }),
    text: async () => "" });
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const msg = await m.chat([usr("hello")]);
  assert.equal(msg.usage.prompt_tokens, 1234);
});

test("usage: a trailing SSE chunk's usage is kept", async () => {
  const fetch = async () => sseResponse([
    delta({ content: "hi" }),
    sseData({ choices: [{ delta: {} }], usage: { prompt_tokens: 999, completion_tokens: 2 } }),
    "data: [DONE]\n",
  ]);
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const msg = await m.chat([usr("hello")], [], () => {});
  assert.equal(msg.usage.prompt_tokens, 999);
  assert.equal(msg.content, "hi");
});

test("usage: runAgent reports it and keeps it out of the conversation", async () => {
  const model = { async chat() {
    return { role: "assistant", content: "done", usage: { prompt_tokens: 500 } };
  } };
  const seen = [];
  const msgs = [usr("go")];
  await C.runAgent({ model, tools: {}, messages: msgs,
    onEvent: (e) => { if (e.type === "usage") seen.push(e); } });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].usage.prompt_tokens, 500);
  assert.equal(seen[0].sent.length, 1, "the messages that were actually priced");
  assert.ok(!msgs.some((m) => m.usage), "usage must not be resent as conversation");
});

test("tokens: tool_call arguments are counted", () => {
  const withArgs = [{ role: "assistant", content: "", tool_calls: [
    { id: "1", type: "function", function: { name: "x", arguments: "a".repeat(400) } }] }];
  assert.ok(C.estimateTokens(withArgs) >= 100);
});

test("trim: leaves a conversation that already fits completely alone", () => {
  const msgs = [sys("p"), usr("hi"), asst("hello")];
  const out = C.trimHistory(msgs, 10000);
  assert.equal(out.messages, msgs, "same array back when nothing to do");
  assert.equal(out.droppedTurns, 0);
});

test("trim: drops the oldest turns and keeps the newest", () => {
  const msgs = [sys("p")];
  for (let i = 0; i < 10; i++) { msgs.push(usr("q" + i + " " + "x".repeat(400))); msgs.push(asst("a" + i)); }
  const out = C.trimHistory(msgs, 300);
  const kept = out.messages.filter((m) => m.role === "user").map((m) => m.content.slice(0, 3));
  assert.ok(out.droppedTurns > 0, "something was dropped");
  assert.ok(kept.includes("q9 "), "the newest turn survives");
  assert.ok(!kept.includes("q0 "), "the oldest turn is gone");
});

test("trim: never separates tool replies from the call that made them", () => {
  const msgs = [sys("p")];
  for (let i = 0; i < 8; i++) {
    msgs.push(usr("q" + i + " " + "x".repeat(400)));
    msgs.push(callsTool("t" + i, "read_file"));
    msgs.push(toolReply("t" + i, "contents " + "y".repeat(400)));
    msgs.push(asst("done " + i));
  }
  const out = C.trimHistory(msgs, 400);
  assert.ok(out.droppedTurns > 0);
  // every tool reply kept must still have its assistant tool_call present
  const liveIds = new Set();
  for (const m of out.messages) for (const tc of m.tool_calls || []) liveIds.add(tc.id);
  for (const m of out.messages) {
    if (m.role === "tool") {
      assert.ok(liveIds.has(m.tool_call_id),
        `orphaned tool reply ${m.tool_call_id} — the API would reject this`);
    }
  }
});

test("trim: keeps every system message wherever it sits", () => {
  // The live prompt is at index 0; handoff/desktop-state notes are appended at
  // the end. Both are small and load-bearing, so neither may be trimmed away.
  const msgs = [sys("live prompt")];
  for (let i = 0; i < 10; i++) { msgs.push(usr("q" + i + " " + "x".repeat(400))); msgs.push(asst("a")); }
  msgs.push(sys("[device-handoff] you are now on the phone"));
  const out = C.trimHistory(msgs, 250);
  const kept = out.messages.filter((m) => m.role === "system").map((m) => m.content);
  assert.deepEqual(kept, ["live prompt", "[device-handoff] you are now on the phone"]);
});

test("trim: keeps the newest turn even when it alone blows the budget", () => {
  const msgs = [sys("p"), usr("old"), asst("a"), usr("x".repeat(40000))];
  const out = C.trimHistory(msgs, 100);
  const users = out.messages.filter((m) => m.role === "user");
  assert.equal(users.length, 1);
  assert.equal(users[0].content.length, 40000, "refusing to send what was just typed is worse");
});

test("trim: reports what it dropped, in order", () => {
  const msgs = [sys("p"), usr("first " + "x".repeat(400)), asst("a1"),
                usr("second " + "x".repeat(400)), asst("a2")];
  const out = C.trimHistory(msgs, 120);
  assert.ok(out.dropped.length > 0);
  assert.equal(out.dropped[0].content.slice(0, 5), "first");
});

test("trim: an assistant message before any user message is still a turn", () => {
  const out = C.trimHistory([sys("p"), asst("orphan"), usr("hi")], 10000);
  assert.equal(out.messages.length, 3, "no message is silently lost");
});

test("digest: renders roles, names tool calls, and clips long output", () => {
  const d = C.historyDigest([
    usr("do the thing"),
    callsTool("t1", "read_file"),
    toolReply("t1", "z".repeat(5000)),
  ], 100);
  assert.match(d, /user: do the thing/);
  assert.match(d, /\[called read_file\]/);
  assert.ok(!d.includes("z".repeat(200)), "tool output is clipped hard");
});

test("digest: images are named, not inlined", () => {
  const d = C.historyDigest([{ role: "user", content: [
    { type: "text", text: "look" },
    { type: "image_url", image_url: { url: "data:image/png;base64,AAAA" } }] }]);
  assert.match(d, /look/);
  assert.match(d, /\[image\]/);
  assert.ok(!d.includes("base64"));
});

// ------------------------------------------------------------ streaming --
// Build a fake SSE response whose body yields the given chunks. Chunks are
// deliberately split at awkward points to prove the line buffering works.
function sseResponse(chunks) {
  return {
    ok: true, status: 200,
    body: {
      getReader() {
        let i = 0;
        return { async read() {
          if (i >= chunks.length) return { done: true, value: undefined };
          return { done: false, value: new TextEncoder().encode(chunks[i++]) };
        } };
      },
    },
    json: async () => { throw new Error("should have streamed, not read json"); },
    text: async () => "",
  };
}
const sseData = (obj) => "data: " + JSON.stringify(obj) + "\n";
const delta = (d) => sseData({ choices: [{ delta: d }] });

test("stream: text deltas arrive live and assemble into the final message", async () => {
  const fetch = async () => sseResponse([
    delta({ role: "assistant", content: "Hel" }),
    delta({ content: "lo, " }) + delta({ content: "world" }),
    "data: [DONE]\n",
  ]);
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const seen = [];
  const msg = await m.chat([{ role: "user", content: "hi" }], [], (t) => seen.push(t));
  assert.deepEqual(seen, ["Hel", "lo, ", "world"], "each chunk surfaces as it lands");
  assert.equal(msg.content, "Hello, world");
  assert.equal(msg.role, "assistant");
});

test("stream: a data line split across chunks is still parsed once whole", async () => {
  const line = delta({ content: "split!" });
  const fetch = async () => sseResponse([line.slice(0, 12), line.slice(12), "data: [DONE]\n"]);
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const seen = [];
  const msg = await m.chat([{ role: "user", content: "hi" }], [], (t) => seen.push(t));
  assert.deepEqual(seen, ["split!"]);
  assert.equal(msg.content, "split!");
});

test("stream: fragmented tool_call deltas accumulate into one usable call", async () => {
  const fetch = async () => sseResponse([
    delta({ tool_calls: [{ index: 0, id: "call_1", function: { name: "read_file", arguments: "" } }] }),
    delta({ tool_calls: [{ index: 0, function: { arguments: '{"pa' } }] }),
    delta({ tool_calls: [{ index: 0, function: { arguments: 'th":"a.js"}' } }] }),
    "data: [DONE]\n",
  ]);
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const msg = await m.chat([{ role: "user", content: "hi" }], [], () => {});
  assert.equal(msg.tool_calls.length, 1);
  assert.equal(msg.tool_calls[0].id, "call_1");
  assert.equal(msg.tool_calls[0].function.name, "read_file");
  assert.deepEqual(JSON.parse(msg.tool_calls[0].function.arguments), { path: "a.js" });
});

test("stream: two parallel tool calls stay separate by index", async () => {
  const fetch = async () => sseResponse([
    delta({ tool_calls: [{ index: 0, id: "a", function: { name: "glob", arguments: '{"p":1}' } }] }),
    delta({ tool_calls: [{ index: 1, id: "b", function: { name: "grep", arguments: '{"p":2}' } }] }),
  ]);
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const msg = await m.chat([{ role: "user", content: "hi" }], [], () => {});
  assert.equal(msg.tool_calls.length, 2);
  assert.deepEqual(msg.tool_calls.map((t) => t.function.name), ["glob", "grep"]);
});

test("stream: malformed SSE lines are skipped, not fatal", async () => {
  const fetch = async () => sseResponse([
    ": keep-alive comment\n",
    "data: {not json}\n",
    delta({ content: "still here" }),
    "\n",
  ]);
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const msg = await m.chat([{ role: "user", content: "hi" }], [], () => {});
  assert.equal(msg.content, "still here");
});

test("stream: falls back to a normal read when the runtime can't stream", async () => {
  // No .body on the response (older WebViews) — must not hang or throw.
  const fetch = async () => ({ ok: true, status: 200,
    json: async () => ({ choices: [{ message: { role: "assistant", content: "plain" } }] }),
    text: async () => "" });
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  const msg = await m.chat([{ role: "user", content: "hi" }], [], () => {});
  assert.equal(msg.content, "plain");
});

test("stream: only requested when a delta handler is passed", async () => {
  const bodies = [];
  const fetch = async (url, init) => {
    bodies.push(JSON.parse(init.body));
    return { ok: true, status: 200,
      json: async () => ({ choices: [{ message: { role: "assistant", content: "x" } }] }),
      text: async () => "" };
  };
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch });
  await m.chat([{ role: "user", content: "hi" }]);
  assert.equal(bodies[0].stream, undefined, "no handler → no stream flag");
});

test("stream: a rate limit still retries before streaming starts", async () => {
  let n = 0;
  const fetch = async () => {
    if (++n < 2) return { ok: false, status: 429, text: async () => "rate limit" };
    return sseResponse([delta({ content: "after retry" })]);
  };
  const m = C.makeModel({ apiKey: "k", model: "glm", fetch, retryBaseMs: 1 });
  const msg = await m.chat([{ role: "user", content: "hi" }], [], () => {});
  assert.equal(msg.content, "after retry");
});

test("runAgent: streams deltas only when cfg.stream is set", async () => {
  const model = { async chat(messages, tools, onDelta) {
    if (onDelta) { onDelta("a"); onDelta("b"); }
    return { role: "assistant", content: "ab" };
  } };
  const streamed = [];
  await C.runAgent({ model, tools: {}, messages: [], stream: true,
    onEvent: (e) => { if (e.type === "delta") streamed.push(e.text); } });
  assert.deepEqual(streamed, ["a", "b"]);

  const quiet = [];
  await C.runAgent({ model, tools: {}, messages: [],
    onEvent: (e) => { if (e.type === "delta") quiet.push(e.text); } });
  assert.deepEqual(quiet, [], "sub-agents (no cfg.stream) keep the cheaper single read");
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

test("tools: spawn_agent exists only when a spawner is wired, and forwards args", async () => {
  const plain = toolsOverFiles({ "a.txt": "x" });
  assert.equal(typeof plain.spawn_agent, "undefined", "no spawn tool without opts.spawn");

  let got = null;
  const withSpawn = toolsOverFiles({ "a.txt": "x" }, {
    spawn: async (task, context) => { got = { task, context }; return "sub done: " + task; },
  });
  assert.equal(typeof withSpawn.spawn_agent, "function");
  const out = await withSpawn.spawn_agent({ task: "add tests", context: "for auth" });
  assert.deepEqual(got, { task: "add tests", context: "for auth" });
  assert.equal(out, "sub done: add tests");
});

test("tools: view_image exists only when a viewer is wired, and forwards args", async () => {
  const plain = toolsOverFiles({ "a.txt": "x" });
  assert.equal(typeof plain.view_image, "undefined");

  let got = null;
  const withViewer = toolsOverFiles({ "a.txt": "x" }, {
    viewImage: async (name, question) => { got = { name, question }; return "a red logo on white"; },
  });
  const out = await withViewer.view_image({ name: "logo.png", question: "what colour?" });
  assert.deepEqual(got, { name: "logo.png", question: "what colour?" });
  assert.equal(out, "a red logo on white");
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

// -------------------------------------------------------------- sync store --

// An in-memory stand-in for a sync gh-client (branch=STATE_BRANCH). Models the
// orphan branch as a flat {path: text} filesystem so we can exercise the
// encrypted store without a network. Returns { gh, files, branchCreated }.
function fakeSyncGh(initial) {
  const files = Object.assign({}, initial);
  const state = { branch: initial ? "sha0" : null, orphanCalls: 0 };
  const gh = {
    async getFile(path) {
      if (!(path in files)) throw new Error("GitHub 404: not found " + path);
      return { text: files[path], sha: "sha-" + path + "-" + files[path].length };
    },
    async putFile(path, text, message, sha) { files[path] = text; return { commit: { sha: "c" } }; },
    async deleteFile(path, message, sha) { delete files[path]; return null; },
    async branchSha() { return state.branch; },
    async createOrphanBranch() { state.orphanCalls++; state.branch = "sha-orphan"; return state.branch; },
  };
  return { gh, files, state };
}

test("sync: openSync bootstraps a brand-new store (creates branch + sync.json)", async () => {
  const { gh, files, state } = fakeSyncGh(null);
  const { key, store, created } = await C.openSync(gh, "correct horse battery");
  assert.equal(created, true);
  assert.equal(state.orphanCalls, 1, "orphan branch created on first device");
  assert.ok(files["sync.json"], "sync.json written");
  const meta = JSON.parse(files["sync.json"]);
  assert.equal(meta.v, 1);
  assert.ok(meta.salt && meta.check, "salt + check-blob present");
  // check-blob must be encrypted, not the plaintext sentinel
  assert.ok(!JSON.stringify(meta.check).includes("mnm-sync-ok"));
  assert.ok(key && store, "returns a usable key + store");
});

test("sync: a second device with the RIGHT passphrase re-derives the same key", async () => {
  const { gh } = fakeSyncGh(null);
  const first = await C.openSync(gh, "shared secret 1");
  await first.store.save({ id: "c1", title: "Hello", messages: [{ role: "user", content: "hi" }] });
  // second device: same files, fresh open
  const again = await C.openSync(gh, "shared secret 1");
  assert.equal(again.created, false);
  const loaded = await again.store.load("c1");
  assert.equal(loaded.title, "Hello");
  assert.equal(loaded.messages[0].content, "hi");
});

test("sync: the WRONG passphrase is rejected (never returns garbage)", async () => {
  const { gh } = fakeSyncGh(null);
  await C.openSync(gh, "the real passphrase");
  await assert.rejects(() => C.openSync(gh, "an impostor phrase"), /Wrong sync passphrase/);
});

test("sync: openSync rejects too-short passphrases", async () => {
  const { gh } = fakeSyncGh(null);
  await assert.rejects(() => C.openSync(gh, "short"), /at least 6/);
});

test("sync: stored chat + index files contain no plaintext", async () => {
  const { gh, files } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "passphrase here");
  await store.save({ id: "c9", title: "Secret Project", preview: "TOP SECRET text",
    messages: [{ role: "user", content: "launch codes 0000" }] });
  const dump = JSON.stringify(files);
  assert.ok(!dump.includes("Secret Project"), "title leaked");
  assert.ok(!dump.includes("TOP SECRET"), "preview leaked");
  assert.ok(!dump.includes("launch codes"), "message leaked");
});

test("sync: save→list→load→remove lifecycle keeps the index in step", async () => {
  const { gh } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "lifecycle pass");
  await store.save({ id: "a", title: "Alpha", messages: [] });
  await new Promise((r) => setTimeout(r, 2)); // ensure a later timestamp
  await store.save({ id: "b", title: "Beta", messages: [] });

  const list = await store.list();
  assert.equal(list.length, 2);
  assert.equal(list[0].id, "b", "newest chat is first");
  assert.ok(list[0].updated >= list[1].updated);

  // re-saving updates the entry in place, not duplicates it
  await store.save({ id: "a", title: "Alpha renamed", messages: [] });
  const list2 = await store.list();
  assert.equal(list2.length, 2);
  assert.equal(list2.find((c) => c.id === "a").title, "Alpha renamed");

  await store.remove("a");
  const list3 = await store.list();
  assert.equal(list3.length, 1);
  assert.equal(list3[0].id, "b");
  await assert.rejects(() => store.load("a"), /404/);
});

test("sync: list on an empty store returns [] (no index yet)", async () => {
  const { gh } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "empty store pass");
  assert.deepEqual(await store.list(), []);
});

// ------------------------------------------------ cross-device lock -----
// A courtesy, not a guarantee: TTL self-heals a dead device, and GitHub's
// own sha-gated PUT (compare-and-swap) is the only real atomicity here.
// Mirrors glmcode/syncstore.py's SyncStore lock tests so behavior stays in
// lockstep between the desktop and phone implementations.

// Unlike fakeSyncGh (lenient — accepts any sha, fine for the non-racing
// tests above), this fake enforces real sha-gating so a genuine lost-race
// can actually be exercised.
function fakeLockingGh(initial) {
  const files = Object.assign({}, initial);
  const shas = {};
  let counter = 0;
  const state = { branch: initial ? "sha0" : null, orphanCalls: 0 };
  const gh = {
    async getFile(path) {
      if (!(path in files)) throw new Error("GitHub 404: not found " + path);
      if (!(path in shas)) shas[path] = "seed-" + path;
      return { text: files[path], sha: shas[path] };
    },
    async putFile(path, text, message, sha) {
      const current = shas[path];
      if (current !== undefined ? sha !== current : !!sha) {
        throw new Error("GitHub 409: " + path + " is out of date");
      }
      files[path] = text;
      shas[path] = "sha-" + path + "-" + (++counter);
      return { commit: { sha: "c" } };
    },
    async deleteFile(path, message, sha) {
      const current = shas[path];
      if (current !== sha) throw new Error("GitHub 409: " + path + " is out of date");
      delete files[path]; delete shas[path];
      return null;
    },
    async branchSha() { return state.branch; },
    async createOrphanBranch() { state.orphanCalls++; state.branch = "sha-orphan"; return state.branch; },
  };
  return { gh, files, shas, state };
}

test("lock: acquireLock succeeds when the chat is free", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  const payload = await store.acquireLock("c1", "device-a", "desktop", false);
  assert.equal(payload.device_id, "device-a");
  assert.equal(payload.label, "desktop");
  assert.ok(payload.expires > payload.acquired);
  assert.deepEqual(await store.checkLock("c1"), payload);
});

test("lock: acquireLock fails when held by another live device", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  await assert.rejects(
    () => store.acquireLock("c1", "device-b", "phone", false),
    (e) => { assert.match(e.message, /active on desktop/); assert.equal(e.lockedElsewhere, true);
      assert.equal(e.deviceLabel, "desktop"); return true; });
});

test("lock: acquireLock is a no-op re-lock for the SAME device (no throw)", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  const again = await store.acquireLock("c1", "device-a", "desktop", false);
  assert.equal(again.device_id, "device-a");
});

test("lock: acquireLock succeeds once the existing lock has expired", async () => {
  const { gh, files, shas } = fakeLockingGh(null);
  const { key, store } = await C.openSync(gh, "lock passphrase");
  const expired = { v: 1, device_id: "device-a", device: "desktop", label: "desktop",
    acquired: Date.now() - 200000, expires: Date.now() - 100000 };
  const blob = await C.aesEncrypt(expired, key);
  files["chats/c1.lock.json"] = JSON.stringify(blob);
  shas["chats/c1.lock.json"] = "seed-expired";
  const payload = await store.acquireLock("c1", "device-b", "phone", false);
  assert.equal(payload.device_id, "device-b");
});

test("lock: force overrides a live lock held by another device", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  const payload = await store.acquireLock("c1", "device-b", "phone", true);
  assert.equal(payload.device_id, "device-b");
  assert.equal((await store.checkLock("c1")).device_id, "device-b");
});

test("lock: acquireLock detects a genuine write race and reports the true winner", async () => {
  const { gh } = fakeLockingGh(null);
  const { key, store } = await C.openSync(gh, "lock passphrase");
  const originalPut = gh.putFile.bind(gh);
  let injected = false;
  gh.putFile = async (path, text, message, sha) => {
    if (path === "chats/c1.lock.json" && !injected) {
      injected = true;
      // Device A's lock lands, using the SAME stale sha device B just read,
      // right in the window between B's read and B's own write.
      const payload = { v: 1, device_id: "device-a", device: "desktop", label: "desktop",
        acquired: Date.now(), expires: Date.now() + 90000 };
      const blob = await C.aesEncrypt(payload, key);
      await originalPut(path, JSON.stringify(blob), "Lock c1", sha);
    }
    return originalPut(path, text, message, sha); // B's write now sees a stale sha
  };
  await assert.rejects(
    () => store.acquireLock("c1", "device-b", "phone", false),
    (e) => { assert.match(e.message, /active on desktop/); return true; });
});

test("lock: renewLock extends the TTL while still held by the same device", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  const first = await store.acquireLock("c1", "device-a", "desktop", false);
  await new Promise((r) => setTimeout(r, 5));
  const ok = await store.renewLock("c1", "device-a", "desktop");
  assert.equal(ok, true);
  const now = await store.checkLock("c1");
  assert.ok(now.expires >= first.expires);
});

test("lock: renewLock returns false once another device has taken over", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  await store.acquireLock("c1", "device-b", "phone", true); // force takeover
  const ok = await store.renewLock("c1", "device-a", "desktop");
  assert.equal(ok, false);
});

test("lock: renewLock fails OPEN (true) on a transient write error", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  gh.putFile = async () => { throw new Error("network blip"); };
  const ok = await store.renewLock("c1", "device-a", "desktop");
  assert.equal(ok, true, "a transient error must never be misread as preempted");
});

test("lock: releaseLock frees the chat immediately", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  await store.releaseLock("c1", "device-a");
  assert.equal(await store.checkLock("c1"), null);
  // and it's immediately acquirable again
  const payload = await store.acquireLock("c1", "device-b", "phone", false);
  assert.equal(payload.device_id, "device-b");
});

test("lock: releaseLock does not clear a lock taken over by someone else", async () => {
  const { gh } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "desktop", false);
  await store.acquireLock("c1", "device-b", "phone", true);
  await store.releaseLock("c1", "device-a"); // stale release from the loser
  const current = await store.checkLock("c1");
  assert.equal(current.device_id, "device-b", "the winner's lock must survive a stale release");
});

test("lock: checkLock treats an expired lock as free", async () => {
  const { gh, files, shas } = fakeLockingGh(null);
  const { key, store } = await C.openSync(gh, "lock passphrase");
  const expired = { v: 1, device_id: "device-a", device: "desktop", label: "desktop",
    acquired: Date.now() - 200000, expires: Date.now() - 100000 };
  files["chats/c1.lock.json"] = JSON.stringify(await C.aesEncrypt(expired, key));
  shas["chats/c1.lock.json"] = "seed-expired";
  assert.equal(await store.checkLock("c1"), null);
});

test("lock: the lock file is encrypted (no plaintext device label on the wire)", async () => {
  const { gh, files } = fakeLockingGh(null);
  const { store } = await C.openSync(gh, "lock passphrase");
  await store.acquireLock("c1", "device-a", "my-laptop", false);
  const dump = JSON.stringify(files["chats/c1.lock.json"]);
  assert.ok(!dump.includes("my-laptop"), "device label leaked into the lock file");
  assert.ok(!dump.includes("device-a"), "device id leaked into the lock file");
});

// -------------------------------------------------- phone → desktop queue --
// The phone has no shell, so "run the tests" has to travel to the machine that
// does. Prose loses it the moment the reply scrolls away; a structured list
// survives and is put in front of the desktop agent on open.

test("needs_desktop: only wired when the host provides a sink", async () => {
  assert.equal(C.makeTools({}).needs_desktop, undefined);
  const parked = [];
  const tools = C.makeTools({}, { needsDesktop: async (t, w) => { parked.push([t, w]); return "ok"; } });
  assert.equal(typeof tools.needs_desktop, "function");
  await tools.needs_desktop({ task: "run pytest", why: "confirms the fix" });
  assert.deepEqual(parked, [["run pytest", "confirms the fix"]]);
});

test("needs_desktop: trims and tolerates a missing reason", async () => {
  const parked = [];
  const tools = C.makeTools({}, { needsDesktop: async (t, w) => { parked.push([t, w]); return "ok"; } });
  await tools.needs_desktop({ task: "  build the app  " });
  assert.deepEqual(parked, [["build the app", ""]]);
});

test("pending note: numbers the work and explains why it's here", () => {
  const note = C.pendingNote([
    { task: "run pytest tests/test_sync.py", why: "confirms the lock TTL" },
    { task: "start the dev server" },
  ]);
  assert.ok(note.startsWith(C.PENDING_MARKER));
  assert.match(note, /1\. run pytest tests\/test_sync\.py -- confirms the lock TTL/);
  assert.match(note, /2\. start the dev server/);
  assert.match(note, /no shell/);
});

test("pending note: nothing to hand over means no note at all", () => {
  assert.equal(C.pendingNote([]), "");
  assert.equal(C.pendingNote(null), "");
  assert.equal(C.pendingNote([{ task: "   " }]), "");
});

test("pending note: stale ones are cleared like the other per-open notes", () => {
  const msgs = [
    { role: "system", content: "live prompt" },
    { role: "user", content: "hi" },
    { role: "system", content: "[from-your-phone] old asks" },
  ];
  const out = C.applyHandoff(msgs, "desktop", "phone");
  assert.ok(!out.some((m) => String(m.content).startsWith("[from-your-phone]")));
});

// ------------------------------------------------------------ tombstones --
// Deleting used to be an absence, and absence loses to a write: the other
// device still had the chat open, saved it on its next turn, and brought it
// back — every time.

test("delete: records a tombstone", async () => {
  const { gh } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "delete pass here");
  await store.save({ id: "c1", title: "Doomed", messages: [] });
  await store.remove("c1");
  assert.deepEqual(await store.list(), []);
});

test("delete: the other device cannot resurrect a deleted chat", async () => {
  // The exact reported bug: delete on one device, the other still has it open
  // and sends one more message, and it comes back.
  const { gh } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "delete pass here");
  await store.save({ id: "c1", title: "Doomed", messages: [] });
  await store.remove("c1");
  await assert.rejects(
    () => store.save({ id: "c1", title: "Doomed", messages: [{ role: "user", content: "hi" }] }),
    (e) => { assert.equal(e.chatDeleted, true); assert.match(e.message, /deleted on another device/); return true; });
  assert.deepEqual(await store.list(), []);
});

test("delete: other chats are unaffected", async () => {
  const { gh } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "delete pass here");
  await store.save({ id: "keep", title: "Keep", messages: [] });
  await store.save({ id: "drop", title: "Drop", messages: [] });
  await store.remove("drop");
  assert.deepEqual((await store.list()).map((c) => c.id), ["keep"]);
});

test("delete: saving another chat preserves the tombstones", async () => {
  // save() rewrites the index too — it must not wipe the deletions.
  const { gh } = fakeSyncGh(null);
  const { store } = await C.openSync(gh, "delete pass here");
  await store.save({ id: "gone", title: "Gone", messages: [] });
  await store.remove("gone");
  await store.save({ id: "other", title: "Other", messages: [] });
  await assert.rejects(() => store.save({ id: "gone", title: "Gone", messages: [] }),
                       /deleted on another device/);
});

test("delete: an index written before tombstones still loads", async () => {
  const { gh } = fakeSyncGh(null);
  const { key, store } = await C.openSync(gh, "delete pass here");
  const blob = await C.aesEncrypt({ v: 1, chats: [{ id: "c1", updated: 1 }] }, key);
  await gh.putFile("index.json", JSON.stringify(blob), "old-style index");
  assert.deepEqual((await store.list()).map((c) => c.id), ["c1"]);
});

// ------------------------------------------------------------- steering --
// A message typed while a turn is already running redirects that turn instead
// of starting another. Typing is slow on a phone, so the thing you forgot to
// say usually arrives after you've hit send.

test("steer: an injected message reaches the model on the next step", async () => {
  const seen = [];
  let turn = 0;
  const model = { async chat(messages) {
    seen.push(messages.map((m) => m.content));
    if (++turn === 1) {
      return { role: "assistant", content: "", tool_calls: [
        { id: "t1", type: "function", function: { name: "read_file", arguments: "{}" } }] };
    }
    return { role: "assistant", content: "done" };
  } };
  let pending = "also rename it";
  const events = [];
  const msgs = [{ role: "user", content: "refactor this" }];
  await C.runAgent({ model, tools: { read_file: async () => "ok" }, messages: msgs,
    takeSteer: () => { const t = pending; pending = ""; return t; },
    onEvent: (e) => { if (e.type === "steered") events.push(e.text); } });
  assert.deepEqual(events, ["also rename it"]);
  assert.ok(msgs.some((m) => m.role === "user" && m.content === "also rename it"));
});

test("steer: goes in between steps, never inside a tool batch", async () => {
  // The model must see a complete tool round before the new instruction.
  const order = [];
  let turn = 0;
  const model = { async chat(messages) {
    order.push("chat:" + messages.length);
    if (++turn === 1) {
      return { role: "assistant", content: "", tool_calls: [
        { id: "a", type: "function", function: { name: "t", arguments: "{}" } },
        { id: "b", type: "function", function: { name: "t", arguments: "{}" } }] };
    }
    return { role: "assistant", content: "done" };
  } };
  let pending = "";
  await C.runAgent({ model, messages: [{ role: "user", content: "go" }],
    tools: { t: async () => { order.push("tool"); pending = "steer now"; return "x"; } },
    takeSteer: () => { const t = pending; pending = ""; return t; },
    onEvent: (e) => { if (e.type === "steered") order.push("steered"); } });
  // Both tools run, THEN the steer lands, then the next model call.
  assert.deepEqual(order.slice(0, 4), ["chat:1", "tool", "tool", "steered"]);
});

test("steer: nothing queued changes nothing", async () => {
  const model = { async chat() { return { role: "assistant", content: "hi" }; } };
  const msgs = [{ role: "user", content: "go" }];
  await C.runAgent({ model, tools: {}, messages: msgs, takeSteer: () => "" });
  assert.equal(msgs.filter((m) => m.role === "user").length, 1);
});

test("steer: absent hook is harmless (sub-agents don't steer)", async () => {
  const model = { async chat() { return { role: "assistant", content: "hi" }; } };
  const msgs = [{ role: "user", content: "go" }];
  await C.runAgent({ model, tools: {}, messages: msgs });
  assert.equal(msgs[msgs.length - 1].content, "hi");
});

test("steer: a stop still wins over a queued message", async () => {
  const model = { async chat() { return { role: "assistant", content: "hi" }; } };
  const msgs = [{ role: "user", content: "go" }];
  const events = [];
  await C.runAgent({ model, tools: {}, messages: msgs, shouldStop: () => true,
    takeSteer: () => "too late", onEvent: (e) => events.push(e.type) });
  assert.deepEqual(events, ["stopped"]);
  assert.ok(!msgs.some((m) => m.content === "too late"));
});

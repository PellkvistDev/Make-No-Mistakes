/* Make No Mistakes — mobile browser glue.
 *
 * This is the only DOM-touching layer. All crypto, GitHub, model, and agent
 * logic lives in agent-core.js (AgentCore), which is unit-tested in Node.
 *
 * SECURITY posture enforced here:
 *  - The vault (keys) is persisted only as ciphertext; the decrypted secrets
 *    live in memory while unlocked. The conversation is persisted encrypted
 *    under the vault key. "Keep me signed in" (opt-in) remembers the key.
 *  - The app auto-locks after a configurable idle timeout (not on every
 *    backgrounding); it saves the session when hidden.
 *  - Every write is routed through a modal confirm dialog by default.
 */
(function () {
  "use strict";
  const AC = window.AgentCore;
  const $ = (id) => document.getElementById(id);
  const VAULT_KEY = "mnm.vault.v1";
  const SESSION_KEY = "mnm.session.v1";  // encrypted conversation (under the vault key)
  const KEEPKEY_KEY = "mnm.key.v1";      // remembered key for "keep me signed in"

  // In-memory session (cleared on lock). Secrets/keys never persisted unless
  // "keep me signed in" is on; the conversation is persisted encrypted.
  let session = null; // { secrets, cryptoKey, pin, vaultSalt, gh, model, repo, messages, transcript }
  let currentRun = null; // { stop }
  let stopFlag = false;  // shared stop signal (main turn + sub-agents)
  let composing = false; // true while building a message (may call the vision model)

  // ---------------------------------------------------------------- screens
  const SCREENS = ["screen-setup", "screen-unlock", "screen-repo", "screen-chat"];
  function show(id) {
    for (const s of SCREENS) $(s).hidden = s !== id;
    if (id === "screen-chat") requestAnimationFrame(fitMessages);
  }
  // Pad the scroll area so content clears the (overlaid) top bar and bottom dock,
  // which vary with the safe areas, the growing textarea, and attachment chips.
  function fitMessages() {
    const bar = document.querySelector("#screen-chat .bar");
    const dock = $("composer-dock");
    const msgs = $("messages");
    if (!bar || !dock || !msgs || $("screen-chat").hidden) return;
    const near = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;
    msgs.style.paddingTop = (bar.offsetHeight + 6) + "px";
    msgs.style.paddingBottom = (dock.offsetHeight + 6) + "px";
    if (near) msgs.scrollTop = msgs.scrollHeight;
  }
  window.addEventListener("resize", fitMessages);
  window.addEventListener("orientationchange", () => setTimeout(fitMessages, 200));

  // ------------------------------------------------------------- vault I/O
  function loadVault() {
    try { return JSON.parse(localStorage.getItem(VAULT_KEY) || "null"); }
    catch { return null; }
  }
  function storeVault(blob) { localStorage.setItem(VAULT_KEY, JSON.stringify(blob)); }
  function clearVault() { localStorage.removeItem(VAULT_KEY); }

  // ------------------------------------------------------------- auto-lock
  // Auto-lock is time-based and configurable (0 = never). We deliberately do
  // NOT lock the moment the app is backgrounded — a quick trip to the Home
  // Screen shouldn't kick you out. We save the session on hide (in case iOS
  // discards the page) and, on return, lock only if we've been idle too long.
  let idleTimer = null;
  let lastActive = Date.now();
  function autolockMs() {
    const m = parseInt(pref("mnm.autolock", "15"), 10);   // minutes; 0/NaN = never
    return (isNaN(m) || m <= 0) ? 0 : m * 60000;
  }
  function armIdle() {
    lastActive = Date.now();
    clearTimeout(idleTimer);
    const ms = autolockMs();
    if (session && ms) idleTimer = setTimeout(lock, ms);
  }
  function lock() {
    if (currentRun) { try { currentRun.stop(); } catch {} currentRun = null; }
    session = null;
    clearTimeout(idleTimer);
    $("in-unlock-pin").value = "";
    show("screen-unlock");
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { if (session) persistSession(); return; }
    const ms = autolockMs();
    if (session && ms && Date.now() - lastActive > ms) lock(); else armIdle();
  });
  ["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, armIdle, { passive: true }));

  // ---------------------------------------------------------------- toast
  let toastTimer = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  // Build a model client that shows a status while the free model is rate-limited.
  function onModelRetry(attempt, waitMs) {
    setStatus("model busy — retrying in " + Math.round(waitMs / 1000) + "s… (" + attempt + "/3)");
  }
  function newModel(modelName) {
    return AC.makeModel({ apiKey: session.secrets.modelKey, model: modelName, baseUrl: session.secrets.baseUrl, onRetry: onModelRetry });
  }

  // ================================================================ SETUP
  $("btn-save-setup").addEventListener("click", async () => {
    const err = $("setup-error"); err.textContent = "";
    const modelKey = $("in-model-key").value.trim();
    const model = $("in-model").value.trim() || "glm-4.7-flash";
    const baseUrl = $("in-base-url").value;
    const githubToken = $("in-gh-token").value.trim();
    const pin = $("in-pin").value, pin2 = $("in-pin2").value;
    if (!modelKey || !githubToken) return (err.textContent = "Model key and GitHub token are both required.");
    if (pin.length < 4) return (err.textContent = "PIN must be at least 4 characters.");
    if (pin !== pin2) return (err.textContent = "PINs don't match.");
    try {
      const secrets = { modelKey, model, baseUrl, githubToken };
      const blob = await AC.encryptVault(secrets, pin);
      storeVault(blob);
      clearSession();  // a fresh setup starts a fresh session
      const salt = AC._b64.b64ToBytes(blob.salt);
      const key = await AC.deriveKey(pin, salt, keepSignedIn());
      await finishUnlock(secrets, key, pin, salt);
    } catch (e) { err.textContent = e.message || String(e); }
  });

  // ================================================================ UNLOCK
  $("btn-unlock").addEventListener("click", doUnlock);
  $("in-unlock-pin").addEventListener("keydown", (e) => { if (e.key === "Enter") doUnlock(); });
  async function doUnlock() {
    const err = $("unlock-error"); err.textContent = "";
    const pin = $("in-unlock-pin").value;
    const blob = loadVault();
    if (!blob) return show("screen-setup");
    try {
      const secrets = await AC.decryptVault(blob, pin);   // verifies the PIN
      const salt = AC._b64.b64ToBytes(blob.salt);
      const key = await AC.deriveKey(pin, salt, keepSignedIn());
      await finishUnlock(secrets, key, pin, salt);
    } catch (e) { err.textContent = "Wrong PIN."; }
  }
  $("btn-reset").addEventListener("click", () => {
    if (confirm("Erase your encrypted keys and saved session from this device? You'll re-enter your keys.")) {
      clearVault(); clearSession(); localStorage.removeItem(KEEPKEY_KEY); session = null; show("screen-setup");
    }
  });

  // Finish unlocking: cache the key, honour "keep me signed in", and resume the
  // saved conversation if there is one (otherwise go to the repo picker).
  async function finishUnlock(secrets, key, pin, salt) {
    session = { secrets, cryptoKey: key, pin: pin || null, vaultSalt: salt || null };
    session.model = newModel(getModelName());
    armIdle();
    if (keepSignedIn()) { try { localStorage.setItem(KEEPKEY_KEY, await AC.exportRawKey(key)); } catch {} }
    else localStorage.removeItem(KEEPKEY_KEY);
    if (!(await tryRestoreSession())) await enterRepoPicker();
  }

  // ------------------------------------------------------- session persistence
  function keepSignedIn() { return pref("mnm.keepsignedin", "0") === "1"; }
  function clearSession() { localStorage.removeItem(SESSION_KEY); }
  // Drop bulky image data URLs from saved history (keep the flow, not the bytes).
  function stripImages(messages) {
    return messages.map((m) => Array.isArray(m.content)
      ? Object.assign({}, m, { content: m.content.map((c) => c.type === "image_url" ? { type: "text", text: "[image omitted from saved history]" } : c) })
      : m);
  }
  async function persistSession() {
    if (!session || !session.repo || !session.cryptoKey) return;
    try {
      const blob = await AC.aesEncrypt({
        repo: session.repo, baseSystem: session.baseSystem,
        messages: stripImages(session.messages || []), transcript: session.transcript || [],
      }, session.cryptoKey);
      localStorage.setItem(SESSION_KEY, JSON.stringify(blob));
    } catch (e) { /* quota / crypto — skip silently */ }
  }
  async function tryRestoreSession() {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return false;
    let data;
    try { data = await AC.aesDecrypt(JSON.parse(raw), session.cryptoKey); }
    catch { return false; }
    if (!data || !data.repo || !Array.isArray(data.messages)) return false;
    const r = data.repo;
    connectRepo(r.owner, r.repo, r.branch, r.full_name);
    if (data.baseSystem) session.baseSystem = data.baseSystem;
    session.messages = data.messages;
    session.transcript = data.transcript || [];
    $("chat-repo-name").textContent = r.full_name;
    $("messages").innerHTML = "";
    for (const b of session.transcript) addBubble(b.role, b.text, false);
    addBubble("system", "Resumed your session in " + r.full_name + ".", false);
    show("screen-chat");
    return true;
  }

  // ================================================================ REPO PICKER
  let repoCache = [];
  async function enterRepoPicker() {
    show("screen-repo");
    $("repo-error").textContent = "";
    $("repo-whoami").textContent = "Loading account…";
    const tmpGh = AC.makeGitHub({ token: session.secrets.githubToken, owner: "", repo: "" });
    try {
      const me = await tmpGh.me();
      $("repo-whoami").textContent = "Signed in as " + me.login;
      session.login = me.login;
    } catch (e) {
      $("repo-whoami").textContent = "";
      $("repo-error").textContent = "GitHub token rejected: " + friendlyGhError(e, "auth");
      return;
    }
    await refreshRepos();
  }
  async function refreshRepos() {
    const tmpGh = AC.makeGitHub({ token: session.secrets.githubToken, owner: "", repo: "" });
    try {
      repoCache = await tmpGh.listRepos();
      renderRepos();
    } catch (e) { $("repo-error").textContent = friendlyGhError(e, "list"); }
  }
  function renderRepos() {
    const filter = $("in-repo-filter").value.toLowerCase();
    const ul = $("repo-list"); ul.innerHTML = "";
    for (const r of repoCache.filter((r) => r.full_name.toLowerCase().includes(filter))) {
      const li = document.createElement("li");
      li.textContent = r.full_name;
      li.addEventListener("click", () => openRepo(r.full_name, r.default_branch || "main"));
      ul.appendChild(li);
    }
    if (!ul.children.length) ul.innerHTML = "<li class='muted'>no matching repos</li>";
  }
  $("in-repo-filter").addEventListener("input", renderRepos);
  $("btn-repo-refresh").addEventListener("click", refreshRepos);
  $("btn-repo-lock").addEventListener("click", lock);
  $("btn-create-repo").addEventListener("click", async () => {
    const name = $("in-new-repo").value.trim();
    if (!name) return;
    $("repo-error").textContent = "";
    const tmpGh = AC.makeGitHub({ token: session.secrets.githubToken, owner: "", repo: "" });
    try {
      const created = await tmpGh.createRepo(name, $("in-new-private").checked);
      openRepo(created.full_name, created.default_branch || "main");
    } catch (e) { $("repo-error").textContent = friendlyGhError(e, "create"); }
  });

  // Turn raw GitHub API errors into something actionable on a phone.
  function friendlyGhError(e, action) {
    const m = (e && e.message) || String(e);
    if (/not accessible by personal access token|Resource not accessible/i.test(m)) {
      if (action === "create") {
        return "Your token isn't allowed to create repos. In its GitHub settings give it " +
          "Repository access: All repositories, and Permissions → Administration: Read and write " +
          "(keep Contents: Read and write). Or create the repo on GitHub and open it from the list above.";
      }
      return "Your token doesn't have permission for that. Check its repository access and permissions in GitHub settings.";
    }
    if (/^GitHub 401/.test(m)) return "GitHub rejected the token (401). It may be expired — create a new fine-grained token.";
    if (/^GitHub 404/.test(m)) return "Not found (404). The token may not have access to that repository.";
    return m;
  }

  // Wire up the GitHub client + tools for a repo (shared by open and resume).
  function connectRepo(owner, repo, branch, fullName) {
    session.repo = { owner, repo, branch, full_name: fullName };
    session.gh = AC.makeGitHub({ token: session.secrets.githubToken, owner, repo, branch });
    session.baseSystem = AC.SYSTEM_PROMPT + "\n\nRepository: " + fullName + " (branch " + branch + ").";
    session.turnCommits = 0;
    session.images = {};   // name -> data URL, for view_image
    session.transcript = [];
    const onCommit = (p) => { session.turnCommits = (session.turnCommits || 0) + 1; toast("committed " + p); };
    // Sub-agent tools have NO spawn (depth 1); the main tools add spawn_agent.
    session.subTools = AC.makeTools(session.gh, { confirmWrite, onCommit, viewImage });
    session.tools = AC.makeTools(session.gh, { confirmWrite, onCommit, spawn: runSubAgent, viewImage });
    session.readTools = {};
    for (const n of READ_TOOL_NAMES) session.readTools[n] = session.tools[n];
    session.readTools.view_image = session.tools.view_image;   // let planning look at images too
    clearAttachments();
  }

  function openRepo(fullName, branch) {
    const [owner, repo] = fullName.split("/");
    connectRepo(owner, repo, branch, fullName);
    session.messages = [{ role: "system", content: session.baseSystem }];
    $("chat-repo-name").textContent = fullName;
    $("messages").innerHTML = "";
    addBubble("system", "Connected to " + fullName + ". I can read, search, and edit files here — each edit is committed. I can't run code on the phone; that happens when your desktop syncs or via CI.");
    show("screen-chat");
    persistSession();
  }
  $("btn-back-repo").addEventListener("click", () => { if (!currentRun) enterRepoPicker(); });
  $("btn-chat-lock").addEventListener("click", lock);

  // ================================================================ CONFIRM DIALOG
  function confirmWrite(kind, path, content) {
    if (!confirmCommits()) return Promise.resolve(true);
    return new Promise((resolve) => {
      $("confirm-title").textContent = (kind === "edit" ? "Commit edit?" : "Commit new file?");
      $("confirm-path").textContent = path;
      $("confirm-preview").textContent = String(content).slice(0, 4000);
      $("confirm-backdrop").hidden = false;
      const done = (val) => {
        $("confirm-backdrop").hidden = true;
        $("btn-confirm-yes").onclick = null; $("btn-confirm-no").onclick = null;
        resolve(val);
      };
      $("btn-confirm-yes").onclick = () => done(true);
      $("btn-confirm-no").onclick = () => done(false);
    });
  }

  // ================================================================ CHAT
  const composer = $("composer");
  const prompt = $("in-prompt");
  prompt.addEventListener("input", () => {
    prompt.style.height = "auto";
    prompt.style.height = Math.min(prompt.scrollHeight, 160) + "px";
    fitMessages();
  });
  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); }
  });
  composer.addEventListener("submit", (e) => { e.preventDefault(); sendPrompt(); });
  $("btn-stop").addEventListener("click", () => { if (currentRun) currentRun.stop(); });

  // ---------------------------------------------------------- attachments
  // Each item is one of:
  //   { kind:"repo", path }            — a file already in the GitHub repo
  //   { kind:"text", name, text }      — a text/code file uploaded from the phone
  //   { kind:"image", name, dataUrl }  — an image uploaded from the phone (vision)
  let attachments = [];
  function clearAttachments() { attachments = []; renderChips(); }
  function attLabel(a) { return a.path ? a.path.split("/").pop() : a.name; }
  function renderChips() {
    const box = $("attach-chips");
    box.innerHTML = "";
    box.hidden = attachments.length === 0;
    attachments.forEach((a, i) => {
      const chip = document.createElement("div");
      chip.className = "chip";
      const label = document.createElement("span");
      label.textContent = (a.kind === "image" ? "🖼 " : "") + attLabel(a);
      const x = document.createElement("button");
      x.type = "button"; x.textContent = "✕";
      x.onclick = () => { attachments.splice(i, 1); renderChips(); };
      chip.append(label, x);
      box.appendChild(chip);
    });
    fitMessages();
  }
  function attachmentNote() {
    return attachments.length ? "\n\n📎 " + attachments.map(attLabel).join(", ") : "";
  }
  function isVisionModel(m) { return /v-flash|vision|4\.\dv/i.test(m || ""); }

  // Build the message: text/code files are prepended as context; images become
  // an OpenAI-style multimodal content array (for a vision model to see).
  async function composeMessage(text) {
    if (!attachments.length) return text;
    const parts = [], images = [];
    for (const a of attachments) {
      if (a.kind === "repo") {
        try { const f = await session.gh.getFile(a.path); parts.push("=== " + a.path + " ===\n" + f.text); }
        catch { parts.push("=== " + a.path + " (couldn't read) ==="); }
      } else if (a.kind === "text") {
        parts.push("=== " + a.name + " ===\n" + a.text);
      } else if (a.kind === "image") {
        images.push(a);
        session.images = session.images || {};
        session.images[a.name] = a.dataUrl;   // make it viewable via view_image
      }
    }
    const ctx = parts.length ? "Attached files for context:\n\n" + parts.join("\n\n") + "\n\n---\n\n" : "";
    const body = ctx + (text || (images.length ? "" : "(see attached files)"));
    if (!images.length) return body;
    // A vision model sees the images directly.
    if (isVisionModel(getModelName())) {
      return [{ type: "text", text: body || "(describe the attached image)" }]
        .concat(images.map((im) => ({ type: "image_url", image_url: { url: im.dataUrl } })));
    }
    // Text/coding model: describe each uploaded image NOW via the free vision
    // model and inject the writeup, so the model gets the content directly and
    // never mistakes the upload for a file in the repo.
    const blocks = [];
    for (const im of images) {
      const d = await viewImage(im.name, text || "");
      if (/^(Couldn't analyze|No attached image)/.test(d)) addBubble("error", d);
      blocks.push('The user uploaded an image "' + im.name + '" (it is NOT a file in the repo — do not ' +
        'look for it with read_file/glob). Here is what it shows, described by the vision model:\n' + d);
    }
    setStatus("");
    return blocks.join("\n\n---\n\n") + "\n\n===\n\n" + (body || "(Act on the uploaded image described above.)");
  }

  $("btn-attach").addEventListener("click", openFilePicker);
  $("filepick-done").addEventListener("click", () => { $("filepick-backdrop").hidden = true; });
  $("filepick-backdrop").addEventListener("click", (e) => { if (e.target === $("filepick-backdrop")) $("filepick-backdrop").hidden = true; });
  $("filepick-search").addEventListener("input", renderFileList);
  $("filepick-upload").addEventListener("click", () => $("filepick-input").click());
  $("filepick-input").addEventListener("change", () => handleUploads($("filepick-input")));

  let fileTree = [];
  async function openFilePicker() {
    if (!session || !session.gh) return;
    $("filepick-search").value = "";
    $("filepick-list").innerHTML = "<div class='muted' style='padding:10px'>Loading…</div>";
    $("filepick-backdrop").hidden = false;
    try { fileTree = (await session.gh.tree()).map((e) => e.path); }
    catch (e) { $("filepick-list").innerHTML = ""; toast(friendlyGhError(e, "list")); return; }
    renderFileList();
  }
  function renderFileList() {
    const q = $("filepick-search").value.toLowerCase();
    const list = $("filepick-list");
    list.innerHTML = "";
    const matches = fileTree.filter((p) => p.toLowerCase().includes(q)).slice(0, 200);
    if (!matches.length) { list.innerHTML = "<div class='muted' style='padding:10px'>no files</div>"; return; }
    for (const p of matches) {
      const item = document.createElement("div");
      const picked = attachments.some((a) => a.kind === "repo" && a.path === p);
      item.className = "fp-item" + (picked ? " picked" : "");
      item.textContent = p;
      item.onclick = () => {
        const idx = attachments.findIndex((a) => a.kind === "repo" && a.path === p);
        if (idx >= 0) attachments.splice(idx, 1); else attachments.push({ kind: "repo", path: p });
        item.classList.toggle("picked");
        renderChips();
      };
      list.appendChild(item);
    }
  }

  // Local uploads from the phone: images are downscaled; other files read as text.
  async function handleUploads(input) {
    const files = [...(input.files || [])];
    input.value = "";
    for (const f of files) {
      try {
        if (f.type.startsWith("image/")) {
          attachments.push({ kind: "image", name: f.name, dataUrl: await downscaleImage(f, 1024) });
        } else {
          const text = await f.text();
          attachments.push({ kind: "text", name: f.name, text: text.slice(0, 100000) });
        }
      } catch { toast("Couldn't read " + f.name); }
    }
    renderChips();
    $("filepick-backdrop").hidden = true;
  }
  function downscaleImage(file, max) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const img = new Image();
        img.onload = () => {
          let w = img.width, h = img.height;
          const s = Math.min(1, max / Math.max(w, h));
          w = Math.round(w * s); h = Math.round(h * s);
          const c = document.createElement("canvas"); c.width = w; c.height = h;
          c.getContext("2d").drawImage(img, 0, 0, w, h);
          try { resolve(c.toDataURL("image/jpeg", 0.85)); } catch { resolve(reader.result); }
        };
        img.onerror = reject; img.src = reader.result;
      };
      reader.onerror = reject; reader.readAsDataURL(file);
    });
  }

  function runTurn(shouldStop, tools, toolSchemas) {
    let liveTool = null;
    return AC.runAgent({
      model: session.model,
      tools: tools || session.tools,
      messages: session.messages,
      shouldStop,
      toolSchemas,
      onEvent: (ev) => {
        armIdle();
        if (ev.type === "thinking") setStatus("thinking…");
        else if (ev.type === "tool") { liveTool = addTool(ev.name, ev.args); setStatus(ev.name + "…"); }
        else if (ev.type === "tool_result") { if (liveTool) finishTool(liveTool, ev.out); }
        else if (ev.type === "answer") { setStatus(""); if (ev.text) addBubble("assistant", ev.text); }
        else if (ev.type === "error") { setStatus(""); addBubble("error", ev.text); }
        else if (ev.type === "stopped") { setStatus(""); addBubble("system", "Stopped."); }
      },
    });
  }

  const VISION_MODEL = "glm-4.6v-flash";  // free vision model, used by view_image

  // Add view_image when the user has attached images and the chat model can't
  // see them itself (a text/coding model). A vision model sees them directly.
  function visionSchemas(base) {
    const has = session.images && Object.keys(session.images).length;
    return (has && !isVisionModel(getModelName())) ? base.concat([AC.VIEW_IMAGE_SCHEMA]) : base;
  }
  // Advertise spawn_agent on the main turn only when sub-agents are enabled.
  function mainSchemas() {
    const base = subagentsOn() ? AC.TOOL_SCHEMAS.concat([AC.SPAWN_SCHEMA]) : AC.TOOL_SCHEMAS;
    return visionSchemas(base);
  }

  // The view_image tool: send an attached image to the free vision model and
  // return its written description, so a text model can act on the image.
  async function viewImage(name, question) {
    const imgs = session.images || {};
    const keys = Object.keys(imgs);
    let url = imgs[name];
    if (!url) {
      const hit = keys.find((k) => k === name || k.endsWith(name) || name.endsWith(k) || k.includes(name));
      url = hit ? imgs[hit] : (keys.length === 1 ? imgs[keys[0]] : null);
    }
    if (!url) return "No attached image matches '" + name + "'. Available: " + (keys.join(", ") || "none");
    if (!session.visionModel) {
      session.visionModel = newModel(VISION_MODEL);
    }
    const focus = (question && question.trim()) ? "Focus on: " + question.trim() : "Describe the image in detail.";
    setStatus("looking with " + VISION_MODEL + "…");
    try {
      const resp = await session.visionModel.chat(
        [{ role: "user", content: [{ type: "text", text: "You are a vision assistant. " + focus }, { type: "image_url", image_url: { url } }] }],
        undefined);
      return ((resp && resp.content) || "").trim() || "(the vision model returned no description)";
    } catch (e) {
      return "Couldn't analyze the image: " + (e && e.message ? e.message : e);
    }
  }

  // A normal build turn, plus one Max self-review pass when it changed files.
  async function runBuild(getStopped) {
    session.messages[0].content = session.baseSystem + thinkingDirective(getThinking());
    await runTurn(getStopped, session.tools, mainSchemas());
    if (!getStopped() && getThinking() === "max" && session.turnCommits > 0) {
      setStatus("reviewing…");
      session.messages.push({ role: "user", content: REVIEW_NUDGE });
      await runTurn(getStopped, session.tools, mainSchemas());
    }
  }

  // A delegated sub-agent: its own history + tools (no spawn), reported inline.
  async function runSubAgent(task, context) {
    addBubble("system", "🧬 Sub-agent: " + task);
    const messages = [
      { role: "system", content: AC.SUBAGENT_PROMPT + "\n\nRepository: " + session.repo.full_name + " (branch " + session.repo.branch + ")." },
      { role: "user", content: task + (context ? "\n\nContext: " + context : "") },
    ];
    let liveTool = null, report = "";
    await AC.runAgent({
      model: session.model, tools: session.subTools, messages,
      toolSchemas: visionSchemas(AC.TOOL_SCHEMAS), maxSteps: 16, shouldStop: () => stopFlag,
      onEvent: (ev) => {
        armIdle();
        if (ev.type === "tool") { liveTool = addTool("↳ " + ev.name, ev.args); setStatus("sub · " + ev.name + "…"); }
        else if (ev.type === "tool_result") { if (liveTool) finishTool(liveTool, ev.out); }
        else if (ev.type === "answer") { report = ev.text || ""; }
        else if (ev.type === "error") { report = "Sub-agent error: " + ev.text; }
      },
    });
    if (report) addBubble("assistant", "🧬 " + report);
    return report || "(the sub-agent finished without a report)";
  }

  // Wrap a run: manage the running state, stop control, and errors.
  async function withRun(fn) {
    if (currentRun) return;
    stopFlag = false;
    currentRun = { stop: () => { stopFlag = true; $("btn-stop").disabled = true; } };
    setRunning(true);
    try { await fn(() => stopFlag); }
    catch (e) { addBubble("error", e.message || String(e)); }
    finally { setRunning(false); currentRun = null; persistSession(); }
  }

  async function sendPrompt() {
    if (currentRun || composing) return;
    const text = prompt.value.trim();
    if (!text && !attachments.length) return;
    prompt.value = ""; prompt.style.height = "auto"; fitMessages();
    addBubble("user", (text || "(attached files)") + attachmentNote());
    // composeMessage may call the vision model (to describe uploaded images), so
    // guard against a second send and disable the composer while it runs.
    composing = true; setRunning(true);
    let content;
    try { content = await composeMessage(text); }
    finally { composing = false; setRunning(false); }
    clearAttachments();
    session.turnCommits = 0;
    session.messages.push({ role: "user", content });

    await withRun(async (getStopped) => {
      if (planMode()) {
        session.messages[0].content = session.baseSystem + PLAN_DIRECTIVE;
        await runTurn(getStopped, session.readTools, READ_SCHEMAS);
        if (!getStopped()) showApproveBar();
      } else {
        await runBuild(getStopped);
      }
    });
  }

  function showApproveBar() {
    const bar = document.createElement("div");
    bar.className = "approve-bar";
    const discard = document.createElement("button");
    discard.className = "ghost"; discard.textContent = "Discard";
    const build = document.createElement("button");
    build.className = "primary"; build.textContent = "Approve & build";
    discard.onclick = () => { bar.remove(); addBubble("system", "Plan discarded."); };
    build.onclick = () => { bar.remove(); executePlan(); };
    bar.append(discard, build);
    messages.appendChild(bar);
    if (atBottom()) scroll();
  }

  async function executePlan() {
    addBubble("user", "Approved — build it.");
    session.turnCommits = 0;
    session.messages.push({ role: "user", content: "Approved. Implement that plan now: make the edits and commit them." });
    await withRun((getStopped) => runBuild(getStopped));
  }

  function setRunning(on) {
    $("btn-send").hidden = on;
    $("btn-stop").hidden = !on;
    $("btn-stop").disabled = false;
    $("btn-attach").disabled = on;
    prompt.disabled = on;
  }

  // ------------------------------------------------------------- rendering
  const messages = $("messages");
  function atBottom() { return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80; }
  function scroll() { messages.scrollTop = messages.scrollHeight; }
  function addBubble(role, text, record) {
    const near = atBottom();
    const div = document.createElement("div");
    div.className = "bubble " + role;
    renderText(div, text);
    messages.appendChild(div);
    if (near) scroll();
    // Record durable bubbles so the conversation can be re-rendered on resume.
    // (record defaults to true; the transcript replay passes false.)
    if (record !== false && session && (role === "user" || role === "assistant" || role === "system")) {
      session.transcript = session.transcript || [];
      session.transcript.push({ role, text });
    }
    return div;
  }
  // Minimal, safe markdown-ish rendering. Everything goes through textContent /
  // createTextNode — no innerHTML with model output, so no HTML/script injection.
  function renderText(container, text) {
    const parts = String(text).split(/```/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        const pre = document.createElement("pre");
        pre.className = "code";
        const nl = part.indexOf("\n");
        pre.textContent = nl >= 0 ? part.slice(nl + 1) : part;
        container.appendChild(pre);
      } else if (part) {
        const p = document.createElement("div");
        p.className = "para";
        p.textContent = part;
        container.appendChild(p);
      }
    });
  }
  function addTool(name, args) {
    const near = atBottom();
    const div = document.createElement("div");
    div.className = "tool-line running";
    const head = document.createElement("div");
    head.className = "tool-head";
    head.textContent = "⚙ " + name + argSummary(name, args);
    div.appendChild(head);
    messages.appendChild(div);
    if (near) scroll();
    return div;
  }
  function finishTool(div, out) {
    div.classList.remove("running");
    const body = document.createElement("pre");
    body.className = "tool-out";
    body.textContent = String(out).slice(0, 1500);
    div.appendChild(body);
    div.querySelector(".tool-head").addEventListener("click", () => div.classList.toggle("open"));
    if (atBottom()) scroll();
  }
  function argSummary(name, a) {
    if (!a) return "";
    if (a.path) return " · " + a.path;
    if (a.pattern) return " · " + a.pattern;
    if (a.query) return " · " + a.query;
    return "";
  }
  let statusBubble = null;
  function setStatus(text) {
    if (!text) { if (statusBubble) { statusBubble.remove(); statusBubble = null; } return; }
    if (!statusBubble) {
      statusBubble = document.createElement("div");
      statusBubble.className = "status";
      messages.appendChild(statusBubble);
    }
    statusBubble.textContent = text;
    if (atBottom()) scroll();
  }

  // ================================================================ BACKGROUND
  // The background choice is a cosmetic preference, not a secret, so it lives in
  // plain localStorage (unencrypted) and is applied at boot regardless of lock
  // state. Uploaded images are downscaled and stored as a data URL on-device.
  const BG_KEY = "mnm.bg.v1";
  const BG_PRESETS = [
    { label: "Default", type: "default", css: "#0b0d10" },
    { label: "Midnight", type: "color", value: "linear-gradient(160deg,#0d1526,#0b0d10 70%)" },
    { label: "Plum", type: "color", value: "linear-gradient(160deg,#1c1030,#0b0d10 70%)" },
    { label: "Pine", type: "color", value: "linear-gradient(160deg,#052622,#0b0d10 70%)" },
    { label: "Ember", type: "color", value: "linear-gradient(160deg,#2a1206,#0b0d10 70%)" },
    { label: "Nebula", type: "color", value: "radial-gradient(120% 90% at 28% 12%,#26407a,#0b0d10 60%)" },
  ];
  function loadBg() { try { return JSON.parse(localStorage.getItem(BG_KEY) || "null"); } catch { return null; } }
  function saveBg(bg) {
    try { if (bg) localStorage.setItem(BG_KEY, JSON.stringify(bg)); else localStorage.removeItem(BG_KEY); return true; }
    catch { return false; }
  }
  // Relative luminance of a #rrggbb colour (0 = black … 1 = white).
  function hexLuminance(hex) {
    const m = /^#?([0-9a-fA-F]{6})$/.exec(String(hex || "").trim());
    if (!m) return null;
    const n = parseInt(m[1], 16);
    const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * lin(n >> 16 & 255) + 0.7152 * lin(n >> 8 & 255) + 0.0722 * lin(n & 255);
  }
  // Is the background light enough that we should switch to dark text?
  function bgIsLight(bg) {
    if (!bg || bg.type === "default") return false;      // default is dark
    if (bg.type === "image") return !!bg.light;          // sampled when chosen
    if (bg.type === "color") { const L = hexLuminance(bg.value); return L != null && L > 0.6; } // gradients (not hex) are dark presets
    return false;
  }
  function applyBg(bg) {
    const layer = $("bg-layer");
    const root = document.documentElement;   // also paint <html> so the bottom
                                             // safe-area strip matches (not #0b0d10)
    layer.classList.remove("image");
    document.body.classList.toggle("light-bg", bgIsLight(bg));
    const set = (el, css, img) => { el.style.background = css; el.style.backgroundImage = img || ""; };
    if (!bg || bg.type === "default") {
      document.body.classList.remove("has-bg");
      set(layer, ""); set(root, "");
      return;
    }
    document.body.classList.add("has-bg");
    if (bg.type === "image") {
      layer.classList.add("image");
      set(layer, "#0b0d10 center/cover no-repeat", 'url("' + bg.value + '")');
      set(root, "#0b0d10 center/cover no-repeat", 'url("' + bg.value + '")');
    } else {
      set(layer, bg.value); set(root, bg.value);
    }
  }
  function sameBg(a, b) {
    if (!a) return b.type === "default";
    if (a.type !== b.type) return false;
    return a.type === "default" || a.value === b.value;
  }
  function setBg(bg) {
    applyBg(bg);                       // always apply for this session
    if (!saveBg(bg) && bg) toast("Applied — but too large to remember next launch.");
    renderAllBgPickers();
  }
  function renderAllBgPickers() { ["setup-bg", "settings-bg"].forEach((id) => { const el = $(id); if (el) renderBgPicker(el); }); }
  function renderBgPicker(container) {
    const cur = loadBg();
    container.innerHTML = "";
    for (const p of BG_PRESETS) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "swatch"; b.title = p.label;
      b.style.background = p.type === "default" ? p.css : p.value;
      if (sameBg(cur, p)) b.classList.add("sel");
      b.addEventListener("click", () => setBg(p.type === "default" ? null : { type: "color", value: p.value }));
      container.appendChild(b);
    }
    // custom colour
    const color = document.createElement("label");
    color.className = "swatch color-pick" + (cur && cur.type === "color" && /^#/.test(cur.value) ? " sel" : "");
    color.title = "Custom colour";
    const ci = document.createElement("input");
    ci.type = "color"; ci.value = (cur && cur.type === "color" && /^#/.test(cur.value)) ? cur.value : "#0b0d10";
    ci.addEventListener("input", () => setBg({ type: "color", value: ci.value }));
    color.appendChild(ci); container.appendChild(color);
    // image upload
    const up = document.createElement("label");
    up.className = "swatch upload" + (cur && cur.type === "image" ? " sel" : "");
    up.title = "Upload an image"; up.textContent = "＋";
    const fi = document.createElement("input");
    fi.type = "file"; fi.accept = "image/*"; fi.hidden = true;
    fi.addEventListener("change", () => handleBgFile(fi));
    up.appendChild(fi); container.appendChild(up);
  }
  // Average luminance of a canvas (sampled tiny) → true if it's a light image.
  function canvasIsLight(canvas) {
    try {
      const s = document.createElement("canvas"); s.width = 16; s.height = 16;
      const sc = s.getContext("2d"); sc.drawImage(canvas, 0, 0, 16, 16);
      const d = sc.getImageData(0, 0, 16, 16).data;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4) sum += (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) / 255;
      return sum / (d.length / 4) > 0.6;
    } catch { return false; }
  }
  function handleBgFile(input) {
    const f = input.files && input.files[0];
    input.value = "";
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const max = 2560; // keep wallpaper sharp on high-DPI phones
        let w = img.width, h = img.height;
        const scale = Math.min(1, max / Math.max(w, h));
        w = Math.round(w * scale); h = Math.round(h * scale);
        const c = document.createElement("canvas"); c.width = w; c.height = h;
        c.getContext("2d").drawImage(img, 0, 0, w, h);
        let data; try { data = c.toDataURL("image/jpeg", 0.82); } catch { data = reader.result; }
        setBg({ type: "image", value: data, light: canvasIsLight(c) });
      };
      img.onerror = () => toast("Couldn't read that image.");
      img.src = reader.result;
    };
    reader.onerror = () => toast("Couldn't read that image.");
    reader.readAsDataURL(f);
  }

  // ================================================================ PREFERENCES
  // Non-secret settings live in plain localStorage. The model NAME isn't a
  // secret (the key is), so it can live here and override what setup stored.
  function pref(k, def) { const v = localStorage.getItem(k); return v === null ? def : v; }
  function getModelName() { return pref("mnm.model", "") || (session && session.secrets && session.secrets.model) || "glm-4.7-flash"; }
  function getThinking() { return pref("mnm.thinking", "medium"); }
  function confirmCommits() { return pref("mnm.confirm", "1") === "1"; }
  function planMode() { return pref("mnm.plan", "0") === "1"; }
  function subagentsOn() { return pref("mnm.subagents", "1") === "1"; }
  const READ_TOOL_NAMES = ["list_dir", "glob", "read_file", "grep", "search_code"];
  const READ_SCHEMAS = AC.TOOL_SCHEMAS.filter((s) => READ_TOOL_NAMES.includes(s.function.name));
  const PLAN_DIRECTIVE = "\n\nPLAN MODE: do NOT edit or commit anything. Use the read/search tools to " +
    "investigate, then reply with a short, concrete numbered plan of the exact changes you'd make " +
    "(which files, and what changes in each). Stop after the plan and wait for approval.";
  function buildModel() {
    session.model = newModel(getModelName());
  }
  function thinkingDirective(mode) {
    if (mode === "low") return "\n\nBe fast and direct: minimal deliberation, short answers.";
    if (mode === "high") return "\n\nThink carefully, step by step, before acting; after each edit re-read it to check correctness.";
    if (mode === "max") return "\n\nThink rigorously and be exhaustive: plan before acting, verify every change against the request, and prefer correctness over speed.";
    return ""; // medium
  }
  const REVIEW_NUDGE = "Now review the change(s) you just made with fresh eyes. If anything is incorrect, " +
    "incomplete, or doesn't match my request, fix it now. If it's all correct, reply APPROVED.";
  function setSegOn(seg, val) {
    for (const b of seg.querySelectorAll("button[data-v]")) b.classList.toggle("on", b.dataset.v === val);
  }

  // ================================================================ SETTINGS SHEET
  function openSettings() {
    renderBgPicker($("settings-bg"));
    $("set-model").value = getModelName();
    setSegOn($("set-thinking"), getThinking());
    $("set-plan").checked = planMode();
    $("set-subagents").checked = subagentsOn();
    $("set-confirm").checked = confirmCommits();
    $("set-autolock").value = String(parseInt(pref("mnm.autolock", "15"), 10) || 0);
    $("set-keepsignedin").checked = keepSignedIn();
    $("settings-backdrop").hidden = false;
  }
  function closeSettings() { $("settings-backdrop").hidden = true; }
  $("btn-repo-settings").addEventListener("click", openSettings);
  $("btn-chat-settings").addEventListener("click", openSettings);
  $("btn-settings-done").addEventListener("click", closeSettings);
  $("settings-backdrop").addEventListener("click", (e) => { if (e.target === $("settings-backdrop")) closeSettings(); });
  $("btn-settings-lock").addEventListener("click", () => { closeSettings(); lock(); });
  $("set-model").addEventListener("change", () => {
    localStorage.setItem("mnm.model", $("set-model").value.trim() || "glm-4.7-flash");
    if (session) buildModel();
  });
  $("set-thinking").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    localStorage.setItem("mnm.thinking", b.dataset.v);
    setSegOn($("set-thinking"), b.dataset.v);
  });
  $("set-plan").addEventListener("change", () => {
    localStorage.setItem("mnm.plan", $("set-plan").checked ? "1" : "0");
  });
  $("set-subagents").addEventListener("change", () => {
    localStorage.setItem("mnm.subagents", $("set-subagents").checked ? "1" : "0");
  });
  $("set-confirm").addEventListener("change", () => {
    localStorage.setItem("mnm.confirm", $("set-confirm").checked ? "1" : "0");
  });
  $("set-autolock").addEventListener("change", () => {
    localStorage.setItem("mnm.autolock", $("set-autolock").value);
    armIdle();
  });
  $("set-keepsignedin").addEventListener("change", async () => {
    const on = $("set-keepsignedin").checked;
    localStorage.setItem("mnm.keepsignedin", on ? "1" : "0");
    if (!on) { localStorage.removeItem(KEEPKEY_KEY); return; }
    // Turning it on: remember the key so future launches skip the PIN. Re-derive
    // an extractable key from the PIN we still hold if needed.
    try {
      let key = session && session.cryptoKey;
      if (session && session.pin && session.vaultSalt) key = await AC.deriveKey(session.pin, session.vaultSalt, true);
      if (key) { session.cryptoKey = key; localStorage.setItem(KEEPKEY_KEY, await AC.exportRawKey(key)); }
    } catch { toast("Couldn't enable — lock and unlock once, then try again."); }
  });

  function registerSW() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("sw.js").catch(() => {});
    // When a new SW takes control (a fresh deploy), reload once so the page runs
    // the new code. Guarded on an existing controller so a first install doesn't loop.
    if (navigator.serviceWorker.controller) {
      let refreshing = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (refreshing) return;
        refreshing = true;
        location.reload();
      });
    }
  }

  // ================================================================ BOOT
  async function boot() {
    applyBg(loadBg());
    renderBgPicker($("setup-bg"));
    registerSW();
    const blob = loadVault();
    const rawKey = localStorage.getItem(KEEPKEY_KEY);
    // "Keep me signed in": use the remembered key to unlock without the PIN.
    if (blob && rawKey) {
      try {
        const key = await AC.importRawKey(rawKey, true);
        const secrets = await AC.aesDecrypt(blob, key);
        await finishUnlock(secrets, key, null, AC._b64.b64ToBytes(blob.salt));
        return;
      } catch { localStorage.removeItem(KEEPKEY_KEY); }
    }
    if (blob) show("screen-unlock"); else show("screen-setup");
  }
  boot();
})();

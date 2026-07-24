/* Make No Mistakes — mobile browser glue.
 *
 * This is the only DOM-touching layer. All crypto, GitHub, model, and agent
 * logic lives in agent-core.js (AgentCore), which is unit-tested in Node.
 *
 * SECURITY posture enforced here:
 *  - Only the encrypted vault blob is persisted (localStorage). The PIN and the
 *    decrypted secrets live in JS memory (`session`) and only while unlocked.
 *  - The app auto-locks — dropping the decrypted secrets — when it goes to the
 *    background or after an idle timeout.
 *  - Every write is routed through a modal confirm dialog by default.
 */
(function () {
  "use strict";
  const AC = window.AgentCore;
  const $ = (id) => document.getElementById(id);
  const VAULT_KEY = "mnm.vault.v1";
  const IDLE_MS = 5 * 60 * 1000; // lock after 5 min idle

  // In-memory session (cleared on lock). Never persisted.
  let session = null; // { secrets, gh, model, repo:{owner,repo,branch,full_name} }
  let currentRun = null; // { stop }

  // ---------------------------------------------------------------- screens
  const SCREENS = ["screen-setup", "screen-unlock", "screen-repo", "screen-chat"];
  function show(id) {
    for (const s of SCREENS) $(s).hidden = s !== id;
  }

  // ------------------------------------------------------------- vault I/O
  function loadVault() {
    try { return JSON.parse(localStorage.getItem(VAULT_KEY) || "null"); }
    catch { return null; }
  }
  function storeVault(blob) { localStorage.setItem(VAULT_KEY, JSON.stringify(blob)); }
  function clearVault() { localStorage.removeItem(VAULT_KEY); }

  // ------------------------------------------------------------- auto-lock
  let idleTimer = null;
  function armIdle() {
    clearTimeout(idleTimer);
    if (session) idleTimer = setTimeout(lock, IDLE_MS);
  }
  function lock() {
    if (currentRun) { try { currentRun.stop(); } catch {} currentRun = null; }
    session = null;
    clearTimeout(idleTimer);
    $("in-unlock-pin").value = "";
    show("screen-unlock");
  }
  // Lock the moment we lose focus/visibility — a phone set down shouldn't stay open.
  document.addEventListener("visibilitychange", () => { if (document.hidden && session) lock(); });
  window.addEventListener("pagehide", () => { if (session) lock(); });
  ["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, armIdle, { passive: true }));

  // ---------------------------------------------------------------- toast
  let toastTimer = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  // ================================================================ SETUP
  $("btn-save-setup").addEventListener("click", async () => {
    const err = $("setup-error"); err.textContent = "";
    const modelKey = $("in-model-key").value.trim();
    const model = $("in-model").value.trim() || "glm-4.6";
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
      await unlockWith(secrets, pin);
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
      const secrets = await AC.decryptVault(blob, pin);
      await unlockWith(secrets, pin);
    } catch (e) { err.textContent = "Wrong PIN."; }
  }
  $("btn-reset").addEventListener("click", () => {
    if (confirm("Erase your encrypted keys from this device? You'll re-enter them.")) {
      clearVault(); session = null; show("screen-setup");
    }
  });

  async function unlockWith(secrets, pin) {
    session = { secrets, pin };
    session.model = AC.makeModel({ apiKey: secrets.modelKey, model: secrets.model, baseUrl: secrets.baseUrl });
    armIdle();
    await enterRepoPicker();
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

  function openRepo(fullName, branch) {
    const [owner, repo] = fullName.split("/");
    session.repo = { owner, repo, branch, full_name: fullName };
    session.gh = AC.makeGitHub({ token: session.secrets.githubToken, owner, repo, branch });
    session.messages = [{ role: "system", content: AC.SYSTEM_PROMPT + "\n\nRepository: " + fullName + " (branch " + branch + ")." }];
    session.tools = AC.makeTools(session.gh, {
      confirmWrite: confirmWrite,
      onCommit: (p) => toast("committed " + p),
    });
    $("chat-repo-name").textContent = fullName;
    $("messages").innerHTML = "";
    addBubble("system", "Connected to " + fullName + ". I can read, search, and edit files here — each edit is committed. I can't run code on the phone; that happens when your desktop syncs or via CI.");
    show("screen-chat");
  }
  $("btn-back-repo").addEventListener("click", () => { if (!currentRun) enterRepoPicker(); });
  $("btn-chat-lock").addEventListener("click", lock);

  // ================================================================ CONFIRM DIALOG
  function confirmWrite(kind, path, content) {
    if (!$("in-confirm-writes").checked) return Promise.resolve(true);
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
  });
  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); }
  });
  composer.addEventListener("submit", (e) => { e.preventDefault(); sendPrompt(); });
  $("btn-stop").addEventListener("click", () => { if (currentRun) currentRun.stop(); });

  async function sendPrompt() {
    if (currentRun) return;
    const text = prompt.value.trim();
    if (!text) return;
    prompt.value = ""; prompt.style.height = "auto";
    addBubble("user", text);
    session.messages.push({ role: "user", content: text });
    setRunning(true);

    let stopped = false;
    currentRun = { stop: () => { stopped = true; $("btn-stop").disabled = true; } };
    let liveTool = null;
    try {
      await AC.runAgent({
        model: session.model,
        tools: session.tools,
        messages: session.messages,
        shouldStop: () => stopped,
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
    } catch (e) {
      addBubble("error", e.message || String(e));
    } finally {
      setRunning(false);
      currentRun = null;
    }
  }

  function setRunning(on) {
    $("btn-send").hidden = on;
    $("btn-stop").hidden = !on;
    $("btn-stop").disabled = false;
    prompt.disabled = on;
  }

  // ------------------------------------------------------------- rendering
  const messages = $("messages");
  function atBottom() { return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80; }
  function scroll() { messages.scrollTop = messages.scrollHeight; }
  function addBubble(role, text) {
    const near = atBottom();
    const div = document.createElement("div");
    div.className = "bubble " + role;
    renderText(div, text);
    messages.appendChild(div);
    if (near) scroll();
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
  function applyBg(bg) {
    const layer = $("bg-layer");
    layer.classList.remove("image");
    if (!bg || bg.type === "default") { document.body.classList.remove("has-bg"); layer.style.background = ""; return; }
    document.body.classList.add("has-bg");
    if (bg.type === "image") { layer.classList.add("image"); layer.style.background = "#0b0d10 center/cover no-repeat"; layer.style.backgroundImage = 'url("' + bg.value + '")'; }
    else { layer.style.background = bg.value; }
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
        setBg({ type: "image", value: data });
      };
      img.onerror = () => toast("Couldn't read that image.");
      img.src = reader.result;
    };
    reader.onerror = () => toast("Couldn't read that image.");
    reader.readAsDataURL(f);
  }

  // ================================================================ SETTINGS SHEET
  function openSettings() { renderBgPicker($("settings-bg")); $("settings-backdrop").hidden = false; }
  function closeSettings() { $("settings-backdrop").hidden = true; }
  $("btn-repo-settings").addEventListener("click", openSettings);
  $("btn-chat-settings").addEventListener("click", openSettings);
  $("btn-settings-done").addEventListener("click", closeSettings);
  $("settings-backdrop").addEventListener("click", (e) => { if (e.target === $("settings-backdrop")) closeSettings(); });
  $("btn-settings-lock").addEventListener("click", () => { closeSettings(); lock(); });

  // ================================================================ BOOT
  function boot() {
    applyBg(loadBg());
    renderBgPicker($("setup-bg"));
    if (loadVault()) show("screen-unlock");
    else show("screen-setup");
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => {});
      // When a new SW takes control (a fresh deploy), reload once so the page
      // runs the new code instead of whatever the old SW already handed us.
      // Guarded on an existing controller so a first-ever install doesn't loop.
      if (navigator.serviceWorker.controller) {
        let refreshing = false;
        navigator.serviceWorker.addEventListener("controllerchange", () => {
          if (refreshing) return;
          refreshing = true;
          location.reload();
        });
      }
    }
  }
  boot();
})();

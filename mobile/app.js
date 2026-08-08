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
  const SYNCPASS_KEY = "mnm.syncpass.v1"; // sync passphrase, encrypted under the vault key
  const DEVICEID_KEY = "mnm.deviceid.v1"; // random id identifying this phone for cross-device locks
  const DEVICE_LABEL = "phone";

  // In-memory session (cleared on lock). Secrets/keys never persisted unless
  // "keep me signed in" is on; the conversation is persisted encrypted.
  let session = null; // { secrets, cryptoKey, pin, vaultSalt, gh, model, repo, messages, transcript }
  let currentRun = null; // { stop }
  let stopFlag = false;  // shared stop signal (main turn + sub-agents)
  let composing = false; // true while building a message (may call the vision model)

  // ---------------------------------------------------------------- screens
  const SCREENS = ["screen-setup", "screen-unlock", "screen-repo", "screen-chats", "screen-chat"];
  function show(id) {
    for (const s of SCREENS) $(s).hidden = s !== id;
    if (id === "screen-chat") requestAnimationFrame(fitMessages);
  }
  // Pad the scroll area so content clears the (overlaid) top bar and bottom dock,
  // which vary with the safe areas, the growing textarea, and attachment chips.
  /** How far the message list is from its own bottom, in px. */
  function distanceFromBottom() {
    const msgs = $("messages");
    return msgs ? msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight : 0;
  }

  // `wasNear` lets the caller decide from a measurement taken EARLIER. The
  // keyboard needs that: --kb grows this list's bottom padding by the height of
  // the keyboard, so by the time this runs, someone sitting at the very bottom
  // measures as ~344px away from it and the check below says "not near". The
  // result was the last message sliding out of view exactly when you tapped the
  // composer to reply to it.
  function fitMessages(wasNear) {
    const bar = document.querySelector("#screen-chat .bar");
    const dock = $("composer-dock");
    const msgs = $("messages");
    if (!bar || !dock || !msgs || $("screen-chat").hidden) return;
    const near = wasNear === undefined ? distanceFromBottom() < 80 : wasNear;
    msgs.style.paddingTop = (bar.offsetHeight + 6) + "px";
    // The bottom padding is NOT set here. It is a calc in the stylesheet over
    // --dock-h and --kb, so it cannot fall out of step with the keyboard on an
    // event this function does not run on — which is how a keyboard's worth of
    // chat came to sit under the composer.
    // --dock-h is published for it, and for the jump-to-latest pill: the dock
    // grows with the textarea and the attachment chips.
    $("screen-chat").style.setProperty("--dock-h", dock.offsetHeight + "px");
    if (near) msgs.scrollTop = msgs.scrollHeight;
  }
  window.addEventListener("resize", fitMessages);
  window.addEventListener("orientationchange", () => setTimeout(fitMessages, 200));

  // How much of the screen the on-screen keyboard covers, published as --kb so
  // the composer can lift itself over it.
  //
  // The document is locked (html/body are fixed and overflow:hidden), which is
  // what stops iOS shoving the whole UI — wallpaper and all — up off the
  // screen to reveal the focused field. The cost of locking it is that nothing
  // moves out of the keyboard's way on its own any more, so measure it here
  // instead. visualViewport is the only thing that reports this: on iOS the
  // layout viewport does not change when the keyboard opens, only the visual
  // one shrinks.
  // <html> is deliberately taller than the viewport (see style.css: it is what
  // lets the canvas background paint the strip of screen below the shifted
  // layout viewport in an installed iOS PWA). That height is scrollable, and
  // overflow:hidden on the root does NOT reliably stop it — Chromium still
  // honours a programmatic scroll, and iOS scrolls the document by itself to
  // reveal a focused field. Either way the whole UI slides up. So put it back.
  function pinDocument() {
    if (window.scrollY || document.documentElement.scrollTop) window.scrollTo(0, 0);
  }
  window.addEventListener("scroll", pinDocument, { passive: true });

  // Correcting the scroll is a frame too late — you see the lurch and the snap
  // back. So remove the thing being scrolled first: focusin fires before the
  // keyboard animates in, and html.kb-open drops the spare inset of height that
  // is the only scrollable slack there is. The listener above stays as a
  // backstop for anything that scrolls the document by another route.
  // WHAT ACTUALLY MOVES WHEN A FIELD IS FOCUSED.
  //
  // The flash on focus — the UI shoved up and snapped back — has now been
  // attributed twice to a mechanism that turned out not to be causing it: the
  // document scrolling, then the wallpaper's box being rescaled by kb-open.
  // Removing the second one entirely left the flash exactly as it was. Both
  // fixes shipped before anything measured which thing had moved.
  //
  // So measure. There are only three candidates, and they need different
  // fixes, so telling them apart is the whole job:
  //   scrollY — the document scrolled. A scroll listener can undo this.
  //   vv top  — iOS shifted the VISUAL viewport instead, which it does to
  //             reveal a focused field when the document cannot scroll. No
  //             scroll handler touches this one.
  //   html Δh — <html>'s own box changed size, which rescales anything painted
  //             into it.
  // Sample from focusin across the keyboard animation and keep the largest of
  // each. Reported in Settings next to the keyboard numbers.
  let focusShove = null;
  function watchFocusShove() {
    const vv = window.visualViewport;
    const t0 = performance.now();
    const startH = document.documentElement.getBoundingClientRect().height;
    const seen = { at: new Date().toLocaleTimeString(), scrollY: 0, vvTop: 0, htmlD: 0 };
    const tick = () => {
      seen.scrollY = Math.max(seen.scrollY,
        window.scrollY || document.documentElement.scrollTop || 0);
      if (vv) seen.vvTop = Math.max(seen.vvTop, vv.offsetTop || 0);
      seen.htmlD = Math.max(seen.htmlD,
        Math.abs(document.documentElement.getBoundingClientRect().height - startH));
      if (performance.now() - t0 < 700) requestAnimationFrame(tick);
      else focusShove = seen;
    };
    requestAnimationFrame(tick);
  }

  const TYPES = /^(input|textarea|select)$/i;
  document.addEventListener("focusin", (e) => {
    if (e.target && TYPES.test(e.target.tagName)) {
      watchFocusShove();
      document.documentElement.classList.add("kb-open");
      pinDocument();
    }
  });
  document.addEventListener("focusout", (e) => {
    if (!e.target || !TYPES.test(e.target.tagName)) return;
    // Only once nothing else has taken focus, or moving between two fields
    // would put the height back for a frame — the same flash, in miniature.
    setTimeout(() => {
      const el = document.activeElement;
      if (!el || !TYPES.test(el.tagName)) document.documentElement.classList.remove("kb-open");
    }, 0);
  });

  // The box a position:fixed element is actually laid out in. #app is
  // position:fixed;inset:0, so its rect IS that containing block. Reported in
  // diagnostics next to the named properties: if they ever disagree on a real
  // device, that difference is the answer, and no desktop browser will show it.
  function fixedBoxHeight() {
    const app = document.getElementById("app");
    const h = app ? app.getBoundingClientRect().height : 0;
    return h || window.innerHeight || document.documentElement.clientHeight || 0;
  }

  // Every number that describes the keyboard, read at this instant.
  function geometry() {
    const vv = window.visualViewport;
    const cs = getComputedStyle(document.documentElement);
    const dock = document.getElementById("composer-dock");
    const num = (v) => (v == null ? "?" : String(Math.round(v)));
    // Which field this recording is about. Without it a reading is ambiguous:
    // one came back with the dock's rect at 0/0 -- a zero rect means the
    // element was not rendered at that instant -- and there was no way to tell
    // whether the keyboard had been raised by the composer or by something in
    // a sheet on top of it. A number you cannot attribute answers nothing.
    const focused = document.activeElement;
    const who = !focused || focused === document.body
      ? "nothing"
      : (focused.id || focused.tagName.toLowerCase()) +
        (dock && dock.contains(focused) ? " (the composer)" : " (not the composer)");
    return [
      ["focused", who],
      ["fixed box h", num(fixedBoxHeight())],
      ["html h", num(document.documentElement.clientHeight)],
      ["inner h", num(window.innerHeight)],
      ["visual h", vv ? num(vv.height) : "no visualViewport"],
      ["visual top", vv ? num(vv.offsetTop) : "-"],
      ["--kb", cs.getPropertyValue("--kb").trim() || "0px"],
      ["--safe-t", cs.getPropertyValue("--safe-t").trim() || "0px"],
      ["--safe-b", cs.getPropertyValue("--safe-b").trim() || "0px"],
      ["dock bottom", num(dock && dock.getBoundingClientRect().bottom)],
      ["dock top", num(dock && dock.getBoundingClientRect().top)],
    ];
  }

  // ...and a copy of them from while the keyboard was actually up. A live
  // readout in Settings cannot answer this: opening Settings takes focus off
  // the composer, the keyboard drops, and every interesting value reverts
  // before it can be read. So record at the moment the keyboard covers the
  // screen and keep the last recording to read afterwards.
  let kbPeak = null;

  function trackKeyboard() {
    const vv = window.visualViewport;
    if (!vv) return;                       // --kb stays 0; layout is unchanged
    const apply = () => {
      // Measured BEFORE --kb changes anything. Opening the keyboard adds its
      // own height to the message list's bottom padding, which moves the
      // bottom away from you; asking "were you at the bottom?" afterwards
      // always answers no. See fitMessages.
      const wasNear = distanceFromBottom() < 80;
      // How much of the screen is hidden = (where the layout viewport ends)
      // minus (where the visible area ends). The dock is position:fixed, so it
      // is laid out against the LAYOUT viewport, and that is the reference this
      // subtraction needs.
      //
      // window.innerHeight alone was wrong: in an installed iOS PWA it tracks
      // the visual viewport, so it shrinks with the keyboard and the difference
      // comes out ~0 — the dock never lifted at all. That was hidden until now
      // because iOS was scrolling the whole document up to reveal the focused
      // field, which put the composer on screen by accident; removing that
      // shove is what exposed it.
      //
      // Taking the larger of the two references is right whichever one this
      // engine keeps stable, and needs no per-platform guess.
      //
      // This subtraction is the whole answer, and nothing needs adding to it.
      // A previous version added 44px on iOS for the system form accessory bar
      // (prev/next chevrons and Done) on the theory that visualViewport shrinks
      // by the keys alone. The device says otherwise: on an iPhone 15 Pro with
      // the keyboard up it reported a 852px screen, a 59px top inset and a
      // 449px visible viewport, leaving 344px hidden -- which is the keys AND
      // that bar together. Adding 44 counted the bar twice and parked the
      // composer in mid-air with a strip of chat showing underneath it.
      const layoutH = Math.max(document.documentElement.clientHeight || 0,
                               window.innerHeight || 0);
      let covered = Math.max(0, layoutH - vv.height - vv.offsetTop);
      // Ignore small deltas so a browser's collapsing address bar doesn't read
      // as a keyboard.
      if (covered <= 80) covered = 0;
      document.documentElement.style.setProperty("--kb", covered + "px");
      pinDocument();     // opening the keyboard is when iOS tries to scroll
      fitMessages(wasNear);
      // Read after the layout above, so the dock's rect reflects this --kb.
      if (covered > 0) {
        kbPeak = {
          at: new Date().toLocaleTimeString(),
          rows: geometry(),
        };
      }
    };
    vv.addEventListener("resize", apply);
    vv.addEventListener("scroll", apply);
    apply();
  }
  trackKeyboard();

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
  // A light heartbeat, so a chat open on both devices notices the other within
  // ~30s rather than only when you switch back to the app. One small index read
  // per tick, skipped while hidden or mid-turn, so it barely costs anything.
  const LIVE_POLL_MS = 30000;
  setInterval(() => {
    if (document.hidden || currentRun || composing) return;
    refreshOpenChatFromSync();
  }, LIVE_POLL_MS);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { if (session) persistSession(); return; }
    const ms = autolockMs();
    if (session && ms && Date.now() - lastActive > ms) { lock(); return; }
    armIdle();
    // Back in the foreground and still unlocked: see if the desktop moved on.
    if (session) refreshOpenChatFromSync();
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
  // saved conversation if there is one. Otherwise: sync users land on the chat
  // hub (every chat, every repo — the phone's equivalent of the desktop
  // sidebar); without sync there's no hub to show, so it's the repo picker.
  async function finishUnlock(secrets, key, pin, salt) {
    session = { secrets, cryptoKey: key, pin: pin || null, vaultSalt: salt || null };
    session.model = newModel(getModelName());
    armIdle();
    if (keepSignedIn()) { try { localStorage.setItem(KEEPKEY_KEY, await AC.exportRawKey(key)); } catch {} }
    else localStorage.removeItem(KEEPKEY_KEY);
    // A passphrase that came over from the desktop can only be stored now: it
    // is kept encrypted under the vault key, which didn't exist until the PIN
    // was set. Doing it here is what makes "install the app" and "share your
    // chats" one act instead of two.
    if (pendingSyncPass) {
      try {
        await storeSyncPass(pendingSyncPass);
        localStorage.setItem("mnm.sync", "1");
        toast("Your chats will sync with your computer.");
      } catch (e) { toast("Couldn't turn on sync — set the passphrase in Settings."); }
      pendingSyncPass = "";
    }
    if (await tryRestoreSession()) return;
    if (syncOn() && hasSyncPass()) await enterChatList();
    else await enterRepoPicker();
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
    // session.repo is still whatever was connected before a read-only chat was
    // opened, so the guard above doesn't catch this: persisting here would
    // write the read-only chat's empty message list over the restorable one.
    if (session.readOnly) return;
    try {
      const blob = await AC.aesEncrypt({
        repo: session.repo, baseSystem: session.baseSystem,
        chatId: session.chatId || null, chatTitle: session.chatTitle || "",
        messages: stripImages(session.messages || []), transcript: session.transcript || [],
        pending: session.pending || [],
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
    session.chatId = data.chatId || newChatId();
    session.chatTitle = data.chatTitle || "";
    session.messages = data.messages;
    session.transcript = data.transcript || [];
    session.pending = data.pending || [];
    $("chat-repo-name").textContent = r.full_name;
    $("messages").innerHTML = "";
    for (const b of session.transcript) addBubble(b.role, b.text, false);
    addBubble("system", "Resumed your session in " + r.full_name + ".", false);
    show("screen-chat");
    return true;
  }

  // ================================================================ REPO PICKER
  // Reached either as the first screen after unlock (sync off — nothing else to
  // show) or via "＋ New chat" from the hub (sync on — picking/creating a repo
  // here always starts a FRESH chat; existing ones are resumed from the hub).
  let repoCache = [];
  async function enterRepoPicker() {
    show("screen-repo");
    $("btn-repo-back").hidden = !(syncOn() && hasSyncPass());
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
  $("btn-repo-back").addEventListener("click", () => { if (!currentRun) enterChatList(); });
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
    session.compact = null;  // cached summary of trimmed turns, per conversation
    session.toldCompact = false;
    session.pending = [];    // work parked for the desktop, per conversation
    session.carry = {};      // nothing to carry: this chat starts here
    session.readOnly = false;
    applyReadOnlyChrome(false);
    session.transcript = [];
    const onCommit = (p) => { session.turnCommits = (session.turnCommits || 0) + 1; toast("committed " + p); haptic(18); };
    // Sub-agent tools have NO spawn (depth 1); the main tools add spawn_agent.
    session.subTools = AC.makeTools(session.gh, { confirmWrite, onCommit, viewImage, needsDesktop });
    session.tools = AC.makeTools(session.gh, { confirmWrite, onCommit, spawn: runSubAgent, viewImage, needsDesktop });
    session.readTools = {};
    for (const n of READ_TOOL_NAMES) session.readTools[n] = session.tools[n];
    session.readTools.view_image = session.tools.view_image;   // let planning look at images too
    clearAttachments();
  }

  // Picking (or creating) a repo always starts a FRESH chat — resuming an
  // existing one happens from the hub, never by re-picking its repo. The sync
  // store is central (one repo, independent of the project), so it stays
  // cached across this switch instead of being re-derived from GitHub again.
  async function openRepo(fullName, branch) {
    const [owner, repo] = fullName.split("/");
    connectRepo(owner, repo, branch, fullName);
    startNewChat();
  }
  // "Back" from an open chat: to the hub if sync can show one, else the repo
  // picker (sync off has no hub — that IS the top-level screen).
  $("btn-back-repo").addEventListener("click", () => {
    if (currentRun) return;
    if (syncOn() && hasSyncPass()) enterChatList(); else enterRepoPicker();
  });
  $("btn-chat-lock").addEventListener("click", lock);

  // Start a brand-new chat in the connected repo.
  function startNewChat() {
    session.chatId = newChatId();
    session.chatTitle = "";
    session.messages = [{ role: "system", content: session.baseSystem }];
    session.transcript = [];
    session.images = {};
    session.compact = null;
    session.toldCompact = false;
    session.pending = [];
    session.carry = {};      // a fresh chat owns all of its own fields
    session.readOnly = false;
    applyReadOnlyChrome(false);
    clearAttachments();
    $("chat-repo-name").textContent = session.repo.full_name;
    $("messages").innerHTML = "";
    addBubble("system", "Connected to " + session.repo.full_name + ". I can read, search, and edit files here — each edit is committed. I can't run code on the phone; that happens when your desktop syncs or via CI.");
    show("screen-chat");
    renderContextMeter();
    persistSession();
    // Don't sync an empty chat — the first real save happens after a turn, so
    // the history list never fills with blank "New chat" entries.
  }
  function newChatId() {
    try { if (crypto.randomUUID) return crypto.randomUUID(); } catch {}
    return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  // ================================================================ SESSION SYNC
  // Opt-in cross-device sync. Chats live encrypted on the repo's orphan state
  // branch (AgentCore.openSync / makeSyncStore), keyed by a SYNC PASSPHRASE that
  // is separate from the PIN. The passphrase is stored on-device only as
  // ciphertext under the vault key, so it survives launches (behind the PIN)
  // without ever being written in plain text or sent to GitHub.
  function syncOn() { return pref("mnm.sync", "0") === "1"; }
  function hasSyncPass() { return !!localStorage.getItem(SYNCPASS_KEY); }
  async function getSyncPass() {
    const raw = localStorage.getItem(SYNCPASS_KEY);
    if (!raw || !session || !session.cryptoKey) return null;
    try { return await AC.aesDecrypt(JSON.parse(raw), session.cryptoKey); }
    catch { return null; }
  }
  async function storeSyncPass(pass) {
    const blob = await AC.aesEncrypt(pass, session.cryptoKey);
    localStorage.setItem(SYNCPASS_KEY, JSON.stringify(blob));
  }
  // Open the ONE central sync store for a given passphrase. Independent of any
  // connected repo — every chat, from every project, lives here. The single
  // path both "unlock the store for use" and "verify a passphrase before
  // saving it" go through, so there's exactly one place that can get the
  // target repo wrong.
  async function openCentralSync(passphrase) {
    const api = AC.makeGitHub({ token: session.secrets.githubToken, owner: "", repo: "" });
    const { owner, repo } = await AC.ensureSyncRepo(api);
    const syncGh = AC.makeGitHub({ token: session.secrets.githubToken,
      owner, repo, branch: AC.SYNC_REPO_BRANCH });
    return AC.openSync(syncGh, passphrase);
  }
  // Lazily open (and cache) the store using the saved passphrase.
  async function ensureSyncStore() {
    if (!syncOn() || !session) return null;
    if (session.syncStore) return session.syncStore;
    const pass = await getSyncPass();
    if (!pass) return null;
    const { store } = await openCentralSync(pass);
    session.syncStore = store;
    return store;
  }
  function deriveTitle() {
    const firstUser = (session.transcript || []).find((b) => b.role === "user");
    const t = ((firstUser && firstUser.text) || "").replace(/\n+/g, " ").trim();
    return t ? t.slice(0, 48) : "New chat";
  }
  function lastPreview() {
    const t = session.transcript || [];
    const last = t[t.length - 1];
    return ((last && last.text) || "").replace(/\n+/g, " ").trim().slice(0, 80);
  }
  // Persist the current chat to the sync store (best-effort; the local copy is
  // always saved regardless, so an offline/rate-limited push loses nothing).
  async function syncSave() {
    if (!syncOn() || !session || !session.chatId) return;
    // A read-only chat has no turns to save, and this payload would claim it
    // for the phone and drop the fields the desktop owns. Notes left here go
    // through parkNoteForDesktop, which writes only `pending`.
    if (session.readOnly) return;
    let store;
    try { store = await ensureSyncStore(); } catch (e) { return; }
    if (!store) return;
    try {
      session.chatTitle = session.chatTitle || deriveTitle();
      // save() replaces the whole chat object, so anything this device does
      // not send is destroyed. The desktop keeps its working directory, todos
      // and model choice under `desktop`, and its checkout state under
      // `repo_state`; none of that is the phone's to author, and dropping it
      // meant a desktop chat lost its cwd the moment the phone answered in it.
      // Carry the untouched fields back out exactly as they came in.
      const carry = session.carry || {};
      session.syncedAt = await store.save(Object.assign({}, carry, {
        id: session.chatId, title: session.chatTitle, preview: lastPreview(),
        repo: session.repo,
        // The project label belongs to whoever owns the chat's repo. Only
        // claim it when this chat has no label yet, so answering from the
        // phone can't relabel a desktop chat to a repo name.
        project: carry.project || (session.repo && session.repo.full_name) || "",
        device: "phone",
        pending: session.pending || [],
        messages: stripImages(session.messages || []),
        transcript: session.transcript || [],
      }));
    } catch (e) {
      // Deleted on the other device while this one still had it open. Say so
      // rather than retrying forever, and stop this chat re-uploading itself.
      if (e && e.chatDeleted) {
        session.chatId = null;
        addBubble("system", "This chat was deleted on your other device. " +
          "It won't be saved here — start a new one to keep going.", false);
        return;
      }
      /* offline / rate-limited — keep the local copy */
    }
  }

  // ---- catching up with the other device ----
  // The point of sync is that you can put the phone down, keep working on the
  // desktop, and pick the phone back up on the same conversation. So whenever
  // the app comes back to the foreground, quietly check whether the open chat
  // moved on elsewhere and adopt it. Never runs mid-turn, and never replaces
  // local history with something shorter — losing a message you just sent would
  // be far worse than being slightly behind.
  async function refreshOpenChatFromSync() {
    if (currentRun || composing) return;
    if (!syncOn() || !session || !session.chatId) return;
    if ($("screen-chat").hidden) return;
    let store;
    try { store = await ensureSyncStore(); } catch (e) { return; }
    if (!store) return;
    try {
      const row = (await store.list()).find((c) => c.id === session.chatId);
      if (!row || !row.updated || row.updated <= (session.syncedAt || 0)) return;
      const data = await store.load(session.chatId);
      if (!data || !Array.isArray(data.messages)) return;
      session.syncedAt = row.updated;
      if (data.messages.length <= (session.messages || []).length) return;
      session.messages = data.messages;
      session.transcript = data.transcript || [];
      session.messages[0] = { role: "system", content: session.baseSystem };
      session.messages = AC.applyHandoff(session.messages, data.device, DEVICE_LABEL);
      $("messages").innerHTML = "";
      noteDesktopWork(data.repo_state);
      for (const b of session.transcript) addBubble(b.role, b.text, false);
      addBubble("system", "Caught up with your " + (row.device || "other device") + ".", false);
      scroll();
      haptic(10);
      persistSession();
    } catch (e) { /* offline — keep what we have */ }
  }

  // ---- cross-device lock (same chat open on phone + desktop at once) ----
  // A courtesy, not a guarantee: self-heals via TTL if a device disappears
  // mid-turn, and never permanently blocks — see agent-core.js's
  // DEVICE_LOCK_TTL_MS note and glmcode/syncstore.py for the desktop twin.
  function deviceId() {
    let id = localStorage.getItem(DEVICEID_KEY);
    if (!id) {
      try { id = crypto.randomUUID(); }
      catch { id = "d" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }
      localStorage.setItem(DEVICEID_KEY, id);
    }
    return id;
  }
  const chatLockHeartbeats = {}; // chatId -> setInterval id, for chats this device currently holds the lock on
  function stopLockHeartbeat(chatId) {
    const id = chatLockHeartbeats[chatId];
    if (id) { clearInterval(id); delete chatLockHeartbeats[chatId]; }
  }
  function startLockHeartbeat(chatId, store) {
    stopLockHeartbeat(chatId);
    chatLockHeartbeats[chatId] = setInterval(async () => {
      try {
        const ok = await store.renewLock(chatId, deviceId(), DEVICE_LABEL);
        if (!ok) { stopLockHeartbeat(chatId); addBubble("system", "Heads up: this chat is now also being used on another device."); }
      } catch (e) { /* fail open — a transient error must not be misread as "preempted" */ }
    }, AC.DEVICE_LOCK_HEARTBEAT_S * 1000);
  }
  // Returns null if the turn is free to proceed (sync off, no chat, lock
  // acquired, or sync unreachable — fail open, since unreachable means the
  // other device can't push either). Returns { locked, lockedBy, lockedSince }
  // if another live device holds it and force wasn't set.
  async function tryAcquireDeviceLock(chatId, force) {
    if (!syncOn() || !chatId) return null;
    let store;
    try { store = await ensureSyncStore(); } catch (e) { return null; }
    if (!store) return null;
    try {
      await store.acquireLock(chatId, deviceId(), DEVICE_LABEL, !!force);
    } catch (e) {
      if (e && e.lockedElsewhere) return { locked: true, lockedBy: e.deviceLabel, lockedSince: e.sinceMs };
      return null;
    }
    startLockHeartbeat(chatId, store);
    return null;
  }
  async function releaseDeviceLock(chatId) {
    stopLockHeartbeat(chatId);
    if (!syncOn() || !chatId) return;
    let store;
    try { store = await ensureSyncStore(); } catch (e) { return; }
    if (!store) return;
    try { await store.releaseLock(chatId, deviceId()); } catch (e) { /* best effort */ }
  }
  // The desktop publishes its git state with the chat. If it has work GitHub
  // hasn't seen, both the user and the agent need to know: the files read here
  // are older than that machine's, so editing them risks committing over it.
  // Silent when GitHub really is the latest word, which is the common case.
  function noteDesktopWork(repoState) {
    const warn = AC.repoStateWarning(repoState, session.repo && session.repo.branch);
    if (!warn) return;
    session.messages.push({ role: "system", content: "[desktop-state] " + warn });
    addBubble("system", "⚠︎ " + warn, false);
  }

  // Shows who holds the chat and lets the user override. Resolves true to
  // retry with force=true, false to leave the composer restored and stop.
  async function confirmLockOverride(lockResult) {
    const mins = Math.max(1, Math.round((Date.now() - (lockResult.lockedSince || Date.now())) / 60000));
    return confirm(
      `This chat is active on ${lockResult.lockedBy} right now (started ${mins}m ago).\n\n` +
      "Sending here too can overwrite what you're doing there. Send anyway?");
  }

  // ---- chat history screen ----
  function relTime(ms) {
    if (!ms) return "";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  async function enterChatList() {
    show("screen-chats");
    $("chats-error").textContent = "";
    $("chats-list").innerHTML = "<li class='muted'>Loading…</li>";
    let store;
    try { store = await ensureSyncStore(); }
    catch (e) {
      $("chats-list").innerHTML = "";
      $("chats-error").textContent = "Couldn't open sync: " + friendlyGhError(e, "list");
      return;
    }
    if (!store) {
      // Sync isn't actually configured (edge case: toggled off elsewhere).
      // There's no hub to show without it — fall back to wherever makes sense.
      if (session.repo) startNewChat(); else enterRepoPicker();
      return;
    }
    let list;
    try { list = await store.list(); }
    catch (e) { $("chats-list").innerHTML = ""; $("chats-error").textContent = friendlyGhError(e, "list"); return; }
    renderChatList(list);
  }
  function renderChatList(list) {
    const ul = $("chats-list");
    ul.innerHTML = "";
    for (const c of list) {
      const li = document.createElement("li");
      li.className = "chat-row";
      const main = document.createElement("div");
      main.className = "chat-row-main";
      const title = document.createElement("div");
      title.className = "chat-row-title";
      title.textContent = c.title || "Untitled";
      const meta = document.createElement("div");
      meta.className = "chat-row-meta";
      // Show which project a chat belongs to — they all share one store now.
      meta.textContent = [c.project, relTime(c.updated), c.preview]
        .filter(Boolean).join(" · ");
      main.append(title, meta);
      // A chat whose project isn't a GitHub repo can be READ here but not
      // continued: every tool on the phone goes through the GitHub API, and
      // there is nothing for it to act on. Say so in the list rather than
      // letting it look identical to a chat that works and only explaining
      // after it's tapped.
      if (!c.repo) {
        li.classList.add("chat-row-local");
        const tag = document.createElement("span");
        tag.className = "chat-row-tag";
        tag.textContent = "on your computer";
        // First on the meta line, not inside the title: the title is a single
        // ellipsised line, so a long one would truncate the label away —
        // exactly on the chats where knowing matters most.
        meta.prepend(tag);
      }
      main.addEventListener("click", () => openSyncChat(c.id));
      const del = document.createElement("button");
      del.className = "chat-row-del"; del.type = "button"; del.title = "Delete"; del.textContent = "🗑";
      del.addEventListener("click", (e) => { e.stopPropagation(); deleteSyncChat(c.id, c.title); });
      li.append(main, del);
      ul.appendChild(li);
    }
    if (!list.length) ul.innerHTML = "<li class='muted'>No saved chats yet — start a new one.</li>";
  }
  async function openSyncChat(id) {
    let data;
    try {
      const store = await ensureSyncStore();
      data = await store.load(id);
    } catch (e) { toast("Couldn't open that chat: " + friendlyGhError(e, "list")); return; }
    if (!data || !Array.isArray(data.messages)) { toast("That chat looks empty."); return; }
    // The store is shared across projects now, so a chat may belong to a repo
    // other than the one currently open — follow it there rather than silently
    // re-pointing the conversation at the wrong codebase.
    // A chat belongs to a repository, and only to that one. Following it there
    // is right; INHERITING whatever this phone had open is not.
    //
    // That is what used to happen when a chat arrived without a repo of its
    // own (every desktop chat did, until the desktop started publishing one).
    // The guard below only caught a phone with no repo at all — if one was
    // left over from an earlier conversation, the chat silently adopted it,
    // the agent read and committed into a codebase the conversation was never
    // about, and the next save wrote that repo back to the shared store,
    // relabelling the chat on every device. Refuse instead: an unanswerable
    // question is not the phone's to guess at.
    const r = data.repo;
    if (r && r.full_name) {
      if (!session.repo || r.full_name !== session.repo.full_name) {
        connectRepo(r.owner, r.repo, r.branch || "main", r.full_name);
      }
    } else {
      // No repo: the agent has nothing to act on here, but the conversation is
      // still worth reading, and work can still be left for the machine that
      // CAN act. Opening it read-only is the whole reason it syncs at all.
      openReadOnlyChat(data, id);
      return;
    }
    if (!session.repo) { toast("That chat has no repository — open one first."); return; }
    session.chatId = data.id || id;
    session.chatTitle = data.title || "";
    session.messages = data.messages;
    session.transcript = data.transcript || [];
    session.syncedAt = data.updated || 0;
    session.pending = data.pending || [];
    session.images = {};
    session.compact = null;
    session.toldCompact = false;
    // Fields this device does not own, kept verbatim so saving from here
    // doesn't destroy them (see syncSave).
    // Everything this chat arrived with; syncSave overlays the parts the phone
    // owns. See openReadOnlyChat for why this is not an enumerated list.
    session.carry = Object.assign({}, data);
    session.readOnly = false;
    applyReadOnlyChrome(false);
    session.messages[0] = { role: "system", content: session.baseSystem };  // rebind to this repo
    // Picking up a chat the desktop was driving: mark the switch, or the model
    // keeps imitating turns that used tools this phone doesn't have.
    session.messages = AC.applyHandoff(session.messages, data.device, DEVICE_LABEL);
    noteDesktopWork(data.repo_state);
    clearAttachments();
    $("chat-repo-name").textContent = session.repo.full_name;
    $("messages").innerHTML = "";
    for (const b of session.transcript) addBubble(b.role, b.text, false);
    addBubble("system", "Resumed “" + (session.chatTitle || "chat") + "”.", false);
    show("screen-chat");
    renderContextMeter();
    persistSession();
  }
  // ---- read-only: a chat whose project this phone can't reach ----
  // It syncs, so it should be readable — looking up what you decided is most of
  // what you want a phone for. And the one useful thing you CAN do without a
  // repo is leave work for the machine that has one, which is the same pending
  // queue the agent uses via needs_desktop.
  function openReadOnlyChat(data, id) {
    session.readOnly = true;
    session.chatId = data.id || id;
    session.chatTitle = data.title || "";
    session.messages = [];             // nothing runs here; keep none of it live
    session.transcript = data.transcript || [];
    session.syncedAt = data.updated || 0;
    session.pending = data.pending || [];
    session.images = {};
    session.compact = null;
    // The WHOLE chat, not a list of fields to remember. An enumerated carry is
    // one someone forgets to extend: the first version of this omitted
    // `transcript`, so leaving a note blanked the conversation you were reading.
    // Carrying everything and overlaying only what this device owns cannot have
    // that failure mode, including for fields added later.
    session.carry = Object.assign({}, data);
    clearAttachments();
    $("chat-repo-name").textContent = data.project || "on your computer";
    $("messages").innerHTML = "";
    for (const b of session.transcript) addBubble(b.role, b.text, false);
    for (const p of session.pending) {
      addBubble("system", "📌 For your computer: " + p.task, false);
    }
    addBubble("system",
      "This chat is about a folder on your computer, so the agent can't work on it " +
      "here. You can read it — and anything you send becomes a note waiting on your " +
      "computer when you open the chat there.", false);
    show("screen-chat");
    applyReadOnlyChrome(true);
    scroll();
  }
  // Read-only changes what the composer is FOR, rather than taking it away: an
  // empty box you can't type in explains nothing.
  function applyReadOnlyChrome(on) {
    $("btn-attach").hidden = on;        // nothing to attach without a repo
    $("ctx-foot").hidden = on;          // no context is being spent here
    prompt.placeholder = on ? "Leave a note for your computer…" : "Message the agent…";
    $("btn-send").title = on ? "Leave a note for your computer" : "Send";
    document.getElementById("screen-chat").classList.toggle("read-only", on);
  }
  // A note left here goes into the same queue needs_desktop writes, so the
  // desktop surfaces it the same way when the chat is opened there.
  async function parkNoteForDesktop(text) {
    session.pending = session.pending || [];
    if (session.pending.some((p) => p.task.toLowerCase() === text.toLowerCase())) {
      toast("Already waiting on your computer.");
      return;
    }
    session.pending.push({ task: text, why: "left from your phone", created: Date.now() });
    addBubble("system", "📌 For your computer: " + text, false);
    haptic(12);
    const store = await ensureSyncStore().catch(() => null);
    if (!store) { toast("Saved here — it'll sync when you're back online."); return; }
    try {
      // Only `pending` is ours to change. Everything else goes back exactly as
      // it came in — including `device`, so the desktop doesn't read this as
      // the phone having driven the conversation.
      const carry = session.carry || {};
      session.syncedAt = await store.save(Object.assign({}, carry, {
        id: session.chatId, pending: session.pending,
      }));
      toast("Waiting on your computer.");
    } catch (e) {
      toast(e && e.chatDeleted ? "That chat was deleted on your other device."
                               : "Couldn't sync that note: " + friendlyGhError(e, "list"));
    }
  }
  async function deleteSyncChat(id, title) {
    if (!confirm("Delete “" + (title || "this chat") + "” from all your devices?")) return;
    try {
      const store = await ensureSyncStore();
      await store.remove(id);
      if (session.chatId === id) session.chatId = null;
      await enterChatList();
    } catch (e) { toast("Couldn't delete: " + friendlyGhError(e, "list")); }
  }
  // "+" always goes through the repo picker: starting something new means
  // choosing (or creating) its project, which the hub itself doesn't do.
  $("btn-chats-new").addEventListener("click", () => { if (!currentRun) enterRepoPicker(); });
  $("btn-chats-lock").addEventListener("click", lock);

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
  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    // Let go of the field. On iOS the keyboard — and the accessory bar iOS
    // draws above it — stay up until something blurs, so sending with the
    // return key left both sitting there and the only way out was the Done
    // button on a bar that has nothing to do with this app. Sending is the end
    // of the thought; the keyboard should go with it.
    prompt.blur();
    sendPrompt();
  });
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

  // ---- live (streamed) assistant text ----
  // The reply is re-rendered as real markdown while it arrives, throttled to a
  // few times a second — so you read formatted prose as it lands instead of raw
  // ## and ** that only resolve at the end. Partial syntax (an unclosed fence or
  // **bold) simply renders as plain text until it completes.
  const STREAM_RENDER_MS = 110;
  let stream = null;   // { bubble, text, raf, lastRender }
  function beginStream() {
    const near = atBottom();
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant streaming";
    messages.appendChild(bubble);
    if (near) scroll();
    return { bubble, text: "", raf: 0, lastRender: 0 };
  }
  function paintStream() {
    const near = atBottom();
    while (stream.bubble.firstChild) stream.bubble.removeChild(stream.bubble.firstChild);
    renderText(stream.bubble, stream.text);
    if (near) scroll();
    syncToBottomBtn();
  }
  function flushStream() {
    if (!stream) return;
    stream.raf = 0;
    const now = (window.performance || Date).now();
    // Too soon to repaint: keep a frame pending so the newest tokens still land.
    if (now - stream.lastRender < STREAM_RENDER_MS) {
      stream.raf = requestAnimationFrame(flushStream);
      return;
    }
    stream.lastRender = now;
    paintStream();
  }
  function streamAppend(text) {
    if (!stream) stream = beginStream();
    stream.text += text;
    if (!stream.raf) stream.raf = requestAnimationFrame(flushStream);
  }
  // Settle the bubble on the final text and record it once. Passing the final
  // text guards against a dropped last frame.
  function endStream(finalText) {
    if (!stream) return false;
    const s = stream;
    if (s.raf) cancelAnimationFrame(s.raf);
    const text = (finalText != null && finalText !== "" ? finalText : s.text).trim();
    if (!text) { s.bubble.remove(); stream = null; return true; }
    stream.text = text;
    paintStream();
    stream = null;
    s.bubble.classList.remove("streaming");
    addBubbleActions(s.bubble, text);
    refreshTailActions();
    if (session) {
      session.transcript = session.transcript || [];
      session.transcript.push({ role: "assistant", text });
    }
    if (atBottom()) scroll();
    return true;
  }

  // The GLM models carry ~200k. The headroom below that is for the reply and
  // for the estimate being an estimate — but the estimate now calibrates itself
  // against the exact prompt_tokens the API reports (see calibrateRatio), so it
  // no longer has to be padded by guesswork the way a pure chars/N count does.
  const CONTEXT_LIMIT_TOKENS = 185000;
  // Trim before the model would refuse. Overshooting doesn't fail one turn, it
  // fails every turn after it, so the automatic pass fires with room to spare.
  const CONTEXT_BUDGET_TOKENS = 170000;

  // chars-per-token, measured rather than assumed once the API prices a request.
  let tokenRatio = AC.DEFAULT_CHARS_PER_TOKEN;
  function noteUsage(sentMessages, usage) {
    const r = AC.calibrateRatio(sentMessages, usage && usage.prompt_tokens);
    if (r) tokenRatio = r;
    renderContextMeter();
  }
  function contextTokens() {
    if (!session || !session.messages) return 0;
    return AC.estimateTokens(session.messages, tokenRatio);
  }
  function renderContextMeter() {
    const foot = $("ctx-foot");
    if (!foot) return;
    const used = contextTokens();
    const pct = Math.min(100, (used / CONTEXT_LIMIT_TOKENS) * 100);
    $("token-segment").setAttribute("stroke-dasharray", pct.toFixed(1) + ", 100");
    $("token-text").textContent = used < 1000
      ? used + " tokens"
      : (used / 1000).toFixed(used < 10000 ? 1 : 0) + "k / " +
        Math.round(CONTEXT_LIMIT_TOKENS / 1000) + "k";
    foot.classList.toggle("warn", pct >= 70 && pct < 90);
    foot.classList.toggle("danger", pct >= 90);
    $("btn-compact").disabled = !session || (session.messages || []).length < 4;
  }
  // Manual compaction: summarise everything but the most recent turns and
  // replace them, so the user can reclaim room deliberately instead of waiting
  // for the automatic pass. Unlike the automatic trim this DOES rewrite
  // session.messages -- it's an explicit instruction, not a display concern.
  async function compactNow() {
    if (!session || currentRun || composing) return;
    const msgs = session.messages || [];
    if (msgs.length < 4) { toast("Nothing to compact yet."); return; }
    // Keep roughly the last fifth of the conversation, and never nothing.
    const keepBudget = Math.max(2000, Math.round(contextTokens() / 5));
    const res = AC.trimHistory(msgs, keepBudget);
    if (!res.droppedTurns) { toast("Nothing old enough to compact."); return; }
    setRunning(true);
    setStatus("compacting…");
    try {
      const summary = await compactSummary(res.dropped);
      const next = res.messages.slice();
      const at = next[0] && next[0].role === "system" ? 1 : 0;
      next.splice(at, 0, { role: "system", content: "[compacted] " + summary });
      session.messages = next;
      session.compact = null;          // the summary is inline now, not a view
      addBubble("system", "Compacted " + res.droppedTurns + " earlier turn" +
        (res.droppedTurns === 1 ? "" : "s") + " into a summary.", false);
      haptic(14);
      persistSession();
      syncSave();
    } catch (e) {
      toast("Couldn't compact: " + (e && e.message ? e.message : e));
    } finally {
      setStatus("");
      setRunning(false);
      renderContextMeter();
    }
  }

  // A summary of the turns that no longer fit. Cached against how much has been
  // dropped so a long chat doesn't pay for a summarisation call every turn.
  async function compactSummary(dropped) {
    const key = dropped.length;
    if (session.compact && session.compact.key === key) return session.compact.text;
    setStatus("compacting…");
    let text = "";
    try {
      const r = await session.model.chat(
        [{ role: "system", content: AC.COMPACT_PROMPT },
         { role: "user", content: AC.historyDigest(dropped) }], undefined);
      text = ((r && r.content) || "").trim();
    } catch (e) { /* best effort — a failed summary must not block the turn */ }
    if (!text) {
      text = "Earlier turns were trimmed to fit the context window. " +
             "Ask the user to re-state anything from them you need.";
    }
    session.compact = { key, text };
    return text;
  }

  // What the model actually sees this turn. session.messages is left WHOLE:
  // this phone's smaller context must not permanently delete history that the
  // desktop — which syncs the same chat — still has room for.
  async function modelView() {
    const full = session.messages;
    if (AC.estimateTokens(full) <= CONTEXT_BUDGET_TOKENS) return full;
    const res = AC.trimHistory(full, CONTEXT_BUDGET_TOKENS);
    if (!res.droppedTurns) return full;
    const summary = await compactSummary(res.dropped);
    const view = res.messages.slice();
    const at = view[0] && view[0].role === "system" ? 1 : 0;
    view.splice(at, 0, { role: "system", content: "[compacted] " + summary });
    // Say it once per conversation, not on every re-summarisation.
    if (!session.toldCompact) {
      session.toldCompact = true;
      addBubble("system", "Earlier turns no longer fit, so they're summarised from here on.", false);
    }
    return view;
  }

  async function runTurn(shouldStop, tools, toolSchemas) {
    let liveTool = null;
    const messages = await modelView();
    const grewFrom = messages.length;
    const out = await AC.runAgent({
      model: session.model,
      tools: tools || session.tools,
      messages,
      shouldStop,
      takeSteer: () => { const t = steerQueued; steerQueued = ""; return t; },
      toolSchemas,
      stream: true,
      onEvent: (ev) => {
        armIdle();
        if (ev.type === "thinking") setStatus("thinking…");
        else if (ev.type === "delta") { setStatus("writing…"); streamAppend(ev.text); }
        // Any tool call ends the streamed preamble that came before it.
        else if (ev.type === "tool") { endStream(); liveTool = addTool(ev.name, ev.args); setStatus(ev.name + "…"); }
        else if (ev.type === "tool_result") { if (liveTool) finishTool(liveTool, ev.out); }
        else if (ev.type === "answer") {
          setStatus("");
          if (!endStream(ev.text) && ev.text) addBubble("assistant", ev.text);
          haptic(12);
        }
        else if (ev.type === "steered") { endStream(); steerAccepted(ev.text); setStatus("taking that in…"); }
        else if (ev.type === "usage") noteUsage(ev.sent, ev.usage);
        else if (ev.type === "error") { setStatus(""); endStream(); addBubble("error", ev.text); }
        else if (ev.type === "stopped") { setStatus(""); endStream(); addBubble("system", "Stopped."); }
      },
    });
    // runAgent appends this turn onto the array it was handed. When that was a
    // trimmed view rather than the real history, fold the new turn back in --
    // otherwise the conversation would quietly stop growing.
    if (messages !== session.messages) {
      session.messages.push(...messages.slice(grewFrom));
    }
    return out;
  }

  const VISION_MODEL = "glm-4.6v-flash";  // free vision model, used by view_image

  // view_image is always advertised: repo images are reachable in any chat, and
  // even a vision model can't fetch one from GitHub by itself. (Uploads are the
  // one case a vision model handles directly — they're already in its context.)
  function visionSchemas(base) {
    return base.concat([AC.VIEW_IMAGE_SCHEMA, AC.NEEDS_DESKTOP_SCHEMA]);
  }
  // Advertise spawn_agent on the main turn only when sub-agents are enabled.
  function mainSchemas() {
    const base = subagentsOn() ? AC.TOOL_SCHEMAS.concat([AC.SPAWN_SCHEMA]) : AC.TOOL_SCHEMAS;
    return visionSchemas(base);
  }

  // needs_desktop: park something that needs a real machine. It rides along
  // with the synced chat and is put in front of the desktop agent when the chat
  // opens there, so "run the tests when you're back" survives the trip instead
  // of scrolling away.
  async function needsDesktop(task, why) {
    if (!task) return "Nothing recorded — say what needs running.";
    session.pending = session.pending || [];
    // Don't stack the same ask twice if the agent repeats itself.
    if (session.pending.some((p) => p.task.toLowerCase() === task.toLowerCase())) {
      return "Already on the list for the desktop: " + task;
    }
    session.pending.push({ task, why: why || "", created: Date.now() });
    addBubble("system", "📌 For your desktop: " + task, false);
    haptic(10);
    persistSession();
    return "Noted for the desktop. It'll see this when the chat opens there. " +
           "Carry on with anything you can do here.";
  }

  // Resolve an image that lives in the REPO to a data URL. getFile() decodes as
  // UTF-8 and would mangle the bytes, so this goes through the binary-safe read.
  // Accepts an exact path or a bare filename, which is matched against the tree.
  async function repoImageDataUrl(name) {
    if (!session || !session.gh) return null;
    let path = name;
    if (!AC.IMAGE_RE.test(path)) return null;
    try {
      const paths = (await session.gh.tree()).map((e) => e.path);
      if (!paths.includes(path)) {
        const lower = name.toLowerCase();
        const hit = paths.find((p) => p.toLowerCase() === lower)
          || paths.find((p) => p.toLowerCase().endsWith("/" + lower))
          || paths.find((p) => AC.IMAGE_RE.test(p) && p.toLowerCase().includes(lower));
        if (!hit) return null;
        path = hit;
      }
    } catch (e) { /* tree unavailable — still try the path as given */ }
    const { b64 } = await session.gh.getFileRaw(path);
    if (!b64) return null;
    return { url: "data:" + AC.imageMime(path) + ";base64," + b64, path };
  }

  // The view_image tool: send an image to the free vision model and return its
  // written description, so a text model can act on it. Resolves attachments
  // first, then falls back to image files in the repo — without that fallback
  // the agent has no way at all to see a screenshot or mockup that's committed.
  async function viewImage(name, question) {
    const imgs = session.images || {};
    const keys = Object.keys(imgs);
    let url = imgs[name];
    if (!url) {
      const hit = keys.find((k) => k === name || k.endsWith(name) || name.endsWith(k) || k.includes(name));
      url = hit ? imgs[hit] : null;
    }
    let label = name;
    if (!url) {
      try {
        const found = await repoImageDataUrl(name);
        if (found) { url = found.url; label = found.path; }
      } catch (e) {
        return "Couldn't read '" + name + "' from the repo: " + (e && e.message ? e.message : e);
      }
    }
    // Only fall back to "the one attachment" when nothing else matched, so a
    // wrong repo path doesn't silently describe an unrelated upload.
    if (!url && keys.length === 1) { url = imgs[keys[0]]; label = keys[0]; }
    if (!url) {
      return "No image matches '" + name + "'. Attached: " + (keys.join(", ") || "none") +
        ". For an image in the repo, pass its path (e.g. docs/shot.png) — use glob '**/*.png' to find it.";
    }
    if (label !== name) setStatus("looking at " + label + "…");
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

  // Wrap a run: manage the running state, stop control, errors, and the
  // cross-device lock. Returns a { locked, lockedBy, lockedSince } object if
  // another live device holds the chat and force wasn't set (fn never runs
  // in that case); otherwise undefined.
  async function withRun(fn, opts) {
    if (currentRun) return;
    opts = opts || {};
    const chatId = session && session.chatId;
    if (chatId) {
      const lockResult = await tryAcquireDeviceLock(chatId, !!opts.force);
      if (lockResult) return lockResult;
    }
    stopFlag = false;
    currentRun = { stop: () => { stopFlag = true; $("btn-stop").disabled = true; } };
    setRunning(true);
    try { await fn(() => stopFlag); }
    catch (e) { addBubble("error", e.message || String(e)); }
    finally {
      setRunning(false); currentRun = null; persistSession(); renderContextMeter();
      await syncSave();
      if (chatId) await releaseDeviceLock(chatId);
      // After currentRun is cleared, so this starts a turn rather than
      // re-queueing itself against the run that just ended.
      if (steerQueued) await steerLeftOver();
    }
  }

  // Undo the optimistic bubble/message-array additions made before a send
  // that turned out to be locked elsewhere.
  function popOptimisticUser(bubble) {
    if (bubble && bubble.remove) bubble.remove();
    if (session) {
      const t = session.transcript;
      if (t && t.length && t[t.length - 1].role === "user") t.pop();
      const m = session.messages;
      if (m && m.length && m[m.length - 1].role === "user") m.pop();
    }
  }
  // Same, plus restore the composer's text/attachments (for a manual send).
  function rollbackOptimisticSend(bubble, text, atts) {
    popOptimisticUser(bubble);
    prompt.value = text;
    prompt.style.height = "auto";
    prompt.style.height = Math.min(prompt.scrollHeight, 160) + "px";
    attachments = atts;
    renderChips();
  }

  // A message typed while a turn is running redirects that turn instead of
  // starting another. Typing is slow on a phone, so the thing you forgot to
  // say usually arrives after you've already hit send.
  let steerQueued = "";
  let steerBubble = null;
  function queueSteer(text) {
    if (steerQueued) {
      // One at a time, so the model isn't handed a pile of contradictions.
      toast("Already queued — that'll go in at the next step.");
      return;
    }
    steerQueued = text;
    prompt.value = ""; prompt.style.height = "auto"; fitMessages();
    steerBubble = addBubble("user", text, false);
    steerBubble.classList.add("queued");
    haptic(8);
  }
  // Consumed by the run: it's a real part of the conversation now.
  function steerAccepted(text) {
    if (steerBubble) { steerBubble.classList.remove("queued"); steerBubble = null; }
    session.transcript = session.transcript || [];
    session.transcript.push({ role: "user", text });
    refreshTailActions();
  }
  // The turn ended before it was picked up. Don't drop it — send it as the
  // next message, which is what was wanted anyway.
  async function steerLeftOver() {
    const text = steerQueued;
    steerQueued = "";
    if (!text) return;
    if (steerBubble) { steerBubble.remove(); steerBubble = null; }
    prompt.value = text;
    await sendPrompt();
  }

  async function sendPrompt(force) {
    if (composing) return;
    if (currentRun) {                    // mid-turn: steer instead of queueing a turn
      const t = prompt.value.trim();
      if (t) queueSteer(t);
      return;
    }
    const text = prompt.value.trim();
    if (session && session.readOnly) {
      // No agent to send to; the send button parks a note instead.
      if (!text) return;
      prompt.value = ""; prompt.style.height = "auto"; fitMessages();
      await parkNoteForDesktop(text);
      return;
    }
    if (!text && !attachments.length) return;
    const savedAttachments = attachments.slice();
    prompt.value = ""; prompt.style.height = "auto"; fitMessages();
    const bubble = addBubble("user", (text || "(attached files)") + attachmentNote());
    // composeMessage may call the vision model (to describe uploaded images), so
    // guard against a second send and disable the composer while it runs.
    composing = true; setRunning(true);
    let content;
    try { content = await composeMessage(text); }
    finally { composing = false; setRunning(false); }
    clearAttachments();
    session.turnCommits = 0;
    session.messages.push({ role: "user", content });

    const lockResult = await withRun(async (getStopped) => {
      if (planMode()) {
        session.messages[0].content = session.baseSystem + PLAN_DIRECTIVE;
        await runTurn(getStopped, session.readTools, READ_SCHEMAS);
        if (!getStopped()) showApproveBar();
      } else {
        await runBuild(getStopped);
      }
    }, { force });

    if (lockResult && lockResult.locked) {
      rollbackOptimisticSend(bubble, text, savedAttachments);
      if (await confirmLockOverride(lockResult)) await sendPrompt(true);
    }
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

  async function executePlan(force) {
    const bubble = addBubble("user", "Approved — build it.");
    session.turnCommits = 0;
    session.messages.push({ role: "user", content: "Approved. Implement that plan now: make the edits and commit them." });
    const lockResult = await withRun((getStopped) => runBuild(getStopped), { force });
    if (lockResult && lockResult.locked) {
      popOptimisticUser(bubble);
      if (await confirmLockOverride(lockResult)) await executePlan(true);
      else showApproveBar();
    }
  }

  function setRunning(on) {
    // The send button stays available while a turn runs: what it does changes
    // from "start a turn" to "steer the one in flight". Stop sits beside it.
    $("btn-stop").hidden = !on;
    $("btn-stop").disabled = false;
    $("btn-attach").disabled = on;   // attachments compose a fresh message
    prompt.disabled = false;
    prompt.placeholder = on ? "Add something to the run…" : "Message the agent…";
  }

  // ------------------------------------------------------------- rendering
  const messages = $("messages");
  function atBottom() { return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80; }
  function scroll() { messages.scrollTop = messages.scrollHeight; }

  // Scrolling up during a long reply shouldn't strand you: a pill appears to
  // jump back to the live end. (Auto-scroll already pauses while you're away
  // from the bottom, so reading back never fights the stream.)
  $("btn-compact").addEventListener("click", compactNow);
  $("btn-ctx").addEventListener("click", () => {
    const used = contextTokens();
    toast(used.toLocaleString() + " of ~" + CONTEXT_LIMIT_TOKENS.toLocaleString() +
      " tokens used. Older turns are summarised automatically before this fills.");
  });

  const toBottomBtn = $("btn-to-bottom");
  function syncToBottomBtn() { toBottomBtn.hidden = atBottom(); }
  messages.addEventListener("scroll", syncToBottomBtn, { passive: true });
  toBottomBtn.addEventListener("click", () => {
    messages.scrollTo({ top: messages.scrollHeight, behavior: "smooth" });
    toBottomBtn.hidden = true;
  });
  // A quiet "Copy" affordance on assistant replies — the phone equivalent of
  // the desktop's hover actions, where there's no hover to rely on.
  // ---- rewinding the tail of a conversation ----
  // Edit-and-resend and Retry are deliberately limited to the LAST exchange.
  // Reaching further back would mean mapping a bubble to a position in
  // session.messages, which tool calls and compaction both shift underneath —
  // and the last turn is where essentially all of the need is: you spot the
  // typo you just made, or the answer you just got was wrong.
  function lastUserIndex() {
    const m = session.messages || [];
    for (let i = m.length - 1; i >= 1; i--) if (m[i].role === "user") return i;
    return -1;
  }
  // Re-render the visible conversation from the transcript. Tool lines aren't
  // in the transcript, so they don't come back — the same as resuming a chat.
  function replayTranscript() {
    $("messages").innerHTML = "";
    for (const b of session.transcript || []) addBubble(b.role, b.text, false);
    refreshTailActions();
    scroll();
    syncToBottomBtn();
  }
  // Drop the transcript back to just before its last entry of `role`.
  function trimTranscriptFromLast(role) {
    const t = session.transcript || [];
    for (let i = t.length - 1; i >= 0; i--) {
      if (t[i].role === role) { session.transcript = t.slice(0, i); return; }
    }
  }

  async function regenerateLast() {
    if (currentRun || composing) return;
    const at = lastUserIndex();
    if (at < 0) { toast("Nothing to retry yet."); return; }
    // Keep the user's message, drop everything the model said after it.
    session.messages = session.messages.slice(0, at + 1);
    trimTranscriptFromLast("assistant");
    session.compact = null;          // the summary described messages that are gone
    replayTranscript();
    haptic(10);
    await withRun((getStopped) => runBuild(getStopped));
  }

  async function editLast() {
    if (currentRun || composing) return;
    const at = lastUserIndex();
    if (at < 0) { toast("Nothing to edit yet."); return; }
    const original = session.messages[at];
    // Attachments were folded into the message text when it was composed, so
    // only the typed part can be handed back — say so instead of pretending.
    const text = typeof original.content === "string"
      ? original.content
      : (original.content || []).filter((p) => p.type === "text").map((p) => p.text).join(" ");
    session.messages = session.messages.slice(0, at);
    trimTranscriptFromLast("user");
    session.compact = null;
    replayTranscript();
    prompt.value = text;
    prompt.style.height = "auto";
    prompt.style.height = Math.min(prompt.scrollHeight, 160) + "px";
    prompt.focus();
    persistSession();
    toast("Edit and send again. Any files already committed stay committed.");
  }

  function addBubbleActions(bubble, text) {
    const act = document.createElement("div");
    act.className = "bubble-actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "bubble-copy";
    copy.textContent = "Copy";
    copy.addEventListener("click", () => copyText(text, copy));
    act.appendChild(copy);
    bubble.appendChild(act);
  }

  // Edit and Retry belong ONLY on the newest exchange, since that's all they
  // can act on. Showing them on older bubbles would quietly rewind the tail
  // instead of the message the user actually pointed at.
  function refreshTailActions() {
    for (const b of messages.querySelectorAll(".tail-action")) b.remove();
    // Nothing to edit or re-run in a read-only chat: there is no agent behind
    // it and session.messages is empty, so both buttons lead nowhere. Offering
    // them is worse than not having them.
    if (session && session.readOnly) return;
    const mk = (bubble, label, fn) => {
      if (!bubble) return;
      let act = bubble.querySelector(".bubble-actions");
      if (!act) {
        act = document.createElement("div");
        act.className = "bubble-actions";
        bubble.appendChild(act);
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "bubble-copy tail-action";
      btn.textContent = label;
      btn.addEventListener("click", fn);
      act.insertBefore(btn, act.firstChild);
    };
    const users = messages.querySelectorAll(".bubble.user");
    const bots = messages.querySelectorAll(".bubble.assistant:not(.streaming)");
    mk(users[users.length - 1], "Edit", editLast);
    mk(bots[bots.length - 1], "Retry", regenerateLast);
  }

  function addBubble(role, text, record) {
    const near = atBottom();
    const div = document.createElement("div");
    div.className = "bubble " + role;
    renderText(div, text);
    if (role === "assistant" && String(text || "").trim()) addBubbleActions(div, text);
    messages.appendChild(div);
    if (near) scroll();
    syncToBottomBtn();
    if (role === "user" || role === "assistant") refreshTailActions();
    // Record durable bubbles so the conversation can be re-rendered on resume.
    // (record defaults to true; the transcript replay passes false.)
    if (record !== false && session && (role === "user" || role === "assistant" || role === "system")) {
      session.transcript = session.transcript || [];
      session.transcript.push({ role, text });
    }
    return div;
  }
  // Safe markdown rendering. Everything text-bearing goes through textContent /
  // createTextNode — no innerHTML with model output, so no HTML/script injection.
  // Handles fenced code, headings, lists, quotes, and inline emphasis/code/links.
  function renderText(container, text) {
    const parts = String(text == null ? "" : text).split(/```/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) renderCodeBlock(container, part);
      else if (part.trim()) renderBlocks(container, part);
    });
  }

  function renderCodeBlock(container, part) {
    const nl = part.indexOf("\n");
    const lang = nl >= 0 ? part.slice(0, nl).trim() : "";
    const code = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, "");
    const wrap = document.createElement("div");
    wrap.className = "codewrap";
    const bar = document.createElement("div");
    bar.className = "codebar";
    const tag = document.createElement("span");
    tag.className = "code-lang";
    tag.textContent = lang || "code";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy";
    btn.textContent = "Copy";
    btn.addEventListener("click", () => copyText(code, btn));
    bar.append(tag, btn);
    const pre = document.createElement("pre");
    pre.className = "code";
    pre.textContent = code;
    wrap.append(bar, pre);
    container.appendChild(wrap);
  }

  // Block level: headings, bullet/numbered lists, blockquotes, paragraphs.
  function renderBlocks(container, text) {
    const lines = String(text).split("\n");
    let list = null, ordered = false, para = [];
    const flushPara = () => {
      if (!para.length) return;
      const p = document.createElement("div");
      p.className = "para";
      // Soft-wrap, as markdown does: a model that hard-wraps its prose at 80
      // columns must not come out with ragged line breaks on a narrow phone.
      renderInline(p, para.join(" "));
      container.appendChild(p);
      para = [];
    };
    const flushList = () => { list = null; };
    for (const line of lines) {
      const t = line.trim();
      if (!t) { flushPara(); flushList(); continue; }
      const h = /^(#{1,4})\s+(.*)$/.exec(t);
      const bullet = /^[-*+]\s+(.*)$/.exec(t);
      const num = /^\d+[.)]\s+(.*)$/.exec(t);
      const quote = /^>\s?(.*)$/.exec(t);
      if (h) {
        flushPara(); flushList();
        const el = document.createElement("div");
        el.className = "mdh mdh" + h[1].length;
        renderInline(el, h[2]);
        container.appendChild(el);
      } else if (bullet || num) {
        flushPara();
        const isOrdered = !!num;
        if (!list || ordered !== isOrdered) {
          list = document.createElement(isOrdered ? "ol" : "ul");
          list.className = "mdlist";
          ordered = isOrdered;
          container.appendChild(list);
        }
        const li = document.createElement("li");
        renderInline(li, bullet ? bullet[1] : num[1]);
        list.appendChild(li);
      } else if (quote) {
        flushPara(); flushList();
        const q = document.createElement("div");
        q.className = "mdquote";
        renderInline(q, quote[1]);
        container.appendChild(q);
      } else { flushList(); para.push(t); }
    }
    flushPara();
  }

  // Inline: `code`, **bold**, *italic*, [label](url), and bare links.
  function renderInline(el, text) {
    const src = String(text);
    const rx = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\n]+)\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>]+)/g;
    let last = 0, m;
    while ((m = rx.exec(src))) {
      if (m.index > last) el.appendChild(document.createTextNode(src.slice(last, m.index)));
      if (m[1] != null) { const c = document.createElement("code"); c.textContent = m[1]; el.appendChild(c); }
      else if (m[2] != null) { const b = document.createElement("strong"); b.textContent = m[2]; el.appendChild(b); }
      else if (m[3] != null) { const i = document.createElement("em"); i.textContent = m[3]; el.appendChild(i); }
      else if (m[4] != null) el.appendChild(mdLink(m[5], m[4]));
      else if (m[6] != null) el.appendChild(mdLink(m[6], m[6]));
      last = rx.lastIndex;
    }
    if (last < src.length) el.appendChild(document.createTextNode(src.slice(last)));
  }
  // http(s) only — never javascript:/data:, whatever the model emits.
  function mdLink(href, label) {
    const a = document.createElement("a");
    a.href = /^https?:\/\//i.test(href) ? href : "#";
    a.textContent = label;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  }

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      haptic(8);
      if (btn) { const was = btn.textContent; btn.textContent = "Copied"; btn.classList.add("ok");
        setTimeout(() => { btn.textContent = was; btn.classList.remove("ok"); }, 1200); }
    } catch (e) { toast("Couldn't copy"); }
  }
  // Short, quiet taps on meaningful moments. Silently absent on iOS Safari.
  function haptic(ms) {
    if (!hapticsOn()) return;
    try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) {}
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
  // The wallpaper goes on <html> ONLY, never on #bg-layer as well.
  //
  // <html>'s background propagates to the root canvas, which is the one
  // surface that reaches the strip of screen below the layout viewport in an
  // installed iOS PWA — a position:fixed layer cannot, which was tried. If
  // #bg-layer painted the same image too, it would scale `cover` to its own
  // (viewport-sized) box while the canvas scaled to <html>'s taller box, and
  // the two would meet in a visible line just above the bottom of the screen.
  // That line was the reported "gradient offset". One painter, no seam.
  function applyBg(bg) {
    const layer = $("bg-layer");
    const root = document.documentElement;
    layer.classList.remove("image");
    document.body.classList.toggle("light-bg", bgIsLight(bg));
    // Assigning the `background` shorthand already resets background-image, so
    // there is nothing to clear afterwards — and clearing it unconditionally
    // wiped out the gradient the shorthand had just set, which is why the
    // gradient presets came out blank while photo wallpapers worked.
    const set = (el, css, img) => {
      el.style.background = css;
      if (img) el.style.backgroundImage = img;
    };
    set(layer, "");                       // never paints while a wallpaper is up
    if (!bg || bg.type === "default") {
      document.body.classList.remove("has-bg");
      set(root, "");
      return;
    }
    // body.has-bg turns body transparent so the canvas shows through it; while
    // body still had an opaque background it covered the canvas over the whole
    // viewport, leaving the canvas visible only in the strip. That split is
    // exactly what produced two differently-scaled copies of the image.
    document.body.classList.add("has-bg");
    if (bg.type === "image") {
      layer.classList.add("image");
      set(root, "#0b0d10 center/cover no-repeat", 'url("' + bg.value + '")');
    } else {
      set(root, bg.value);
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
  function hapticsOn() { return pref("mnm.haptics", "1") === "1"; }
  const READ_TOOL_NAMES = ["list_dir", "glob", "read_file", "grep", "search_code"];
  // view_image is read-only, and looking at a mockup is exactly a planning job,
  // so plan mode gets it too (session.readTools already wires the implementation).
  const READ_SCHEMAS = AC.TOOL_SCHEMAS.filter((s) => READ_TOOL_NAMES.includes(s.function.name))
    .concat([AC.VIEW_IMAGE_SCHEMA]);
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
    $("set-haptics").checked = hapticsOn();
    $("set-autolock").value = String(parseInt(pref("mnm.autolock", "15"), 10) || 0);
    $("set-keepsignedin").checked = keepSignedIn();
    $("set-sync").checked = syncOn();
    $("set-sync-pass-row").hidden = !syncOn();
    renderDiagnostics();
    $("settings-backdrop").hidden = false;
  }
  function closeSettings() { $("settings-backdrop").hidden = true; stopDiagnostics(); }

  // ---- diagnostics ----
  // Everything that has gone wrong on this screen -- the keyboard, the safe
  // areas, the strip under the layout viewport -- is invisible to any browser
  // that isn't this phone. Three attempts at the keyboard were guesses shipped
  // and checked by screenshot. These are the numbers that would have answered
  // it on the first try, so they are in the app now rather than in my head.
  let diagTimer = null;
  function buildDiagnostics() {
    const build = (document.querySelector('meta[name="mnm-build"]') || {}).content || "?";
    const rows = [
      ["build", build],
      ["standalone", String(!!(window.matchMedia("(display-mode: standalone)").matches ||
                               navigator.standalone))],
      ["platform", navigator.platform || "?"],
    ].concat(geometry());
    if (kbPeak) {
      rows.push(["", ""], ["while typing", kbPeak.at]);
      for (const [k, v] of kbPeak.rows) rows.push(["  " + k, v]);
    } else {
      rows.push(["", ""],
                ["while typing", "nothing recorded — type a character, then reopen this"]);
    }
    if (focusShove) {
      rows.push(["", ""], ["on focus", focusShove.at],
                ["  scrollY moved", focusShove.scrollY + "px"],
                ["  visual top moved", focusShove.vvTop + "px"],
                ["  html height moved", Math.round(focusShove.htmlD) + "px"]);
    } else {
      rows.push(["", ""], ["on focus", "nothing recorded yet"]);
    }
    return rows;
  }
  const diagText = () =>
    buildDiagnostics().map(([k, v]) => (k ? k + ": " + v : "")).join("\n");

  function renderDiagnostics() {
    const el = $("set-diag");
    if (!el) return;
    const paint = () => { el.textContent = diagText(); };
    paint();
    // Live, because the interesting values only exist while the keyboard is up
    // and you cannot read a static snapshot taken before you opened it.
    stopDiagnostics();
    diagTimer = setInterval(paint, 400);
  }
  function stopDiagnostics() {
    if (diagTimer) { clearInterval(diagTimer); diagTimer = null; }
  }
  $("btn-copy-diag").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(diagText()); toast("Diagnostics copied."); }
    catch (e) { toast("Couldn't copy — read them off the screen."); }
  });
  $("btn-repo-settings").addEventListener("click", openSettings);
  $("btn-chat-settings").addEventListener("click", openSettings);
  $("btn-chats-settings").addEventListener("click", openSettings);
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
  $("set-haptics").addEventListener("change", () => {
    localStorage.setItem("mnm.haptics", $("set-haptics").checked ? "1" : "0");
    haptic(12);   // confirm the setting with the thing itself
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

  // ---- sync settings + passphrase sheet ----
  $("set-sync").addEventListener("change", () => {
    const on = $("set-sync").checked;
    if (on && !hasSyncPass()) { $("set-sync").checked = false; openSyncPass(); return; }
    localStorage.setItem("mnm.sync", on ? "1" : "0");
    $("set-sync-pass-row").hidden = !on;
    if (!on && session) session.syncStore = null;
  });
  $("btn-change-syncpass").addEventListener("click", openSyncPass);
  function openSyncPass() {
    $("in-syncpass").value = ""; $("in-syncpass2").value = "";
    $("syncpass-error").textContent = "";
    $("syncpass-backdrop").hidden = false;
  }
  function closeSyncPass() { $("syncpass-backdrop").hidden = true; }
  $("btn-syncpass-cancel").addEventListener("click", closeSyncPass);
  $("syncpass-backdrop").addEventListener("click", (e) => { if (e.target === $("syncpass-backdrop")) closeSyncPass(); });
  $("btn-syncpass-save").addEventListener("click", async () => {
    const err = $("syncpass-error"); err.textContent = "";
    const p1 = $("in-syncpass").value, p2 = $("in-syncpass2").value;
    if (p1.length < 6) return (err.textContent = "Passphrase must be at least 6 characters.");
    if (p1 !== p2) return (err.textContent = "Passphrases don't match.");
    if (!session || !session.cryptoKey) return (err.textContent = "Unlock first.");
    $("btn-syncpass-save").disabled = true;
    try {
      // Verify the passphrase against the central store BEFORE storing it, so
      // a mismatch with another device is caught here rather than silently
      // forking the history (or, worse, bootstrapping a fresh empty store
      // under a typo'd passphrase that then "just works" with nothing in it).
      const { store } = await openCentralSync(p1);
      await storeSyncPass(p1);
      localStorage.setItem("mnm.sync", "1");
      session.syncStore = store;
      if (session.chatId) await syncSave();   // catch up an in-progress chat
      closeSyncPass();
      $("set-sync").checked = true;
      $("set-sync-pass-row").hidden = false;
      toast("Sync enabled.");
    } catch (e) { err.textContent = friendlyGhError(e, "list"); }
    finally { $("btn-syncpass-save").disabled = false; }
  });

  function registerSW() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("sw.js").then((reg) => {
      // Ask whether there's a new build whenever the app comes back to the
      // foreground. Without this, an installed PWA only looks on a fresh
      // navigation — so one left open for days keeps running old code, and the
      // only way out is knowing to close and reopen it. Nobody should have to
      // know that the app has a cache.
      const check = () => {
        if (document.visibilityState === "visible") reg.update().catch(() => {});
      };
      document.addEventListener("visibilitychange", check);
      window.addEventListener("focus", check);
    }).catch(() => {});
    // When a new SW takes control (a fresh deploy), reload once so the page runs
    // the new code. Guarded on an existing controller so a first install doesn't loop.
    if (navigator.serviceWorker.controller) {
      let refreshing = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (refreshing) return;
        refreshing = true;
        // Never mid-turn. The agent's reply is in flight and lives only in this
        // page; reloading now throws away work the user is waiting for, and a
        // deploy landing at that moment is pure bad luck they didn't cause.
        // Wait for the turn to finish — the new code is one reload away either
        // way, and this one can be a quiet one.
        const whenIdle = () => {
          if (currentRun || composing) return setTimeout(whenIdle, 1000);
          location.reload();
        };
        whenIdle();
      });
    }
  }

  // ================================================================ PAIRING
  // Arriving from the desktop's QR: the sealed payload is in the URL fragment,
  // which the browser never sends to the server — so the keys got here without
  // touching the network. Take it out of the URL before anything else, so it
  // can't linger in history, a bookmark, or a shared screenshot of the address
  // bar. The pairing code is NOT in the link; it's the six characters shown on
  // the computer, which is what makes a captured QR useless on its own.
  let pendingSyncPass = "";

  // Ask for the code and fill the setup form in. Shared by both ways a token
  // can arrive: scanned in-app (the good path) or carried in the URL.
  async function applyPairToken(token) {
    for (;;) {
      // window.prompt explicitly: `prompt` is the composer textarea in this
      // scope (const prompt = $("in-prompt")), so a bare call reaches a DOM
      // element and throws — which broke pairing entirely, by both routes.
      const code = window.prompt(
        "Setting up from your computer.\n\n" +
        "Type the 6-character pairing code shown next to the QR code.");
      if (code === null) { toast("Set-up cancelled — you can still type your keys in."); return false; }
      try {
        const data = await AC.openPairToken(token, code);
        if (data.modelKey) $("in-model-key").value = data.modelKey;
        if (data.githubToken) $("in-gh-token").value = data.githubToken;
        if (data.model) $("in-model").value = data.model;
        if (data.baseUrl) {
          const sel = $("in-base-url");
          if ([...sel.options].some((o) => o.value === data.baseUrl)) sel.value = data.baseUrl;
        }
        // Sync needs the vault key, which doesn't exist until a PIN is set —
        // so hold it and turn sync on once the vault is unlocked.
        pendingSyncPass = data.syncPass || "";
        haptic(14);
        toast(pendingSyncPass
          ? "Keys filled in from your computer. Pick a PIN and your chats will sync too."
          : "Keys filled in from your computer. Now pick a PIN.");
        return true;
      } catch (e) {
        // Wrong or stale code: say which, and let them try again.
        if (!confirm((e && e.message ? e.message : e) + "\n\nTry again?")) return false;
      }
    }
  }

  // A token can also arrive in the URL, from following the QR link in a
  // browser. That works, but on iOS a home-screen app has its own storage, so
  // anything paired that way stays in the browser and never reaches the
  // installed app — which is why the in-app scanner exists and is offered first.
  async function consumePairLink() {
    const m = /[#&]pair=([A-Za-z0-9_-]+)/.exec(location.hash || "");
    if (!m) return;
    const token = m[1];
    // Out of the URL before anything else, so it can't linger in history or a
    // bookmark. Keeping it would also mean re-pairing on every launch.
    history.replaceState(null, "", location.pathname + location.search);
    if (loadVault()) {
      // Previously this did nothing at all here, so scanning just showed the
      // PIN prompt with no explanation. Ask instead of silently ignoring.
      if (!confirm("This device already has keys set up.\n\n" +
                   "Replace them with the ones from your computer?")) return;
      clearVault(); clearSession(); localStorage.removeItem(KEEPKEY_KEY);
      show("screen-setup");
    }
    await applyPairToken(token);
  }

  // ---- in-app scanner ----
  // Uses the platform decoder when there is one (Chrome/Android) and falls back
  // to a bundled decoder, because Safari has no BarcodeDetector — and iOS is
  // exactly where scanning in-app matters most.
  let scanStop = null;
  let jsQRLoading = null;
  function loadJsQR() {
    if (window.jsQR) return Promise.resolve(window.jsQR);
    if (jsQRLoading) return jsQRLoading;
    jsQRLoading = new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "vendor/jsQR.js";           // same-origin, so CSP script-src 'self' allows it
      s.onload = () => res(window.jsQR);
      s.onerror = () => rej(new Error("Couldn't load the QR decoder."));
      document.head.appendChild(s);
    });
    return jsQRLoading;
  }

  async function startScan() {
    const back = $("scan-backdrop"), video = $("scan-video"), status = $("scan-status");
    status.textContent = "Starting the camera…";
    back.hidden = false;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } }, audio: false });
    } catch (e) {
      back.hidden = true;
      toast(e && e.name === "NotAllowedError"
        ? "Camera access was denied — allow it, or type your keys in below."
        : "No camera available here — type your keys in below.");
      return;
    }
    video.srcObject = stream;
    try { await video.play(); } catch (e) { /* autoplay attr covers most cases */ }

    let detector = null;
    try {
      if (window.BarcodeDetector) detector = new BarcodeDetector({ formats: ["qr_code"] });
    } catch (e) { detector = null; }
    let decode = null;
    if (!detector) {
      status.textContent = "Getting ready…";
      try { await loadJsQR(); } catch (e) { stop(); toast(e.message); return; }
    }
    status.textContent = "Point at the QR code on your computer.";

    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    let raf = 0, busy = false, done = false;

    function stop() {
      done = true;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
      video.srcObject = null;
      back.hidden = true;
      scanStop = null;
    }
    scanStop = stop;

    async function frame() {
      raf = 0;
      if (done) return;
      if (!busy && video.videoWidth) {
        busy = true;
        try {
          // Cap the working size: a 4K frame costs far more to scan than it
          // adds in detail, and the phone has to do this every frame.
          const scale = Math.min(1, 720 / video.videoWidth);
          canvas.width = Math.round(video.videoWidth * scale);
          canvas.height = Math.round(video.videoHeight * scale);
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          let text = "";
          // ONLY the decode is allowed to fail quietly — an unreadable frame is
          // completely normal. Everything after it is real work, and swallowing
          // an error there would leave the scanner dead with nothing said.
          try {
            if (detector) {
              const hits = await detector.detect(canvas);
              text = hits && hits.length ? hits[0].rawValue : "";
            } else {
              const d = ctx.getImageData(0, 0, canvas.width, canvas.height);
              const r = window.jsQR(d.data, d.width, d.height);
              text = r ? r.data : "";
            }
          } catch (e) { text = ""; }
          if (text) {
            const m = /[#&]pair=([A-Za-z0-9_-]+)/.exec(text);
            if (m) { stop(); haptic(20); await applyPairToken(m[1]); return; }
            // A QR that isn't ours: say so rather than looking broken.
            status.textContent = "That's a QR code, but not a set-up code from your computer.";
          }
        } catch (e) {
          stop();
          toast("Set-up failed: " + (e && e.message ? e.message : e));
          return;
        }
        busy = false;
      }
      if (!done) raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
  }

  $("btn-scan-setup").addEventListener("click", startScan);
  $("scan-cancel").addEventListener("click", () => { if (scanStop) scanStop(); });

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
    // After the screen is up, so prefilled fields are visible behind the prompt.
    await consumePairLink();
  }
  boot();
})();

/* GLM Code desktop frontend */
"use strict";

// A missing element (stale cached HTML from a WebView2 profile that predates
// a markup change, a future id typo, etc.) must never throw and abort the
// rest of this script -- that would silently kill the pywebviewready
// listener registered at the bottom and freeze the whole app on launch with
// no visible error. Fall back to an inert stub instead of null.
const NOOP_EL = () => new Proxy(function () {}, {
  get(_, prop) {
    if (prop === "classList") return { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
    if (prop === "style" || prop === "dataset") return {};
    if (prop === "value" || prop === "textContent" || prop === "title") return "";
    if (prop === "checked" || prop === "hidden" || prop === "disabled") return false;
    return () => {};
  },
  set() { return true; },
});
const $ = (id) => {
  const el = document.getElementById(id);
  if (!el) console.warn(`#${id} not found in DOM -- ignoring`);
  return el || NOOP_EL();
};
const chatEl = $("chat");

// Generic file icon used for non-image attachments, which have no real
// thumbnail (see pick_files/renderAttachments/addUserMessage).
const FILE_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 ' +
  '2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

/* ------------------------------------------------ utilities */

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ------------------------------------------------ syntax highlighting
   Dependency-free: comments/strings are matched per language family, then
   keywords/numbers are picked out of the plain segments. Everything is
   escaped piece-by-piece as it's emitted (never regexed after escaping, so
   entities like &quot; can't get mangled). Unknown languages fall back to
   strings+numbers only -- safe for logs and shell output. */

const HL_KEYWORDS = {
  c: new Set(("break case catch class const continue debugger default delete do else enum export extends " +
    "finally fn for func function if impl implements import in instanceof interface let loop match mod new " +
    "of package private public pub return static struct super switch this throw trait try type typeof use " +
    "var void while with yield async await true false null undefined nil None").split(" ")),
  py: new Set(("and as assert async await break class continue def del elif else except finally for from " +
    "global if import in is lambda nonlocal not or pass raise return try while with yield " +
    "True False None self match case").split(" ")),
  sh: new Set(("if then else elif fi for while do done case esac function return local export echo " +
    "set in break continue exit param begin process end foreach").split(" ")),
};
// [comment/string style, keyword set] per language; style keys below.
const HL_LANGS = {
  python: ["py", "py"], py: ["py", "py"],
  javascript: ["c", "c"], js: ["c", "c"], jsx: ["c", "c"], ts: ["c", "c"], tsx: ["c", "c"],
  typescript: ["c", "c"], java: ["c", "c"], c: ["c", "c"], cpp: ["c", "c"], h: ["c", "c"],
  cs: ["c", "c"], go: ["c", "c"], rs: ["c", "c"], rust: ["c", "c"], kt: ["c", "c"],
  swift: ["c", "c"], php: ["c", "c"], css: ["c", null], scss: ["c", null],
  sh: ["hash", "sh"], bash: ["hash", "sh"], zsh: ["hash", "sh"], shell: ["hash", "sh"],
  powershell: ["hash", "sh"], ps1: ["hash", "sh"], yaml: ["hash", null], yml: ["hash", null],
  toml: ["hash", null], ruby: ["hash", "py"], rb: ["hash", "py"],
  html: ["html", null], xml: ["html", null], json: ["none", null],
};
const HL_TOKEN_RES = {
  c: /(\/\*[\s\S]*?(?:\*\/|$)|\/\/[^\n]*|`(?:\\[\s\S]|[^\\`])*`?|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)/,
  hash: /(#[^\n]*|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)/,
  py: /("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|#[^\n]*|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)/,
  html: /(<!--[\s\S]*?(?:-->|$)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)/,
  none: /("(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)/,
};

function hlPlain(seg, kwset) {
  let out = "";
  let last = 0;
  const re = /[A-Za-z_$][\w$]*|\d[\d_]*(?:\.\d+)?/g;
  let m;
  while ((m = re.exec(seg))) {
    out += esc(seg.slice(last, m.index));
    const t = m[0];
    if (kwset && kwset.has(t)) out += `<span class="hl-kw">${t}</span>`;
    else if (/^\d/.test(t)) out += `<span class="hl-num">${t}</span>`;
    else out += esc(t);
    last = m.index + t.length;
  }
  return out + esc(seg.slice(last));
}

function highlight(code, lang) {
  const [style, kwName] = HL_LANGS[(lang || "").toLowerCase()] || ["none", null];
  const kwset = kwName ? HL_KEYWORDS[kwName] : null;
  const re = new RegExp(HL_TOKEN_RES[style].source, "g");
  let out = "";
  let last = 0;
  let m;
  while ((m = re.exec(code))) {
    out += hlPlain(code.slice(last, m.index), kwset);
    const t = m[0];
    const isComment = t.startsWith("//") || t.startsWith("/*")
      || t.startsWith("#") || t.startsWith("<!--");
    out += `<span class="${isComment ? "hl-com" : "hl-str"}">${esc(t)}</span>`;
    last = m.index + t.length;
    if (m.index === re.lastIndex) re.lastIndex++; // never stall on empty match
  }
  return out + hlPlain(code.slice(last), kwset);
}

// Something that plausibly names a file on disk: optional drive/segments and
// a real extension, optionally with a trailing :line(:col). Deliberately NOT
// matching bare words -- false positives make every identifier look a link.
const PATHISH_RE = /^(?:[A-Za-z]:[\\/])?[\w.~-]+(?:[\\/][\w.~-]+)*\.[A-Za-z0-9]{1,8}(?::\d+(?::\d+)?)?$/;

document.addEventListener("click", async (e) => {
  const el = e.target.closest("code.maybe-path");
  if (!el) return;
  const path = el.textContent.replace(/:\d+(?::\d+)?$/, ""); // strip :line(:col)
  try {
    const res = await api().open_path(path);
    if (res && res.error && res.error !== "not found") toast(res.error, "error", 5000);
    if (res && res.error === "not found") toast(`Not found: ${path}`, "warn", 3500);
  } catch (err) { /* bridge unavailable; ignore */ }
});

/* Minimal markdown renderer (safe: escapes first, then adds structure). */
function md(src) {
  const codeBlocks = [];
  src = String(src ?? "");
  // fenced code blocks out first
  src = src.replace(/```([\w+-]*)\n?([\s\S]*?)(?:```|$)/g, (_, lang, code) => {
    codeBlocks.push(`<pre><code data-lang="${esc(lang)}">${highlight(code.replace(/\n$/, ""), lang)}</code></pre>`);
    return `\u0000${codeBlocks.length - 1}\u0000`;
  });
  src = esc(src);
  // inline code; file-looking spans become click-to-open (see the document
  // click handler -- the backend only acts if the path actually exists)
  src = src.replace(/`([^`\n]+)`/g, (_, c) =>
    PATHISH_RE.test(c)
      ? `<code class="maybe-path" title="Click to open">${c}</code>`
      : `<code>${c}</code>`);
  // links (escaped text: [t](url))
  src = src.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    (_, t, u) => `<a data-href="${u}">${t}</a>`);
  // bold / italic
  src = src.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");

  const lines = src.split("\n");
  const out = [];
  let list = null; // "ul" | "ol"
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of lines) {
    const h = line.match(/^(#{1,4})\s+(.*)/);
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (h) { closeList(); out.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`); }
    else if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { closeList(); out.push("<hr>"); }
    else if (ul) { if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${ul[1]}</li>`); }
    else if (ol) { if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${ol[1]}</li>`); }
    else if (/^\s*&gt;\s?/.test(line)) { closeList(); out.push(`<blockquote>${line.replace(/^\s*&gt;\s?/, "")}</blockquote>`); }
    else if (line.trim() === "") { closeList(); out.push(""); }
    else { closeList(); out.push(`<p>${line}</p>`); }
  }
  closeList();
  let html = out.join("\n").replace(/(<\/p>)\n(<p>)/g, "$1$2");
  html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => codeBlocks[+i]);
  return html;
}

function colorDiff(text) {
  return esc(text).split("\n").map((l) => {
    if (l.startsWith("+") && !l.startsWith("+++")) return `<span class="dadd">${l}</span>`;
    if (l.startsWith("-") && !l.startsWith("---")) return `<span class="ddel">${l}</span>`;
    if (l.startsWith("@@")) return `<span class="dhunk">${l}</span>`;
    return l;
  }).join("\n");
}

function nearBottom() {
  return chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 140;
}
// Follow-the-stream state is TRACKED from scroll events rather than measured
// inside scrollDown(): measuring after the DOM just grew means any single
// render that adds more height than the threshold (a big markdown block, a
// batched sub-agent chunk) instantly "unpins" the view and the chat stops
// following -- the classic broken-auto-scroll bug.
let chatPinned = true;
function scrollDown(force) {
  // behavior:"instant" on purpose (NOT "auto" -- per the CSSOM spec "auto"
  // defers to the CSS scroll-behavior property, which is smooth here): the
  // smooth animation, restarted a dozen times a second during streaming,
  // never catches up and reads as lag. Smooth is kept for user-initiated
  // jumps only.
  if (force || chatPinned) chatEl.scrollTo({ top: chatEl.scrollHeight, behavior: "instant" });
}

// Floating "jump to bottom" button: appears whenever the user has scrolled
// up out of the auto-follow zone (streaming never yanks them back down --
// scrollDown() no-ops while unpinned).
chatEl.addEventListener("scroll", () => {
  chatPinned = nearBottom();
  $("jump-bottom").hidden = chatPinned;
}, { passive: true });
$("jump-bottom").addEventListener("click", () => {
  chatPinned = true;
  chatEl.scrollTo({ top: chatEl.scrollHeight, behavior: "smooth" });
  $("jump-bottom").hidden = true;
});

function toast(text, level = "info", ms = 4200) {
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  t.textContent = text;
  $("toasts").appendChild(t);
  setTimeout(() => { t.classList.add("fade"); setTimeout(() => t.remove(), 600); }, ms);
}

function fmtTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

const api = () => window.pywebview.api;

/* ------------------------------------------------ tool icons */
const ICONS = {
  read_file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
  write_file: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  edit_file: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  list_dir: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  glob: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  grep: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  find_references: '<circle cx="12" cy="12" r="10"/><line x1="22" y1="12" x2="18" y2="12"/><line x1="6" y1="12" x2="2" y2="12"/><line x1="12" y1="6" x2="12" y2="2"/><line x1="12" y1="22" x2="12" y2="18"/>',
  run_powershell: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  todo_write: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  web_search: '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  fetch_url: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  view_image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  generate_image: '<path d="M12 3l1.8 4.2L18 9l-4.2 1.8L12 15l-1.8-4.2L6 9l4.2-1.8z"/><path d="M19 15l.7 1.7L21.5 17.5l-1.8.8-.7 1.7-.7-1.7-1.8-.8 1.8-.8z"/>',
  show_image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  compact_context: '<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/>',
  speak: '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>',
};
function toolIcon(name) {
  const p = ICONS[name] || '<circle cx="12" cy="12" r="9"/>';
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
}
function toolSummary(name, args) {
  switch (name) {
    case "read_file": case "write_file": case "edit_file": return args.path || "";
    case "run_powershell": return args.command || "";
    case "grep": return `/${args.pattern || ""}/` + (args.glob ? ` ${args.glob}` : "");
    case "find_references": return args.symbol || "";
    case "glob": return args.pattern || "";
    case "list_dir": return args.path || ".";
    case "web_search": return args.query || "";
    case "fetch_url": return args.url || "";
    case "todo_write": return `${(args.todos || []).length} items`;
    case "spawn_agents": {
      const a = args.agents || [];
      return `${a.length} agent${a.length === 1 ? "" : "s"}: ` +
        a.map((x) => x.name || "?").join(", ");
    }
    case "view_image":
      return (args.path || "") + (args.question ? ` — ${args.question}` : "");
    case "generate_image":
      return args.prompt || "";
    case "show_image":
      return (args.path || "") + (args.caption ? ` — ${args.caption}` : "");
    case "compact_context":
      return args.reason || "";
    case "speak":
      return args.text || "";
    default: return "";
  }
}

/* ------------------------------------------------ shared tool / todo builders */

function buildToolEl(name, args) {
  const el = document.createElement("div");
  el.className = "tool running";
  el.innerHTML =
    `<button class="tool-head" aria-expanded="false">` +
    `<span class="tool-ico">${toolIcon(name)}</span>` +
    `<span class="tool-name">${esc(name)}</span>` +
    `<span class="tool-sum">${esc(toolSummary(name, args || {}))}</span>` +
    `<span class="tool-state">running</span></button>` +
    `<div class="tool-body"></div>`;
  el.querySelector(".tool-head").addEventListener("click", () => {
    el.classList.toggle("open");
    el.querySelector(".tool-head").setAttribute("aria-expanded", el.classList.contains("open"));
  });
  return el;
}

function finishToolEl(el, content, isError) {
  el.classList.remove("running");
  if (isError) el.classList.add("error");
  el.querySelector(".tool-state").textContent = isError ? "error" : "done";
  const body = el.querySelector(".tool-body");
  const c = content || "(empty)";
  body.innerHTML = /^(---|\+\+\+|@@)/m.test(c) ? colorDiff(c) : esc(c);
  if (isError) el.classList.add("open");
}

function todosHtml(items) {
  const check = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  return "<h4>Tasks</h4>" + items.map((it) => {
    const cls = it.status === "completed" ? "done" : it.status === "in_progress" ? "active" : "";
    return `<div class="todo ${cls}"><span class="todo-box">${it.status === "completed" ? check : ""}</span><span>${esc(it.content)}</span></div>`;
  }).join("");
}

/* ------------------------------------------------ chat rendering state */

let current = null;       // in-progress assistant turn state
let renderQueued = false;

function hideWelcome() {
  const w = $("welcome");
  if (w) w.hidden = true;
}

function addUserMessage(text, images, note, plan) {
  hideWelcome();
  $("empty-hint").hidden = true;
  const wrap = document.createElement("div");
  wrap.className = "msg msg-user";
  const b = document.createElement("div");
  b.className = "bubble-user";
  b.textContent = text;
  if (plan) {
    const tag = document.createElement("span");
    tag.className = "plan-badge";
    tag.textContent = "PLAN";
    b.prepend(tag);
  }
  if (images && images.length) {
    const strip = document.createElement("div");
    strip.className = "img-strip";
    for (const img of images) {
      if (img.thumb) {
        const el = document.createElement("img");
        el.src = img.thumb;
        el.alt = img.name;
        strip.appendChild(el);
      } else {
        const chip = document.createElement("span");
        chip.className = "file-chip";
        chip.title = img.name;
        chip.innerHTML = FILE_ICON_SVG + `<span>${esc(img.name)}</span>`;
        strip.appendChild(chip);
      }
    }
    b.appendChild(strip);
  }
  if (note) {
    const n = document.createElement("div");
    n.className = "user-note";
    n.textContent = note;
    b.appendChild(n);
  }
  wrap.appendChild(b);
  chatEl.appendChild(wrap);
  scrollDown(true);
}

function handleBackgroundEvent(ev) {
  switch (ev.type) {
    case "chat_busy":
      busySessions.add(ev.sid);
      renderSidebar();
      break;
    case "turn_complete": {
      busySessions.delete(ev.sid);
      unreadSessions.add(ev.sid);
      if (ev.sessions) sessions = ev.sessions;
      renderSidebar();
      const name = ev.title || "A background chat";
      toast(ev.ok ? `${name} finished.` : `${name} hit an error.`,
            ev.ok ? "info" : "warn", 6000);
      break;
    }
    case "permission":
      // Don't pop a sheet for a chat the user isn't looking at -- hold it
      // and surface it the moment they switch to that chat.
      pendingPerms[ev.sid] = ev;
      unreadSessions.add(ev.sid);
      renderSidebar();
      toast("A background chat is waiting for permission.", "warn", 6000);
      break;
    // Everything else (stream deltas, tool chips, notices...) is rebuilt
    // from the agent's message history when the user switches to that chat.
  }
}

/* Replays a saved conversation (from sessions.py to_display) into #chat. */
function renderHistory(items, todos) {
  let wrap = null;
  const ensureWrap = () => { if (!wrap) wrap = newAssistantBlock(); return wrap; };
  for (const it of items || []) {
    if (it.kind === "user") {
      wrap = null;
      const imgs = (it.images || []).map((src) => ({ thumb: src, name: "image" }));
      const note = it.described && imgs.length === 0 ? "Image described for the agent" : "";
      addUserMessage(it.text || (it.described ? "(image attached)" : ""), imgs, note, !!it.plan);
    } else if (it.kind === "assistant") {
      const w = ensureWrap();
      const b = document.createElement("div");
      b.className = "bubble-assistant";
      const c = document.createElement("div");
      c.className = "md";
      c.innerHTML = md(it.text);
      b.appendChild(c);
      w.appendChild(b);
    } else if (it.kind === "tool") {
      const w = ensureWrap();
      const el = buildToolEl(it.name, it.args || {});
      finishToolEl(el, it.result || "", !!it.error);
      w.appendChild(el);
    } else if (it.kind === "compacted") {
      ensureWrap().appendChild(buildCompactedEl(it.summary || ""));
    } else if (it.kind === "steered") {
      ensureWrap().appendChild(buildSteeredEl(it.text || ""));
    } else if (it.kind === "tool_image") {
      ensureWrap().appendChild(buildImageCard(it.src || "", it.caption || "", it.path || ""));
    } else if (it.kind === "tool_audio") {
      ensureWrap().appendChild(buildAudioCard(it.src || "", it.caption || "", it.path || ""));
    } else if (it.kind === "note") {
      const w = ensureWrap();
      const n = document.createElement("div");
      n.className = "compact-note";
      n.textContent = it.text;
      w.appendChild(n);
    }
  }
  if (todos && todos.length) {
    const card = document.createElement("div");
    card.id = "todos-card";
    card.className = "todos";
    card.innerHTML = todosHtml(todos);
    ensureWrap().appendChild(card);
  }
  scrollDown(true);
}

function newAssistantBlock() {
  hideWelcome();
  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant";
  chatEl.appendChild(wrap);
  return wrap;
}

function ensureTurn() {
  if (!current) current = { wrap: newAssistantBlock() };
  return current;
}

function flushThinkPending(t) {
  if (t && t.thinkNode && t.reasonPending) {
    t.thinkNode.appendData(t.reasonPending);
    t.reasonPending = "";
    if (t.thinking.open) t.thinkBody.scrollTop = t.thinkBody.scrollHeight;
  }
}

function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  setTimeout(() => {
    renderQueued = false;
    if (!current) return;
    if (current.contentEl && current.mdDirty) {
      current.contentEl.innerHTML = md(current.content);
      current.mdDirty = false;
    }
    flushThinkPending(current);
    scrollDown();
  }, 80);
}

/* ------------------------------------------------ event sink from Python */

window.GLM = {
  emit(ev) {
    try { handle(ev); } catch (e) { console.error(e, ev); }
  },
};

/* Chats keep running when the user switches away; every event carries the
   sid of the chat it belongs to. Events for the ACTIVE chat render live;
   background chats only update the sidebar (spinner, unread, permission
   badge) -- their full state is rebuilt from agent.messages on switch. */
const busySessions = new Set();
const unreadSessions = new Set();
const pendingPerms = {};   // sid -> the permission event waiting for that chat

function handle(ev) {
  if (ev.sid && ev.sid !== activeSessionId) {
    handleBackgroundEvent(ev);
    return;
  }
  switch (ev.type) {
    case "chat_busy": {
      busySessions.add(ev.sid);
      setBusy(true);
      renderSidebar();
      return;
    }
    case "turn_complete": {
      busySessions.delete(ev.sid);
      setBusy(false);
      $("status-chip").hidden = true;
      current = null;
      if (ev.ok) {
        updateUsage(ev.prompt_tokens, ev.completion_tokens, ev.context);
        if (ev.sessions) { sessions = ev.sessions; renderSidebar(); }
        if (ev.plan) $("plan-actions").hidden = false;
        showTurnChanges();
      }
      renderSidebar();
      return;
    }
  }
  switch (ev.type) {
    case "stream_start": {
      // NOTE: this fires once per agentic LLM round-trip, not once per
      // visible user turn -- a turn that uses tools mid-way gets several
      // stream_start/stream_end pairs. Reasoning/thinking state is
      // deliberately NOT reset here (see the "reasoning" case below): it
      // used to reset t.reasoning/t.thinking while a render flush from the
      // PREVIOUS round was still pending in queueRender()'s debounce,
      // which then wrote the now-empty t.reasoning into the old (still
      // visible, possibly user-expanded) thinking panel -- silently
      // wiping text the user was reading. Only content is per-round.
      const t = ensureTurn();
      t.content = "";
      t.contentEl = null;
      break;
    }
    case "reasoning": {
      const t = ensureTurn();
      if (!t.thinking || !t.thinking.classList.contains("active")) {
        // Either the first reasoning burst of the turn, or reasoning resumed
        // in a later agentic round after a tool call. Start a FRESH box
        // rather than reactivating the old one: the old box's DOM position
        // is fixed wherever it was first inserted (before any tool chips
        // that have since been appended), so writing new text into it would
        // visually read as if all the reasoning happened up front, before
        // any of the tool calls -- not interleaved with them as it actually
        // happened.
        t.thinking = document.createElement("details");
        t.thinking.className = "thinking active";
        t.thinking.innerHTML =
          `<summary><span class="think-dot"></span>Thinking…</summary>` +
          `<div class="thinking-body"></div>`;
        t.thinkBody = t.thinking.querySelector(".thinking-body");
        // One persistent text node, grown with appendData(delta): rewriting
        // the whole reasoning string via textContent on every flush was
        // O(total length) per flush -- quadratic over a long burst, and GLM
        // reasons a LOT.
        t.thinkNode = document.createTextNode("");
        t.thinkBody.appendChild(t.thinkNode);
        t.reasonPending = "";
        t.wrap.appendChild(t.thinking);
        scrollDown();
      }
      t.reasonPending += ev.text;
      queueRender();
      break;
    }
    case "content": {
      const t = ensureTurn();
      if (t.thinking && t.thinking.classList.contains("active")) {
        t.thinking.classList.remove("active");
        t.thinking.querySelector("summary").innerHTML =
          `<span class="think-dot"></span>Thought process`;
      }
      if (!t.contentEl) {
        const b = document.createElement("div");
        b.className = "bubble-assistant";
        t.contentEl = document.createElement("div");
        t.contentEl.className = "md";
        b.appendChild(t.contentEl);
        t.wrap.appendChild(b);
      }
      t.content += ev.text;
      t.mdDirty = true;
      queueRender();
      break;
    }
    case "stream_end": {
      if (current) {
        if (current.thinking) {
          flushThinkPending(current);
          current.thinking.classList.remove("active");
          current.thinking.querySelector("summary").innerHTML =
            `<span class="think-dot"></span>Thought process`;
        }
        if (current.contentEl) {
          current.contentEl.innerHTML = md(current.content);
        } else if (current.content === "" && !current.wrap.hasChildNodes()) {
          current.wrap.remove();
          current = null;
          break;
        }
        // keep wrap for tool chips that may follow
        current.contentEl = null;
        current.content = "";
      }
      scrollDown();
      break;
    }
    case "tool_call": {
      const t = ensureTurn();
      const el = buildToolEl(ev.name, ev.args || {});
      t.wrap.appendChild(el);
      t.lastTool = el;
      scrollDown();
      break;
    }
    case "tool_result": {
      const t = current;
      const el = t && t.lastTool;
      if (!el) break;
      if (SILENT_TOOLS.has(ev.name) && !ev.error) {
        // The real result is a "show_image" event (already rendered, or
        // about to be); the generic chip was just a running-state placeholder.
        el.remove();
      } else {
        finishToolEl(el, ev.content, !!ev.error);
      }
      scrollDown();
      break;
    }
    case "todos": {
      const t = ensureTurn();
      let card = document.getElementById("todos-card");
      if (!card) {
        card = document.createElement("div");
        card.id = "todos-card";
        card.className = "todos";
      }
      t.wrap.appendChild(card); // moves to latest position
      card.innerHTML = todosHtml(ev.items);
      scrollDown();
      break;
    }
    case "notice": {
      if (ev.level === "info") toast(ev.text, "info", 3000);
      else toast(ev.text, ev.level, ev.level === "error" ? 8000 : 5000);
      const t = ensureTurn();
      t.wrap.appendChild(buildNoticeEl(ev.level, ev.text || ""));
      scrollDown();
      break;
    }
    case "status": {
      $("status-chip").hidden = !ev.active;
      $("status-label").textContent = ev.label || "";
      break;
    }
    case "permission": showPermission(ev); break;
    case "turn_done": {
      updateUsage(ev.prompt_tokens, ev.completion_tokens, ev.context);
      break;
    }
    case "compacted": {
      const t = ensureTurn();
      t.wrap.appendChild(buildCompactedEl(ev.summary || ""));
      scrollDown();
      break;
    }
    case "subagent": {
      const t = ensureTurn();
      updateSubagent(t, ev);
      scrollDown();
      break;
    }
    case "steered": {
      const t = ensureTurn();
      t.wrap.appendChild(buildSteeredEl(ev.text || ""));
      scrollDown();
      clearSteerQueued();
      break;
    }
    case "steer_returned": {
      // The queued message never got delivered (the turn ended first) --
      // put it back where the user typed it instead of losing it or
      // silently carrying it into some later, unrelated turn.
      restoreSteerText(ev.text || "");
      clearSteerQueued();
      toast("Your steering message wasn't delivered in time -- it's back in the composer.", "info", 4500);
      break;
    }
    case "subagent_stream": {
      renderSubagentEvent(ev.id, ev);
      break;
    }
    case "show_image": {
      const t = ensureTurn();
      t.wrap.appendChild(buildImageCard(ev.src || "", ev.caption || "", ev.path || ""));
      scrollDown();
      break;
    }
    case "show_audio": {
      const t = ensureTurn();
      t.wrap.appendChild(buildAudioCard(ev.src || "", ev.caption || "", ev.path || ""));
      scrollDown();
      break;
    }
    case "play_audio": handlePlayAudio(ev); break;
    case "tts_reset": resetTtsPlayback(); break;
  }
}

// Tool calls whose real result is a dedicated visual event, not the generic
// tool-chip text (see the "show_image"/"show_audio" events and their card
// builders below).
const SILENT_TOOLS = new Set(["generate_image", "show_image", "speak"]);

/* ------------------------------------------------ read-aloud audio queue */
// Chunks arrive as they finish synthesizing (server-side worker is strictly
// serial, but we still key on `seq` here as a defensive guarantee against
// any future reordering) and get queued for strict back-to-back playback.

let ttsExpectedSeq = 1;
let ttsPending = {};
let ttsAudioQueue = [];
let ttsPlaying = false;
let ttsCurrentAudio = null;

function resetTtsPlayback() {
  ttsExpectedSeq = 1;
  ttsPending = {};
  ttsAudioQueue = [];
  ttsPlaying = false;
  if (ttsCurrentAudio) {
    try { ttsCurrentAudio.pause(); } catch (e) { /* ignore */ }
    ttsCurrentAudio = null;
  }
}

function handlePlayAudio(ev) {
  if (ev.error) {
    console.warn("TTS error:", ev.error);
    ttsPending[ev.seq] = null;
  } else {
    ttsPending[ev.seq] = ev.src;
  }
  drainTtsQueue();
}

function drainTtsQueue() {
  while (Object.prototype.hasOwnProperty.call(ttsPending, ttsExpectedSeq)) {
    const src = ttsPending[ttsExpectedSeq];
    delete ttsPending[ttsExpectedSeq];
    ttsExpectedSeq++;
    if (src) enqueueTtsPlayback(src);
  }
}

function enqueueTtsPlayback(src) {
  ttsAudioQueue.push(src);
  if (!ttsPlaying) playNextTts();
}

function playNextTts() {
  if (ttsAudioQueue.length === 0) { ttsPlaying = false; ttsCurrentAudio = null; return; }
  ttsPlaying = true;
  const src = ttsAudioQueue.shift();
  const audio = new Audio(src);
  ttsCurrentAudio = audio;
  audio.addEventListener("ended", playNextTts);
  audio.addEventListener("error", playNextTts);
  audio.play().catch(() => playNextTts());
}

function buildImageCard(src, caption, path) {
  const card = document.createElement("div");
  card.className = "image-card";
  if (src) {
    const img = document.createElement("img");
    img.src = src;
    img.alt = caption || path || "image";
    card.appendChild(img);
  } else {
    const missing = document.createElement("div");
    missing.className = "image-card-missing";
    missing.textContent = `(image not available${path ? ": " + path : ""})`;
    card.appendChild(missing);
  }
  if (caption || path) {
    const cap = document.createElement("div");
    cap.className = "image-card-caption";
    cap.textContent = caption || path;
    card.appendChild(cap);
  }
  return card;
}

function buildAudioCard(src, caption, path) {
  const card = document.createElement("div");
  card.className = "audio-card";
  if (src) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = src;
    card.appendChild(audio);
  } else {
    const missing = document.createElement("div");
    missing.className = "audio-card-missing";
    missing.textContent = `(audio not available${path ? ": " + path : ""})`;
    card.appendChild(missing);
  }
  if (caption || path) {
    const cap = document.createElement("div");
    cap.className = "audio-card-caption";
    cap.textContent = caption || path;
    card.appendChild(cap);
  }
  return card;
}

/* ------------------------------------------------ compaction + sub-agent UI */

function buildNoticeEl(level, text) {
  const lvl = level === "error" ? "error" : level === "warn" ? "warn" : "info";
  const icon = { info: "ℹ", warn: "⚠", error: "⛔" }[lvl];
  const box = document.createElement("div");
  box.className = "notice-box notice-" + lvl;
  box.innerHTML = `<span class="notice-icon">${icon}</span><span class="notice-text"></span>`;
  box.querySelector(".notice-text").textContent = text || "";
  return box;
}

/* ------------------------------------------- turn change review card --
   After a turn, everything that changed on disk (vs the automatic pre-turn
   snapshot) is shown as a reviewable card: per-file diffs + Revert. */

async function showTurnChanges() {
  try {
    const res = await api().turn_changes();
    const files = (res && res.files) || [];
    if (!files.length) return;
    const wrap = document.createElement("div");
    wrap.className = "msg msg-assistant";
    wrap.appendChild(buildChangesCard(files));
    chatEl.appendChild(wrap);
    scrollDown();
  } catch (e) { /* review card is best-effort */ }
}

// A newer turn moves the revert baseline; older cards' buttons would no
// longer mean "back to before THAT turn", so retire them.
function retireOldChangeCards() {
  document.querySelectorAll(".change-revert:not(:disabled)").forEach((b) => {
    b.disabled = true;
    b.title = "A newer turn has run — use Settings → Backups to go further back";
  });
}

function buildChangesCard(files) {
  const card = document.createElement("div");
  card.className = "changes-card";
  card.innerHTML =
    `<div class="changes-head">Changes this turn · ${files.length} ` +
    `file${files.length === 1 ? "" : "s"} — click one to review</div>` +
    `<div class="changes-rows"></div>`;
  const rows = card.querySelector(".changes-rows");
  for (const f of files) rows.appendChild(buildChangeRow(f));
  return card;
}

function buildChangeRow(f) {
  const row = document.createElement("details");
  row.className = "change-row";
  const statusName = { A: "added", M: "modified", D: "deleted", R: "renamed" }[f.status] || f.status;
  row.innerHTML =
    `<summary><span class="change-status st-${esc(f.status)}"></span>` +
    `<span class="change-path"></span>` +
    `<button class="btn btn-danger-ghost change-revert">Revert</button></summary>` +
    `<pre class="change-diff"></pre>`;
  row.querySelector(".change-status").textContent = statusName;
  row.querySelector(".change-path").textContent = f.path;
  row.querySelector(".change-diff").innerHTML = colorDiff(f.diff || "(no diff available)");
  row.querySelector(".change-revert").addEventListener("click", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm(`Revert ${f.path} to how it was before this turn?`)) return;
    const res = await api().revert_change(f.path);
    if (res && res.error) { toast(res.error, "error", 5000); return; }
    row.classList.add("reverted");
    const btn = row.querySelector(".change-revert");
    btn.disabled = true;
    btn.textContent = "Reverted";
    toast(`Reverted ${f.path}`, "info", 3000);
  });
  return row;
}

function buildSteeredEl(text) {
  const box = document.createElement("div");
  box.className = "steered-note";
  box.innerHTML = `<span class="steered-icon">↪</span><span class="steered-label">You steered:</span> ` +
                  `<span class="steered-text"></span>`;
  box.querySelector(".steered-text").textContent = text || "";
  return box;
}

function buildCompactedEl(summary) {
  const box = document.createElement("details");
  box.className = "compacted-box";
  box.open = false;
  box.innerHTML =
    `<summary><span class="compacted-icon">🗜</span> Context compacted` +
    `<span class="compacted-hint">— the conversation was summarized to save space. ` +
    `Click to view the retained context.</span></summary>` +
    `<div class="compacted-body md"></div>`;
  box.querySelector(".compacted-body").innerHTML = md(summary || "(no summary)");
  return box;
}

// One panel per turn, holding a row per sub-agent, keyed by ev.id.
function updateSubagent(turn, ev) {
  let panel = turn.subagentPanel;
  if (!panel || !panel.isConnected) {
    panel = document.createElement("div");
    panel.className = "subagents";
    panel.innerHTML = `<div class="subagents-head">Parallel sub-agents</div>` +
                      `<div class="subagents-rows"></div>`;
    turn.wrap.appendChild(panel);
    turn.subagentPanel = panel;
    turn.subagentRows = {};
  }
  const rows = panel.querySelector(".subagents-rows");
  let row = turn.subagentRows[ev.id];
  if (!row) {
    row = document.createElement("div");
    row.className = "subagent-row";
    row.innerHTML =
      `<span class="subagent-dot"></span>` +
      `<div class="subagent-text"><span class="subagent-name"></span>` +
      `<span class="subagent-detail"></span></div>` +
      `<span class="subagent-status"></span>`;
    row.title = "Click to see this sub-agent's own thread";
    row.addEventListener("click", () => openSubagentPanel(
      ev.id, row.querySelector(".subagent-name").textContent, row.dataset.status));
    rows.appendChild(row);
    turn.subagentRows[ev.id] = row;
  }
  row.className = "subagent-row sa-" + (ev.status || "running");
  row.dataset.status = ev.status || "running";
  row.querySelector(".subagent-name").textContent = ev.name || ev.id;
  const detail = ev.status === "running" ? (ev.mission || "") : (ev.summary || "");
  row.querySelector(".subagent-detail").textContent = detail;
  const label = { running: "running…", done: "done", error: "failed" }[ev.status] || ev.status;
  row.querySelector(".subagent-status").textContent = label;
  updateSubagentTabStatus(ev.id, ev.status);
}

/* ------------------------------------------------ sub-agent inspector panel
   A live mirror of the main chat's streaming render, but per sub-agent id,
   shown in a slide-out panel on the right so the user can watch a specific
   sub-agent's own reasoning/content/tool calls the same way they watch the
   main agent. Fed by "subagent_stream" events (see renderSubagentEvent). */

const subagentThreads = {};   // aid -> per-thread render state (mirrors `current`)
const subagentStatus = {};    // aid -> "running" | "done" | "error"
const subagentSteerPending = {}; // aid -> queued (undelivered) steering text, or absent
let activeSubagentId = null;

function getSubagentThread(aid) {
  let t = subagentThreads[aid];
  if (!t) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-assistant subagent-thread";
    // A thread's DOM node is created lazily, on its first stream event --
    // which can arrive AFTER the user already opened this sub-agent's panel
    // (e.g. they click a "running" row before its first event lands). Match
    // visibility to whatever's currently active instead of always starting
    // hidden, or it silently never appears until some other aid's event
    // happens to touch showActiveSubagentThread again.
    wrap.hidden = aid !== activeSubagentId;
    $("subagent-panel-body").appendChild(wrap);
    t = { wrap, content: "", reasoning: "", contentEl: null, thinking: null, thinkBody: null, lastTool: null };
    subagentThreads[aid] = t;
  }
  return t;
}

function renderSubagentEvent(aid, ev) {
  const t = getSubagentThread(aid);
  switch (ev.kind) {
    case "stream_start":
      t.content = "";
      t.contentEl = null;
      break;
    case "reasoning": {
      // See the main-chat "reasoning" case for why this creates a fresh box
      // per burst instead of reactivating one further up: reusing the old
      // box would visually place all the reasoning before any of the tool
      // calls, not interleaved with them the way it actually happened.
      if (!t.thinking || !t.thinking.classList.contains("active")) {
        t.thinking = document.createElement("details");
        t.thinking.className = "thinking active";
        t.thinking.innerHTML =
          `<summary><span class="think-dot"></span>Thinking…</summary>` +
          `<div class="thinking-body"></div>`;
        t.thinkBody = t.thinking.querySelector(".thinking-body");
        t.thinkNode = document.createTextNode("");
        t.thinkBody.appendChild(t.thinkNode);
        t.wrap.appendChild(t.thinking);
      }
      // Append-only (O(delta)); deltas already arrive batched from Python.
      t.thinkNode.appendData(ev.text || "");
      break;
    }
    case "content": {
      if (t.thinking && t.thinking.classList.contains("active")) {
        t.thinking.classList.remove("active");
        t.thinking.querySelector("summary").innerHTML = `<span class="think-dot"></span>Thought process`;
      }
      if (!t.contentEl) {
        const b = document.createElement("div");
        b.className = "bubble-assistant";
        t.contentEl = document.createElement("div");
        t.contentEl.className = "md";
        b.appendChild(t.contentEl);
        t.wrap.appendChild(b);
      }
      t.content += ev.text || "";
      t.mdDirty = true;
      // Re-rendering the full markdown per delta for EVERY thread was the
      // panel's big cost: with 6 parallel sub-agents at most one is visible,
      // so hidden threads just mark themselves dirty and render lazily on
      // tab switch (see showActiveSubagentThread).
      if (!t.wrap.hidden) queueSubagentRender();
      break;
    }
    case "stream_end": {
      if (t.thinking) {
        t.thinking.classList.remove("active");
        t.thinking.querySelector("summary").innerHTML = `<span class="think-dot"></span>Thought process`;
      }
      renderSubagentMdIfVisible(t);
      break;
    }
    case "tool_call": {
      const el = buildToolEl(ev.name, ev.args || {});
      t.wrap.appendChild(el);
      t.lastTool = el;
      break;
    }
    case "tool_result": {
      const el = t.lastTool;
      if (!el) break;
      if (SILENT_TOOLS.has(ev.name) && !ev.is_error) {
        el.remove();
      } else {
        finishToolEl(el, ev.content, !!ev.is_error);
      }
      break;
    }
    case "steered": {
      t.wrap.appendChild(buildSteeredEl(ev.text || ""));
      delete subagentSteerPending[aid];
      if (aid === activeSubagentId) renderSubagentSteerQueued();
      break;
    }
    case "steer_returned": {
      // Turn ended before this sub-agent's queued message got delivered --
      // put it back in its mini-composer instead of losing it.
      delete subagentSteerPending[aid];
      if (aid === activeSubagentId) {
        const box = $("subagent-input");
        box.value = box.value.trim() ? (ev.text || "") + "\n" + box.value : (ev.text || "");
        renderSubagentSteerQueued();
        toast("Steering message wasn't delivered in time -- it's back in the box.", "info", 4500);
      }
      break;
    }
    case "wrapup_requested": {
      t.wrap.appendChild(buildNoticeEl("info", "Asked to wrap up -- writing its report now instead of continuing."));
      break;
    }
    case "notice": {
      // Previously silently dropped (info/warn/error had no override on the
      // sub-agent event sink) -- rate-limit backoff, output-limit
      // continuations and real API errors happened invisibly, making a
      // sub-agent that hit trouble look like it was just doing nothing.
      t.wrap.appendChild(buildNoticeEl(ev.level, ev.text || ""));
      break;
    }
  }
  if (aid === activeSubagentId) scrollSubagentPanel();
}

function renderSubagentMdIfVisible(t) {
  if (t && t.contentEl && t.mdDirty && !t.wrap.hidden) {
    t.contentEl.innerHTML = md(t.content);
    t.mdDirty = false;
  }
}

let subagentRenderQueued = false;
function queueSubagentRender() {
  if (subagentRenderQueued) return;
  subagentRenderQueued = true;
  setTimeout(() => {
    subagentRenderQueued = false;
    renderSubagentMdIfVisible(subagentThreads[activeSubagentId]);
    scrollSubagentPanel();
  }, 80);
}

let subPanelPinned = true;
$("subagent-panel-body").addEventListener("scroll", () => {
  const b = $("subagent-panel-body");
  subPanelPinned = b.scrollHeight - b.scrollTop - b.clientHeight < 140;
}, { passive: true });

function scrollSubagentPanel() {
  // Same follow-the-stream pinning as the main chat: never yank the user
  // down while they're scrolled up reading earlier output.
  if (!subPanelPinned) return;
  const body = $("subagent-panel-body");
  body.scrollTo({ top: body.scrollHeight, behavior: "instant" });
}

function ensureSubagentTab(aid, name) {
  let tab = $("subagent-tabs").querySelector(`[data-aid="${aid}"]`);
  if (!tab) {
    tab = document.createElement("button");
    tab.className = "subagent-tab";
    tab.dataset.aid = aid;
    tab.innerHTML = `<span class="sa-tab-dot"></span><span class="sa-tab-name"></span>`;
    tab.addEventListener("click", () => {
      activeSubagentId = aid;
      showActiveSubagentThread();
    });
    $("subagent-tabs").appendChild(tab);
  }
  tab.querySelector(".sa-tab-name").textContent = name || aid;
  return tab;
}

function updateSubagentTabStatus(aid, status) {
  subagentStatus[aid] = status || "running";
  const tab = $("subagent-tabs").querySelector(`[data-aid="${aid}"]`);
  if (tab) tab.querySelector(".sa-tab-dot").className = "sa-tab-dot sa-" + (status || "running");
  if (aid === activeSubagentId) updateSubagentComposerState();
}

function updateSubagentComposerState() {
  const running = subagentStatus[activeSubagentId] === "running";
  $("subagent-input").disabled = !running;
  $("subagent-send-btn").disabled = !running;
  $("subagent-wrapup-btn").disabled = !running;
  $("subagent-input").placeholder = running
    ? "Steer this sub-agent…"
    : "This sub-agent has finished";
}

function renderSubagentSteerQueued() {
  const box = $("subagent-steer-queued");
  const text = subagentSteerPending[activeSubagentId];
  if (!text) { box.hidden = true; return; }
  box.hidden = false;
  $("subagent-steer-queued-text").textContent = text;
}

function showActiveSubagentThread() {
  for (const [aid, t] of Object.entries(subagentThreads)) {
    t.wrap.hidden = aid !== activeSubagentId;
  }
  // The newly shown thread may have accumulated content while hidden
  // (hidden threads skip markdown rendering entirely) -- catch it up now.
  renderSubagentMdIfVisible(subagentThreads[activeSubagentId]);
  $("subagent-tabs").querySelectorAll(".subagent-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.aid === activeSubagentId);
  });
  updateSubagentComposerState();
  renderSubagentSteerQueued();
  scrollSubagentPanel();
}

function openSubagentPanel(aid, name, status) {
  ensureSubagentTab(aid, name);
  // The tab may be created well after this sub-agent's last status update
  // (tabs are created lazily, on first click) -- apply the CURRENT status
  // immediately instead of leaving the dot at its neutral default color
  // until some future update happens to touch this aid again.
  if (status) updateSubagentTabStatus(aid, status);
  activeSubagentId = aid;
  showActiveSubagentThread();
  document.body.classList.add("subagent-open");
}

function closeSubagentPanel() {
  document.body.classList.remove("subagent-open");
}

function clearSubagentPanel() {
  document.body.classList.remove("subagent-open");
  $("subagent-tabs").innerHTML = "";
  $("subagent-panel-body").innerHTML = "";
  for (const key of Object.keys(subagentThreads)) delete subagentThreads[key];
  for (const key of Object.keys(subagentSteerPending)) delete subagentSteerPending[key];
  activeSubagentId = null;
  renderSubagentSteerQueued();
}

$("subagent-panel-close").addEventListener("click", closeSubagentPanel);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("subagent-open")) closeSubagentPanel();
});

async function sendSubagentSteer() {
  const box = $("subagent-input");
  const text = box.value.trim();
  const aid = activeSubagentId;
  if (!text || !aid || subagentStatus[aid] !== "running") return;
  if (subagentSteerPending[aid]) {
    toast("A steering message is already queued for this sub-agent.", "warn", 4000);
    return;
  }
  box.value = "";
  subagentSteerPending[aid] = text;
  renderSubagentSteerQueued();
  try {
    const res = await api().steer_subagent(aid, text);
    if (res && res.error) {
      toast(res.error, "error", 5000);
      delete subagentSteerPending[aid];
      renderSubagentSteerQueued();
      box.value = text;
    }
  } catch (e) {
    toast("Bridge error: " + e, "error", 7000);
    delete subagentSteerPending[aid];
    renderSubagentSteerQueued();
    box.value = text;
  }
}
$("subagent-steer-queued-edit").addEventListener("click", async () => {
  const aid = activeSubagentId;
  const text = subagentSteerPending[aid];
  if (!text) return;
  await api().steer_subagent_clear(aid);
  delete subagentSteerPending[aid];
  renderSubagentSteerQueued();
  const box = $("subagent-input");
  box.value = box.value.trim() ? text + "\n" + box.value : text;
  box.focus();
});
$("subagent-steer-queued-delete").addEventListener("click", async () => {
  const aid = activeSubagentId;
  if (!subagentSteerPending[aid]) return;
  await api().steer_subagent_clear(aid);
  delete subagentSteerPending[aid];
  renderSubagentSteerQueued();
});
$("subagent-send-btn").addEventListener("click", sendSubagentSteer);
$("subagent-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendSubagentSteer();
  }
});

async function wrapupSubagent() {
  const aid = activeSubagentId;
  if (!aid || subagentStatus[aid] !== "running") return;
  const res = await api().wrapup_subagent(aid);
  if (res && res.error) toast(res.error, "error", 5000);
  else toast("Asked this sub-agent to wrap up and report now.", "info", 3000);
}
$("subagent-wrapup-btn").addEventListener("click", wrapupSubagent);

/* ------------------------------------------------ permission sheet */

let permId = null;
function showPermission(ev) {
  permId = ev.id;
  $("perm-title").textContent = ev.title;
  $("perm-preview").innerHTML = colorDiff(ev.preview || "");
  $("perm-feedback").value = "";
  $("perm-always").hidden = !ev.always;
  if (ev.always) $("perm-always").textContent = cap(ev.always);
  $("perm-backdrop").hidden = false;
  $("perm-allow").focus();
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
function answerPermission(ans) {
  if (permId === null) return;
  const fb = $("perm-feedback").value.trim();
  api().permission_response(permId, ans, fb);
  permId = null;
  $("perm-backdrop").hidden = true;
}
$("perm-allow").addEventListener("click", () => answerPermission("y"));
$("perm-always").addEventListener("click", () => answerPermission("a"));
$("perm-deny").addEventListener("click", () => answerPermission("n"));

/* ------------------------------------------------ composer / sending */

let busy = false;
let attachments = []; // {path, name, thumb}

const input = $("input");
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

function setBusy(b) {
  busy = b;
  $("stop-btn").hidden = !b;
  // The send button stays up while busy: it becomes a "steer" button instead
  // of disappearing, so the user can send a message that reaches the agent
  // without interrupting whatever it's currently doing (see sendMessage).
  const sendBtn = $("send-btn");
  sendBtn.hidden = false;
  sendBtn.disabled = false;
  sendBtn.classList.toggle("steer-mode", b);
  sendBtn.title = b ? "Steer: send without interrupting the agent" : "Send";
  if (!b) clearSteerQueued();
}

/* -------------------------------------------------- steering queue (main) --
   Only one steering message can be queued at a time. It shows as a small
   bubble above the composer until it's either delivered (the "steered"
   event fires and the bubble clears), returned because the turn ended
   first ("steer_returned" -- text goes back into the input), or the user
   edits/removes it themselves. */
let steerPending = null;

function renderSteerQueued() {
  const box = $("steer-queued");
  if (steerPending === null) { box.hidden = true; return; }
  box.hidden = false;
  $("steer-queued-text").textContent = steerPending;
}

function clearSteerQueued() {
  steerPending = null;
  renderSteerQueued();
}

function restoreSteerText(text) {
  if (!text) return;
  if (input.value.trim()) {
    input.value = text + "\n" + input.value;
  } else {
    input.value = text;
  }
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

$("steer-queued-edit").addEventListener("click", async () => {
  if (steerPending === null) return;
  const text = steerPending;
  await api().steer_clear();
  clearSteerQueued();
  restoreSteerText(text);
  input.focus();
});
$("steer-queued-delete").addEventListener("click", async () => {
  if (steerPending === null) return;
  await api().steer_clear();
  clearSteerQueued();
});

async function sendMessage() {
  const text = input.value.trim();
  if (busy) {
    // Don't interrupt the running turn -- queue this as a steering message.
    // It's delivered the next time the agent finishes a tool call, and a
    // "steered" event renders a note in its thread once that happens.
    if (!text) return;
    if (steerPending !== null) {
      toast("A steering message is already queued -- edit or remove it first.", "warn", 4000);
      return;
    }
    input.value = "";
    input.style.height = "auto";
    steerPending = text;
    renderSteerQueued();
    try {
      const res = await api().steer(text);
      if (res && res.error) {
        toast(res.error, "error", 5000);
        clearSteerQueued();
        restoreSteerText(text);
      }
    } catch (e) {
      toast("Bridge error: " + e, "error", 7000);
      clearSteerQueued();
      restoreSteerText(text);
    }
    return;
  }
  if (!text && attachments.length === 0) return;
  const plan = planMode && !!text;
  const imgs = attachments.slice();
  addUserMessage(text || (imgs.length === 1 ? "(file attached)" : "(files attached)"), imgs, "", plan);
  input.value = "";
  input.style.height = "auto";
  attachments = [];
  renderAttachments();
  setBusy(true);
  current = null;
  $("plan-actions").hidden = true;
  retireOldChangeCards();
  // The turn runs on a backend thread: send() returns immediately and the
  // busy state / usage / plan bar / changes card are all driven by the
  // chat_busy + turn_complete events (which follow the chat even if the
  // user switches away and back).
  try {
    const res = await api().send(text, imgs.map((i) => i.path), plan);
    if (res && res.error) {
      if (res.error !== "busy") toast(res.error, "error", 7000);
      setBusy(false);
    }
  } catch (e) {
    toast("Bridge error: " + e, "error", 7000);
    setBusy(false);
  }
}
$("send-btn").addEventListener("click", sendMessage);
$("stop-btn").addEventListener("click", () => api().cancel());

/* --------------------------------------------------------- plan mode -- */
let planMode = false;
$("plan-toggle").addEventListener("click", () => {
  planMode = !planMode;
  $("plan-toggle").classList.toggle("on", planMode);
  $("plan-toggle").setAttribute("aria-pressed", String(planMode));
  input.placeholder = planMode
    ? "Describe the task — the agent will plan it before touching anything…"
    : "Ask anything about your code…";
  if (!planMode) $("plan-actions").hidden = true;
});
$("plan-dismiss").addEventListener("click", () => { $("plan-actions").hidden = true; });
$("plan-execute").addEventListener("click", async () => {
  if (busy) return;
  $("plan-actions").hidden = true;
  // Executing ends planning: further messages are normal turns again.
  planMode = false;
  $("plan-toggle").classList.remove("on");
  $("plan-toggle").setAttribute("aria-pressed", "false");
  input.placeholder = "Ask anything about your code…";
  addUserMessage("Execute the approved plan.", []);
  setBusy(true);
  current = null;
  retireOldChangeCards();
  try {
    const res = await api().execute_plan();
    if (res && res.error) {
      if (res.error !== "busy") toast(res.error, "error", 7000);
      setBusy(false);
    }
  } catch (e) {
    toast("Bridge error: " + e, "error", 7000);
    setBusy(false);
  }
});

$("attach-btn").addEventListener("click", async () => {
  const picked = await api().pick_files();
  if (picked && picked.length) {
    attachments.push(...picked);
    renderAttachments();
  }
});

function renderAttachments() {
  const box = $("attachments");
  box.hidden = attachments.length === 0;
  box.innerHTML = "";
  attachments.forEach((a, i) => {
    const el = document.createElement("div");
    el.className = "att";
    const preview = a.thumb
      ? `<img src="${a.thumb}" alt="${esc(a.name)}" title="${esc(a.name)}">`
      : `<span class="file-chip" title="${esc(a.name)}">${FILE_ICON_SVG}<span>${esc(a.name)}</span></span>`;
    el.innerHTML = preview + `<button aria-label="Remove ${esc(a.name)}">&#10005;</button>`;
    el.querySelector("button").addEventListener("click", () => {
      attachments.splice(i, 1);
      renderAttachments();
    });
    box.appendChild(el);
  });
}

/* hints (delegated: the empty-hint chips are re-rendered per session) */
document.addEventListener("click", (e) => {
  const h = e.target.closest(".hint");
  if (h) { input.value = h.dataset.hint; input.focus(); }
});

/* external links */
document.addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (a) {
    e.preventDefault();
    const href = a.dataset.href || a.getAttribute("href");
    if (href && href.startsWith("http")) api().open_external(href);
  }
});

/* ------------------------------------------------ usage + mode chips */

let contextLimit = 110000; // overwritten by boot(); mirrors context_limit_tokens

function updateUsage(pt, ct, context) {
  // Headline = OUTPUT tokens. Summing prompt+completion here made the number
  // look absurdly high: prompt tokens re-count the ENTIRE conversation on
  // every agentic round (a turn with 30 tool calls re-sends the whole
  // context 30 times), so the sum measures API traffic, not anything a user
  // would recognize as "tokens used". Output tokens are what the model
  // actually wrote -- the full breakdown lives in the tooltip.
  $("usage-chip").textContent = fmtTokens(ct || 0) + " tok";
  $("usage-chip").title =
    `Model output this chat: ${fmtTokens(ct || 0)} tokens` +
    `\nSent to the model (incl. re-sent context): ${fmtTokens(pt || 0)} tokens` +
    `\nAlways $0.00`;
  if (context !== undefined && context !== null) updateContextDonut(context);
}

function updateContextDonut(used) {
  used = Math.max(0, used || 0);
  const limit = contextLimit || 0;
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const seg = $("token-segment");
  if (seg) {
    seg.setAttribute("stroke-dasharray", `${pct}, 100`);
    // Warm the ring up as the context fills toward the auto-compact limit.
    const color = pct >= 90 ? "var(--danger, #e5484d)"
      : pct >= 70 ? "var(--warn, #f5a623)"
      : "var(--accent)";
    seg.style.stroke = color;
  }
  const txt = $("token-text");
  if (txt) txt.textContent = `${fmtTokens(used)} / ${fmtTokens(limit)} tokens`;
}

const MODES = ["ask", "autoedit", "yolo"];
const MODE_LABEL = { ask: "ask", autoedit: "auto-edit", yolo: "full auto" };
let settings = {};

function applyModeChip() {
  const chip = $("mode-chip");
  chip.textContent = MODE_LABEL[settings.mode] || settings.mode;
  chip.className = "chip chip-btn" +
    (settings.mode === "yolo" ? " mode-yolo" : settings.mode === "autoedit" ? " mode-autoedit" : "");
}
$("mode-chip").addEventListener("click", async () => {
  const next = MODES[(MODES.indexOf(settings.mode) + 1) % MODES.length];
  settings = await api().set_setting("mode", next);
  applyModeChip();
  syncSettingsUI();
  toast("Permission mode: " + MODE_LABEL[next], "info", 2200);
});

/* ------------------------------------------------ read-aloud (TTS) */

function applyReadAloudChip() {
  const chip = $("read-aloud-chip");
  const on = !!settings.read_aloud;
  chip.setAttribute("aria-pressed", String(on));
  chip.title = on ? "Read replies aloud (on)" : "Read replies aloud (off)";
  chip.classList.toggle("active", on);
  $("ra-waves").hidden = !on;
  $("ra-mute").hidden = on;
  $("ra-mute2").hidden = on;
}
$("read-aloud-chip").addEventListener("click", async () => {
  const next = !settings.read_aloud;
  if (next) {
    const status = await api().tts_status();
    if (!status.ready) {
      const proceed = confirm(
        "Reading replies aloud uses local text-to-speech (Kokoro). The first time, this " +
        "downloads about 350MB (one-time) and installs in the background -- everything " +
        "after that runs fully offline. Continue?"
      );
      if (!proceed) return;
    }
  } else {
    resetTtsPlayback();  // stop anything currently playing when turned off
  }
  settings = await api().set_setting("read_aloud", next);
  applyReadAloudChip();
  toast("Read aloud: " + (next ? "on" : "off"), "info", 2200);
});

async function populateVoiceSelect() {
  const sel = $("voice-select");
  const { voices } = await api().tts_voices();
  sel.innerHTML = voices.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if (settings.tts_voice && voices.includes(settings.tts_voice)) sel.value = settings.tts_voice;
  const status = await api().tts_status();
  $("voice-first-use-note").hidden = !!status.ready;
}
$("voice-select").addEventListener("change", async () => {
  settings = await api().set_setting("tts_voice", $("voice-select").value);
});
$("voice-preview-btn").addEventListener("click", async () => {
  const btn = $("voice-preview-btn");
  if (btn.classList.contains("loading")) return;
  btn.classList.add("loading");
  try {
    const res = await api().preview_voice($("voice-select").value);
    if (res.error) { toast("Preview failed: " + res.error, "error", 6000); return; }
    new Audio(res.src).play().catch(() => {});
  } finally {
    btn.classList.remove("loading");
  }
});
$("voice-speed").addEventListener("input", () => {
  $("voice-speed-label").textContent = parseFloat($("voice-speed").value).toFixed(1) + "x";
});
$("voice-speed").addEventListener("change", async () => {
  settings = await api().set_setting("tts_speed", parseFloat($("voice-speed").value));
});

/* ---------------------------------------------- model providers (BYOM) -- */

let providersCache = null;

function refreshModelFoot(res) {
  if (!res) return;
  const builtin = res.chat_provider === (providersCache?.providers?.[0]?.name || "z.ai (free)");
  $("model-foot").textContent = builtin
    ? `${res.chat_model} via z.ai — always $0.00`
    : `${res.chat_model} via ${res.chat_provider}`;
}

function fillModelSelect(selected) {
  const p = (providersCache?.providers || []).find((x) => x.name === $("model-provider").value);
  const models = (p && p.models) || [];
  $("model-select").innerHTML = models.map((m) => `<option>${esc(m)}</option>`).join("");
  if (selected && models.includes(selected)) $("model-select").value = selected;
}

function renderProviderList(providers) {
  const list = $("provider-list");
  list.innerHTML = "";
  for (const p of providers) {
    if (p.builtin) continue;
    const row = document.createElement("div");
    row.className = "provider-row";
    row.innerHTML =
      `<div class="provider-row-text"><span class="provider-name"></span>` +
      `<span class="provider-sub"></span></div>` +
      `<button class="icon-btn-mini" aria-label="Delete provider" title="Delete">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>`;
    row.querySelector(".provider-name").textContent = p.name;
    row.querySelector(".provider-sub").textContent =
      `${p.base_url} · ${p.models.length} model${p.models.length === 1 ? "" : "s"}`;
    row.querySelector("button").addEventListener("click", async () => {
      const res = await api().delete_provider(p.name);
      populateModelPicker(res);
    });
    list.appendChild(row);
  }
  if (!list.children.length) {
    list.innerHTML = '<div class="row-sub">No custom providers yet. Any OpenAI-compatible endpoint works.</div>';
  }
}

async function populateModelPicker(data) {
  const res = data || await api().providers();
  if (!res || !res.providers) return;
  providersCache = res;
  $("model-provider").innerHTML = res.providers
    .map((p) => `<option>${esc(p.name)}</option>`).join("");
  $("model-provider").value = res.chat_provider;
  fillModelSelect(res.chat_model);
  renderProviderList(res.providers);
  refreshModelFoot(res);
}

async function applyChatModel() {
  const res = await api().set_chat_model($("model-provider").value, $("model-select").value);
  if (res && res.error) {
    toast(res.error, "error", 5000);
    populateModelPicker();
    return;
  }
  populateModelPicker(res);
  toast(`This chat now uses ${res.chat_model}.`, "info", 3000);
}

$("model-provider").addEventListener("change", () => { fillModelSelect(); applyChatModel(); });
$("model-select").addEventListener("change", applyChatModel);

$("prov-add").addEventListener("click", async () => {
  const res = await api().add_provider(
    $("prov-name").value, $("prov-url").value, $("prov-key").value, $("prov-models").value);
  if (res && res.error) { toast(res.error, "error", 6000); return; }
  for (const id of ["prov-name", "prov-url", "prov-key", "prov-models"]) $(id).value = "";
  populateModelPicker(res);
  toast("Provider added.", "info", 3000);
});

$("prov-detect").addEventListener("click", async () => {
  const res = await api().detect_local_providers();
  if (res && res.error) { toast(res.error, "error", 6000); return; }
  populateModelPicker(res);
  toast(res.found && res.found.length
    ? `Found: ${res.found.join(", ")}`
    : "No local model servers found (is Ollama or LM Studio running?)", "info", 5000);
});

async function populateBackups() {
  const status = await api().backup_status();
  const toggle = $("backup-toggle");
  toggle.setAttribute("aria-checked", String(!!status.enabled));
  toggle.disabled = !status.available;
  $("backup-unavailable-note").hidden = !!status.available;
  const snaps = status.snapshots || []; // newest first
  $("backup-count-note").textContent = snaps.length
    ? `${snaps.length} snapshot${snaps.length === 1 ? "" : "s"}`
    : "No snapshots yet";
  $("backup-revert-last").disabled = snaps.length === 0;

  const list = $("backup-list");
  list.innerHTML = "";
  for (const s of snaps) {
    const row = document.createElement("div");
    row.className = "backup-item";
    const textWrap = document.createElement("div");
    textWrap.className = "backup-item-text";
    const msgEl = document.createElement("span");
    msgEl.className = "backup-item-msg";
    msgEl.textContent = s.message;
    const timeEl = document.createElement("span");
    timeEl.className = "backup-item-time";
    timeEl.textContent = timeAgo(s.timestamp);
    textWrap.append(msgEl, timeEl);
    const btn = document.createElement("button");
    btn.className = "btn btn-ghost backup-item-revert";
    btn.textContent = "Revert";
    btn.addEventListener("click", () => confirmRevert(s.commit));
    row.append(textWrap, btn);
    list.appendChild(row);
  }
}

async function confirmRevert(commit) {
  if (!confirm("Revert your project's files to this point? Only files on disk change -- "
              + "the chat conversation stays as-is.")) return;
  const res = await api().revert_backup(commit);
  if (res && res.error) { toast(res.error, "error", 6000); return; }
  toast("Files reverted.", "info", 3000);
  populateBackups();
}

$("backup-toggle").addEventListener("click", async () => {
  if ($("backup-toggle").disabled) return;
  const now = $("backup-toggle").getAttribute("aria-checked") === "true";
  await api().set_backup_enabled(!now);
  populateBackups();
});
$("backup-revert-last").addEventListener("click", async () => {
  const status = await api().backup_status();
  const snaps = status.snapshots || [];
  if (!snaps.length) return;
  confirmRevert(snaps[0].commit);
});

/* ------------------------------------------------ settings sheet */

function syncSettingsUI() {
  document.querySelectorAll("#seg-mode button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === settings.mode);
    b.setAttribute("aria-checked", b.dataset.v === settings.mode);
  });
  document.querySelectorAll("#seg-vision button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === settings.vision_route);
    b.setAttribute("aria-checked", b.dataset.v === settings.vision_route);
  });
  $("opt-thinking").setAttribute("aria-checked", !!settings.thinking);
  $("opt-reasoning").setAttribute("aria-checked", !!settings.show_reasoning);
  $("cwd-label").textContent = settings.cwd || "No chat selected";
  $("cwd-label").title = settings.cwd || "";
  $("tb-cwd").textContent = shortPath(settings.cwd || "");
  $("tb-cwd").title = settings.cwd || "";
  const spd = settings.tts_speed || 1.0;
  $("voice-speed").value = spd;
  $("voice-speed-label").textContent = spd.toFixed(1) + "x";
  applyModeChip();
  applyReadAloudChip();
}
function shortPath(p) {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts.length > 2 ? "…\\" + parts.slice(-2).join("\\") : p;
}

$("settings-btn").addEventListener("click", async () => {
  const u = await api().usage();
  $("session-usage").textContent =
    `${fmtTokens(u.completion_tokens)} output · ${fmtTokens(u.prompt_tokens)} sent · context ~${fmtTokens(u.context)} · $0.00`;
  $("settings-backdrop").hidden = false;
  populateVoiceSelect();
  populateBackups();
  populateModelPicker();
});
$("settings-close").addEventListener("click", () => { $("settings-backdrop").hidden = true; });
$("settings-backdrop").addEventListener("click", (e) => {
  if (e.target === $("settings-backdrop")) $("settings-backdrop").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("settings-backdrop").hidden = true;
});

document.querySelectorAll("#seg-mode button").forEach((b) =>
  b.addEventListener("click", async () => {
    settings = await api().set_setting("mode", b.dataset.v);
    syncSettingsUI();
  }));
document.querySelectorAll("#seg-vision button").forEach((b) =>
  b.addEventListener("click", async () => {
    settings = await api().set_setting("vision_route", b.dataset.v);
    syncSettingsUI();
  }));

function bindSwitch(id, key) {
  $(id).addEventListener("click", async () => {
    const now = $(id).getAttribute("aria-checked") === "true";
    settings = await api().set_setting(key, !now);
    syncSettingsUI();
  });
}
bindSwitch("opt-thinking", "thinking");
bindSwitch("opt-reasoning", "show_reasoning");

$("bg-change").addEventListener("click", async () => {
  const res = await api().pick_background();
  if (res && res.background) setBackground(res.background);
  else if (res && res.error) toast(res.error, "error");
});
$("bg-reset").addEventListener("click", async () => {
  const res = await api().reset_background();
  if (res && res.background) setBackground(res.background);
});
function setBackground(uri) {
  $("bg").style.backgroundImage = `url("${uri}")`;
  $("bg-preview").src = uri;
}

$("chat-clear").addEventListener("click", async () => {
  const res = await api().clear_chat();
  if (res && res.error) { toast(res.error, "error"); return; }
  applySession(res);
  $("settings-backdrop").hidden = true;
  toast("Started a new chat in this project", "info", 2500);
});

$("whiteboard-clear").addEventListener("click", async () => {
  if (!confirm("Delete everything in the whiteboard folder? This can't be undone.")) return;
  const res = await api().clear_whiteboard();
  if (res && res.error) { toast(res.error, "error"); return; }
  toast("Whiteboard cleared", "info", 2200);
});
let compacting = false;
async function doCompact() {
  if (compacting) return;              // ignore double-clicks while it runs
  compacting = true;
  toast("Compacting…", "info", 2000);
  try {
    const res = await api().compact_chat();
    if (res && res.sessions) { sessions = res.sessions; renderSidebar(); }
    if (res && res.context !== undefined) updateContextDonut(res.context);
    toast((res && (res.note || res.error)) || "done",
          res && res.error ? "error" : "info", 4000);
  } catch (e) {
    toast("Compact failed: " + e, "error", 7000);
  } finally {
    compacting = false;
  }
}
// Both the composer-footer button and the settings-sheet button compact.
$("chat-compact").addEventListener("click", doCompact);
$("compact-btn").addEventListener("click", doCompact);

/* ------------------------------------------------ window controls */

$("tl-close").addEventListener("click", () => api().win("close"));
$("tl-min").addEventListener("click", () => api().win("min"));
$("tl-max").addEventListener("click", () => api().win("max"));

/* ------------------------------------------------ chat history sidebar */

let sessions = [];
let activeSessionId = null;

function basename(p) {
  const parts = String(p || "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : (p || "");
}

function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return "just now";
  const m = Math.floor(diffSec / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(then).toLocaleDateString();
}

const TRASH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>';

// When non-null, the sidebar shows these search results instead of the full
// session list (kept canonical in `sessions` -- renders during an active
// search must not stomp the filtered view).
let searchResults = null;

function renderSidebar() {
  const list = $("session-list");
  const items = searchResults ?? sessions;
  if (!items.length) {
    list.innerHTML = searchResults
      ? '<div class="session-empty">No chats match.</div>'
      : '<div class="session-empty">No chats yet.<br>Click "New Chat" to pick a project folder and start.</div>';
    return;
  }
  list.innerHTML = "";
  for (const s of items) {
    const row = document.createElement("div");
    row.className = "sess-row" + (s.id === activeSessionId ? " active" : "");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.title = s.cwd;
    const snippet = s.snippet
      ? `<div class="sess-snippet">${esc(s.snippet)}</div>` : "";
    let stateDot = "";
    if (busySessions.has(s.id)) {
      row.classList.add("sess-busy");
      stateDot = '<span class="sess-dot sess-dot-busy" title="Working in the background"></span>';
    } else if (unreadSessions.has(s.id)) {
      stateDot = '<span class="sess-dot sess-dot-unread" title="Finished while you were away"></span>';
    }
    row.innerHTML =
      `<div class="sess-main"><div class="sess-title">${stateDot}${esc(s.title || "New chat")}</div>` +
      `<div class="sess-sub">${esc(basename(s.cwd))} · ${esc(timeAgo(s.updated))}</div>${snippet}</div>` +
      `<button class="sess-del" aria-label="Delete chat: ${esc(s.title || "New chat")}">${TRASH_ICON}</button>`;
    row.addEventListener("click", (e) => {
      if (e.target.closest(".sess-del") || s.id === activeSessionId) return;
      openSession(s.id);
    });
    row.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && s.id !== activeSessionId) {
        e.preventDefault();
        openSession(s.id);
      }
    });
    row.querySelector(".sess-del").addEventListener("click", (e) => {
      e.stopPropagation();
      if (confirm(`Delete "${s.title || "this chat"}"? This can't be undone.`)) deleteSession(s.id);
    });
    list.appendChild(row);
  }
}

// Full-text chat search: matches titles AND the persistent transcripts, so
// it finds conversation content even from chats whose context was long since
// compacted away. Debounced; a sequence counter drops stale async responses
// so fast typing can't render an older query's results over a newer one's.
let searchSeq = 0;
let searchTimer = null;
$("chat-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("chat-search").value.trim();
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    if (!q) {
      searchResults = null;
      renderSidebar();
      return;
    }
    try {
      const res = await api().search_chats(q);
      if (seq !== searchSeq) return; // a newer query superseded this one
      searchResults = (res && res.sessions) || [];
      renderSidebar();
    } catch (e) {
      toast("Search failed: " + e, "error", 5000);
    }
  }, 220);
});
$("chat-search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    $("chat-search").value = "";
    searchResults = null;
    renderSidebar();
  }
});

function setSidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  $("sidebar-toggle").classList.toggle("active", open);
  try { localStorage.setItem("glm-sidebar-open", open ? "1" : "0"); } catch (e) { /* ignore */ }
}
$("sidebar-toggle").addEventListener("click", () => {
  setSidebar(!document.body.classList.contains("sidebar-open"));
});

/* ------------------------------------------------ session activation */

function clearChatDom() {
  chatEl.querySelectorAll(".msg, #todos-card, .compact-note").forEach((el) => el.remove());
  current = null;
  clearSubagentPanel();  // sub-agent threads belong to the chat being replaced
}

function showNoSession() {
  activeSessionId = null;
  document.body.classList.add("no-session");
  clearChatDom();
  $("empty-hint").hidden = true;
  $("welcome").hidden = false;
  settings.cwd = "";
  syncSettingsUI();
  updateUsage(0, 0, 0);
  renderSidebar();
}

function applySession(res) {
  if (!res) return;
  if (res.error) { toast(res.error, "error"); return; }
  if (res.sessions) sessions = res.sessions;
  activeSessionId = res.id;
  document.body.classList.remove("no-session");
  resetTtsPlayback();  // switching chats shouldn't let old audio keep playing
  clearChatDom();
  settings.cwd = res.cwd;
  syncSettingsUI();
  updateUsage(res.prompt_tokens, res.completion_tokens, res.context);
  const hasItems = !!(res.items && res.items.length);
  $("welcome").hidden = true;
  $("empty-hint").hidden = hasItems;
  if (!hasItems) $("empty-hint-folder").textContent = basename(res.cwd);
  renderHistory(res.items, res.todos);
  if (res.cwd_missing) toast(`Project folder not found: ${res.cwd}`, "warn", 6000);
  // Live chats may still be working when we switch to them.
  setBusy(!!res.busy);
  unreadSessions.delete(res.id);
  clearSteerQueued();
  $("plan-actions").hidden = true;
  const heldPerm = pendingPerms[res.id];
  if (heldPerm) {
    delete pendingPerms[res.id];
    showPermission(heldPerm); // it was waiting for the user to come back
  }
  renderSidebar();
  populateModelPicker(); // each chat can use a different model -- refresh the footer
}

async function newChat() {
  const autoBackup = $("newchat-backup").getAttribute("aria-checked") === "true";
  const res = await api().new_session(autoBackup);
  if (!res || res.cancelled) return;
  applySession(res);
  input.focus();
}
async function openWhiteboard() {
  const autoBackup = $("newchat-backup").getAttribute("aria-checked") === "true";
  const res = await api().open_whiteboard(autoBackup);
  if (!res || res.error) {
    if (res && res.error) toast(res.error, "error");
    return;
  }
  applySession(res);
  input.focus();
}
function showNewChatChooser() {
  $("newchat-backup").setAttribute("aria-checked", "true"); // default on for every new chat
  $("newchat-backdrop").hidden = false;
}
$("newchat-backup").addEventListener("click", () => {
  const now = $("newchat-backup").getAttribute("aria-checked") === "true";
  $("newchat-backup").setAttribute("aria-checked", String(!now));
});
async function openSession(id) {
  if (id === activeSessionId) return;
  const res = await api().open_session(id);
  applySession(res);
  input.focus();
}
async function deleteSession(id) {
  const wasActive = id === activeSessionId;
  const res = await api().delete_session(id);
  if (res.sessions) sessions = res.sessions;
  if (wasActive || res.closed_active) showNoSession();
  else renderSidebar();
}
$("new-chat-btn").addEventListener("click", showNewChatChooser);
$("welcome-new-chat").addEventListener("click", showNewChatChooser);
$("newchat-folder").addEventListener("click", () => {
  $("newchat-backdrop").hidden = true;
  newChat();
});
$("newchat-whiteboard").addEventListener("click", () => {
  $("newchat-backdrop").hidden = true;
  openWhiteboard();
});
$("newchat-backdrop").addEventListener("click", (e) => {
  if (e.target === $("newchat-backdrop")) $("newchat-backdrop").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("newchat-backdrop").hidden) $("newchat-backdrop").hidden = true;
});

/* ------------------------------------------------ onboarding */

$("key-save").addEventListener("click", async () => {
  const key = $("key-input").value.trim();
  if (!key) return;
  const res = await api().save_api_key(key);
  if (res && res.ok) {
    $("key-backdrop").hidden = true;
    toast(res.persisted ? "API key saved to your user environment" :
      "Key active for this session", "info", 4000);
    if (res.sessions) sessions = res.sessions;
    if (res.session) applySession(res.session);
    else showNoSession();
  } else {
    toast((res && res.error) || "Could not save key", "error");
  }
});
$("key-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("key-save").click();
});
$("zai-link").addEventListener("click", (e) => {
  e.preventDefault();
  api().open_external("https://z.ai");
});

/* ------------------------------------------------ boot */

async function boot() {
  try { api().log && api().log("boot:start"); } catch (e) { /* ignore */ }
  let sidebarPref = true;
  try {
    const v = localStorage.getItem("glm-sidebar-open");
    if (v !== null) sidebarPref = v === "1";
  } catch (e) { /* ignore */ }
  setSidebar(sidebarPref);

  const b = await api().boot();
  settings = b.settings;
  sessions = b.sessions || [];
  if (b.contextLimit) contextLimit = b.contextLimit;
  setBackground(b.background);
  $("about-version").textContent = "v" + b.version;
  syncSettingsUI();

  if (b.needsKey) {
    showNoSession();
    $("key-backdrop").hidden = false;
  } else if (b.session) {
    applySession(b.session);
    input.focus();
  } else {
    showNoSession();
  }
  try { api().log && api().log("boot:done"); } catch (e) { /* ignore */ }
}

function bootSafely() {
  boot().catch((e) => {
    try { api().log && api().log("boot:error " + e); } catch (_) { /* ignore */ }
    console.error("boot failed", e);
  });
}

if (window.pywebview && window.pywebview.api) bootSafely();
else window.addEventListener("pywebviewready", bootSafely);

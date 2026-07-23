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

// Copy the raw (de-highlighted) text of a fenced code block. navigator.
// clipboard isn't always available under WebView2's page context, so fall
// back to the old execCommand path.
function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText)
      return navigator.clipboard.writeText(text);
  } catch (e) { /* fall through */ }
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      resolve();
    } catch (e) { reject(e); }
  });
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".code-copy");
  if (!btn) return;
  const code = btn.parentElement.querySelector("pre code");
  if (!code) return;
  try {
    await copyText(code.textContent);
    btn.classList.add("copied");
    btn.textContent = "Copied!";
    setTimeout(() => { btn.classList.remove("copied"); btn.textContent = "Copy"; }, 1400);
  } catch (err) {
    toast("Couldn't copy to clipboard", "error", 3000);
  }
});

// Expand/collapse a long fenced block (see md(): blocks over ~a screenful
// render clamped with a "Show all" toggle so pasted files/diffs stay small).
document.addEventListener("click", (e) => {
  const more = e.target.closest(".code-more");
  if (!more) return;
  const wrap = more.closest(".code-clamp");
  if (!wrap) return;
  const expanded = wrap.classList.toggle("expanded");
  more.setAttribute("aria-expanded", expanded ? "true" : "false");
  more.textContent = expanded ? "Show less" : `Show all ${more.dataset.lines} lines`;
});

/* Minimal markdown renderer (safe: escapes first, then adds structure).
   `fast` skips syntax highlighting -- used only for the transient streaming
   tail (an open code fence there would otherwise be re-highlighted from
   scratch every frame); it gets fully highlighted the instant it commits. */
function md(src, fast) {
  const codeBlocks = [];
  src = String(src ?? "");
  // fenced code blocks out first
  src = src.replace(/```([\w+-]*)\n?([\s\S]*?)(?:```|$)/g, (_, lang, code) => {
    const body = code.replace(/\n$/, "");
    const lc = (lang || "").toLowerCase();
    // A fenced diff/patch (explicitly tagged, or an untagged block that
    // clearly *is* one) collapses into a tiny one-line box -- click to
    // expand the git-colored +/- view. Keeps big diffs out of the way.
    const isDiff = lc === "diff" || lc === "patch" ||
      (lc === "" && /^(@@ |diff --git |[-+]{3} )/m.test(body));
    if (isDiff) {
      const [add, del] = diffStat(body);
      codeBlocks.push(
        `<details class="diff-box">` +
        `<summary><span class="diff-box-icon">±</span>` +
        `<span class="diff-box-label">Diff</span>` +
        `<span class="diff-stat"><span class="ds-add">+${add}</span><span class="ds-del">−${del}</span></span>` +
        `<span class="diff-box-hint">— click to view</span></summary>` +
        `<div class="code-wrap code-diff">` +
        `<button class="code-copy" title="Copy code" aria-label="Copy code">Copy</button>` +
        `<pre><code data-lang="${esc(lang)}">${colorDiff(body)}</code></pre></div></details>`);
      return ` ${codeBlocks.length - 1} `;
    }
    // Long non-diff pastes (whole files) still dominate the chat, so anything
    // past a screenful collapses to a preview with a one-click "Show all".
    const nLines = body.split("\n").length;
    const tall = nLines > 16;
    const cls = "code-wrap" + (tall ? " code-clamp" : "");
    const more = tall
      ? `<button class="code-more" data-lines="${nLines}" aria-expanded="false">Show all ${nLines} lines</button>`
      : "";
    codeBlocks.push(
      `<div class="${cls}"><button class="code-copy" title="Copy code" aria-label="Copy code">Copy</button>` +
      `<pre><code data-lang="${esc(lang)}">${fast ? esc(body) : highlight(body, lang)}</code></pre>${more}</div>`);
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

/* ------------------------------------------------ find in conversation
   The webview has no native Ctrl+F, so this walks the rendered chat, wraps
   matches in <mark>, and lets you cycle them. Highlights are transient --
   they're torn out again on close (and before every re-search) so the real
   DOM and its event handlers are never permanently altered. */
const find = { hits: [], cur: -1, timer: 0 };

function clearFindMarks() {
  const marks = chatEl.querySelectorAll("mark.find-hit");
  const parents = new Set();
  marks.forEach((m) => {
    parents.add(m.parentNode);
    m.replaceWith(document.createTextNode(m.textContent));
  });
  parents.forEach((p) => p && p.normalize());  // re-merge the split text nodes
  find.hits = [];
  find.cur = -1;
}

function runFind(query) {
  clearFindMarks();
  const q = (query || "").trim();
  const countEl = $("find-count");
  if (!q) { countEl.textContent = "0/0"; return; }
  const needle = q.toLowerCase();
  // Collect matching text nodes first (mutating during a TreeWalker walk is
  // asking for trouble), skipping hidden subtrees and existing marks.
  const walker = document.createTreeWalker(chatEl, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(needle))
        return NodeFilter.FILTER_REJECT;
      const p = node.parentElement;
      if (!p || p.offsetParent === null) return NodeFilter.FILTER_REJECT; // hidden
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) targets.push(n);
  for (const node of targets) {
    const text = node.nodeValue;
    const low = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let i = 0, idx;
    while ((idx = low.indexOf(needle, i)) !== -1) {
      if (idx > i) frag.appendChild(document.createTextNode(text.slice(i, idx)));
      const mark = document.createElement("mark");
      mark.className = "find-hit";
      mark.textContent = text.slice(idx, idx + needle.length);
      frag.appendChild(mark);
      find.hits.push(mark);
      i = idx + needle.length;
    }
    if (i < text.length) frag.appendChild(document.createTextNode(text.slice(i)));
    node.parentNode.replaceChild(frag, node);
  }
  if (find.hits.length) setFindCurrent(0, false);
  else countEl.textContent = "0/0";
}

function setFindCurrent(i, scroll = true) {
  if (!find.hits.length) return;
  find.cur = (i + find.hits.length) % find.hits.length;
  find.hits.forEach((m, k) => m.classList.toggle("find-current", k === find.cur));
  $("find-count").textContent = `${find.cur + 1}/${find.hits.length}`;
  if (scroll) find.hits[find.cur].scrollIntoView({ block: "center", behavior: "smooth" });
}

function stepFind(delta) {
  if (find.hits.length) setFindCurrent(find.cur + delta);
}

function openFind() {
  const bar = $("find-bar");
  bar.hidden = false;
  const inp = $("find-input");
  // Seed with the current selection if the user highlighted something first.
  const sel = String(window.getSelection() || "").trim();
  if (sel && sel.length <= 80) inp.value = sel;
  inp.focus();
  inp.select();
  if (inp.value) runFind(inp.value);
}

function closeFind() {
  clearFindMarks();
  $("find-bar").hidden = true;
  $("find-count").textContent = "0/0";
}

$("find-input").addEventListener("input", (e) => {
  clearTimeout(find.timer);
  const v = e.target.value;
  find.timer = setTimeout(() => runFind(v), 110);
});
$("find-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1); }
  else if (e.key === "Escape") { e.preventDefault(); closeFind(); }
});
$("find-prev").addEventListener("click", () => stepFind(-1));
$("find-next").addEventListener("click", () => stepFind(1));
$("find-close").addEventListener("click", closeFind);

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "f" || e.key === "F")) {
    e.preventDefault();
    openFind();
  }
}, true);

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
  control_chrome: '<circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a15 15 0 0 1 4 9 15 15 0 0 1-4 9 15 15 0 0 1-4-9 15 15 0 0 1 4-9z"/>',
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
    case "control_chrome":
      return args.goal || "";
    case "speak":
      return args.text || "";
    default: return "";
  }
}

/* ------------------------------------------------ shared tool / todo builders */

// Tools that shell out and can block indefinitely (a dev server, a watch,
// a hung test run). Their chat box gets a Stop button while running so a
// never-ending command can't freeze the whole turn.
const STOPPABLE_TOOLS = new Set(["run_powershell", "run_tests", "run_test_file"]);

function buildToolEl(name, args, callId) {
  const el = document.createElement("div");
  el.className = "tool running";
  let stopBtn = "";
  if (callId && STOPPABLE_TOOLS.has(name)) {
    el.dataset.callId = callId;
    stopBtn = `<button class="tool-stop" title="Stop this command" aria-label="Stop this command">Stop</button>`;
  }
  el.innerHTML =
    `<button class="tool-head" aria-expanded="false">` +
    `<span class="tool-ico">${toolIcon(name)}</span>` +
    `<span class="tool-name">${esc(name)}</span>` +
    `<span class="tool-sum">${esc(toolSummary(name, args || {}))}</span>` +
    `<span class="tool-state">running</span></button>` +
    stopBtn +
    `<div class="tool-body"></div>`;
  el.querySelector(".tool-head").addEventListener("click", () => {
    el.classList.toggle("open");
    el.querySelector(".tool-head").setAttribute("aria-expanded", el.classList.contains("open"));
  });
  const sb = el.querySelector(".tool-stop");
  if (sb) {
    sb.addEventListener("click", async (e) => {
      e.stopPropagation();
      sb.disabled = true;
      sb.textContent = "Stopping…";
      const res = await api().stop_powershell(el.dataset.callId);
      if (!res || !res.ok) {
        // The command already finished on its own between click and call.
        sb.textContent = "Stop";
        sb.disabled = false;
      }
    });
  }
  return el;
}

function diffStat(text) {
  let add = 0, del = 0;
  for (const l of text.split("\n")) {
    if (l.startsWith("+") && !l.startsWith("+++")) add++;
    else if (l.startsWith("-") && !l.startsWith("---")) del++;
  }
  return [add, del];
}

function finishToolEl(el, content, isError) {
  el.classList.remove("running");
  const sb = el.querySelector(".tool-stop");
  if (sb) sb.remove();
  if (isError) el.classList.add("error");
  el.querySelector(".tool-state").textContent = isError ? "error" : "done";
  const body = el.querySelector(".tool-body");
  const c = content || "(empty)";
  const isDiff = /^(---|\+\+\+|@@)/m.test(c);
  body.innerHTML = isDiff ? colorDiff(c) : esc(c);
  // A diff result shows its magnitude (+added / -removed) right in the
  // collapsed header, so you can gauge the change without expanding it.
  if (isDiff && !el.querySelector(".diff-stat")) {
    const [add, del] = diffStat(c);
    if (add || del) {
      const badge = document.createElement("span");
      badge.className = "diff-stat";
      badge.innerHTML = `<span class="ds-add">+${add}</span><span class="ds-del">−${del}</span>`;
      el.querySelector(".tool-sum").after(badge);
    }
  }
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
  wrap.dataset.rawText = text;
  // Edit & resend: rewinds the chat (and reverts project files) to just
  // before this message, then resends the edited text. Hover-revealed.
  const edit = document.createElement("button");
  edit.className = "user-edit";
  edit.title = "Edit & resend";
  edit.setAttribute("aria-label", "Edit and resend this message");
  edit.innerHTML = PENCIL_SVG;
  edit.addEventListener("click", () => startEditUser(wrap));
  const copy = document.createElement("button");
  copy.className = "user-edit user-copy";
  copy.title = "Copy message";
  copy.setAttribute("aria-label", "Copy this message");
  copy.innerHTML = COPY_SVG;
  copy.addEventListener("click", () => {
    copyText(wrap.dataset.rawText || "")
      .then(() => toast("Message copied.", "info", 1500))
      .catch(() => toast("Couldn't copy to clipboard.", "error", 3000));
  });
  wrap.appendChild(copy);
  wrap.appendChild(edit);
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

/* Edit & resend a past message: rewind the chat to just before it (the
   backend reverts the project files to that turn's snapshot and truncates
   the conversation), then resend the edited text as a fresh turn. */
function startEditUser(wrap) {
  if (busy) { toast("Can't edit a message while the agent is working.", "warn", 3500); return; }
  if (wrap.querySelector(".user-edit-box")) return; // already editing this one
  const bubble = wrap.querySelector(".bubble-user");
  bubble.style.display = "none";
  const box = document.createElement("div");
  box.className = "user-edit-box";
  const ta = document.createElement("textarea");
  ta.className = "user-edit-ta";
  ta.value = wrap.dataset.rawText || "";
  const actions = document.createElement("div");
  actions.className = "user-edit-actions";
  const cancel = document.createElement("button");
  cancel.className = "btn btn-ghost"; cancel.textContent = "Cancel";
  const save = document.createElement("button");
  save.className = "btn btn-primary"; save.textContent = "Save & resend";
  actions.append(cancel, save);
  box.append(ta, actions);
  wrap.appendChild(box);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);

  const close = () => { box.remove(); bubble.style.display = ""; };
  cancel.addEventListener("click", close);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSave(); }
  });
  save.addEventListener("click", doSave);

  async function doSave() {
    const newText = ta.value.trim();
    if (!newText) { toast("Message can't be empty.", "warn", 2500); return; }
    // Turn ordinal = this bubble's position among all user bubbles, which is
    // exactly the send-turn order the backend counts.
    const ordinal = [...document.querySelectorAll(".msg-user")].indexOf(wrap);
    save.disabled = cancel.disabled = true;
    let res;
    try { res = await api().rewind_to(ordinal); }
    catch (e) { toast("Bridge error: " + e, "error", 6000); save.disabled = cancel.disabled = false; return; }
    if (res && res.error) { toast(res.error, "error", 5000); save.disabled = cancel.disabled = false; return; }
    // Re-render the truncated conversation, then resend the edited text (the
    // normal send path adds the fresh bubble + drives streaming).
    clearChatDom();
    renderHistory(res.items, res.todos);
    if (res.reverted) toast("Project files reverted to before that message.", "info", 3500);
    else if (res.had_snapshot === false)
      toast("Chat rewound. Files weren't reverted (no backup for that turn).", "info", 4500);
    input.value = newText;
    input.style.height = "auto";
    sendMessage();
  }
}

function handleBackgroundEvent(ev) {
  switch (ev.type) {
    case "chat_busy":
      busySessions.add(ev.sid);
      renderSidebar();
      break;
    case "bg_refresh":       // a scheduled task produced a new background chat
      if (ev.sessions) sessions = ev.sessions;
      if (ev.sid) unreadSessions.add(ev.sid);
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
      b.dataset.raw = it.text || "";  // for the Copy action (markdown source)
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
  const bar = document.createElement("div");
  bar.className = "msg-actions";
  bar.innerHTML =
    `<button class="msg-act msg-copy" title="Copy reply" aria-label="Copy reply">${COPY_SVG}</button>` +
    `<button class="msg-act msg-regen" title="Regenerate reply" aria-label="Regenerate reply">${RELOAD_SVG}</button>`;
  bar.querySelector(".msg-copy").addEventListener("click", () => copyTurn(wrap));
  bar.querySelector(".msg-regen").addEventListener("click", () => regenerateTurn(wrap));
  wrap.appendChild(bar);
  chatEl.appendChild(wrap);
  return wrap;
}

function copyTurn(wrap) {
  const parts = [...wrap.querySelectorAll(".bubble-assistant")]
    .map((b) => b.dataset.raw || b.textContent || "");
  const text = parts.join("\n\n").trim();
  if (!text) { toast("Nothing to copy yet.", "warn", 2000); return; }
  copyText(text)
    .then(() => toast("Reply copied.", "info", 1500))
    .catch(() => toast("Couldn't copy to clipboard.", "error", 3000));
}

async function regenerateTurn(wrap) {
  if (busy) { toast("Wait for the current turn to finish first.", "warn", 3000); return; }
  // The prompt for this reply is the nearest user bubble before this turn.
  let el = wrap.previousElementSibling;
  while (el && !el.classList.contains("msg-user")) el = el.previousElementSibling;
  if (!el) { toast("No prompt to regenerate from.", "warn", 2500); return; }
  const raw = el.dataset.rawText || "";
  const ordinal = [...document.querySelectorAll(".msg-user")].indexOf(el);
  let res;
  try { res = await api().rewind_to(ordinal); }
  catch (e) { toast("Bridge error: " + e, "error", 5000); return; }
  if (res && res.error) { toast(res.error, "error", 5000); return; }
  // Rewound to just before the prompt (files reverted too) -- resend it as-is.
  clearChatDom();
  renderHistory(res.items, res.todos);
  input.value = raw;
  input.style.height = "auto";
  sendMessage();
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

// The largest index > `from` that follows a blank line ("\n\n") AND sits at
// an even code-fence depth, so a committed chunk is always self-contained
// markdown (never cut inside a ``` block). content[0:from] is invariantly at
// even fence depth, so only fences in the NEW region need weighing.
function stableBoundary(content, from) {
  const region = content.slice(from);
  let idx = region.lastIndexOf("\n\n");
  while (idx >= 0) {
    const fences = (region.slice(0, idx).match(/```/g) || []).length;
    if (fences % 2 === 0) return from + idx + 2;
    idx = region.lastIndexOf("\n\n", idx - 1);
  }
  return from;
}

// Incremental markdown for a streaming bubble. The old path re-ran md() (full
// markdown parse + syntax highlighting) over the ENTIRE accumulated message on
// every ~80ms flush -- O(n) per frame, so O(n^2) over a reply, all on the
// WebView2 UI thread that also services every blocking evaluate_js from
// Python. Now: complete blocks (up to the last safe blank-line boundary) are
// rendered ONCE into stable DOM and never touched again; only the trailing
// in-progress block is re-rendered each frame -> O(tail) per frame.
function renderStreamingMarkdown(t) {
  const el = t.contentEl;
  if (!el) return;
  if (t._mdContentEl !== el) {          // a fresh bubble -> reset incremental state
    t._mdContentEl = el;
    t.mdCommitted = 0;
    el.innerHTML = "";
    t.tailEl = document.createElement("div");
    t.tailEl.className = "md-tail";     // display:contents -> layout-transparent
    el.appendChild(t.tailEl);
  }
  const safe = stableBoundary(t.content, t.mdCommitted);
  if (safe > t.mdCommitted) {
    const tmpl = document.createElement("template");
    tmpl.innerHTML = md(t.content.slice(t.mdCommitted, safe));
    el.insertBefore(tmpl.content, t.tailEl);
    t.mdCommitted = safe;
  }
  const tail = t.content.slice(t.mdCommitted);
  // Skip syntax highlighting only while the tail is a LONG, still-OPEN code
  // fence -- the one case that would otherwise re-highlight a growing block
  // from scratch every frame. Any closed fence (including the message's final
  // one, once the ``` arrives) highlights normally, so the finished render is
  // always fully highlighted with no separate finalize pass.
  const openFence = (tail.match(/```/g) || []).length % 2 === 1;
  t.tailEl.innerHTML = md(tail, openFence && tail.length > 400);
}

function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  setTimeout(() => {
    renderQueued = false;
    if (!current) return;
    if (current.contentEl && current.mdDirty) {
      renderStreamingMarkdown(current);
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
  // Speech-to-speech voice events (the delegator agent) are tagged with a
  // "<sid>::voice" sid and drive the voice overlay, not the coding transcript.
  if (ev.sid && ev.sid.endsWith("::voice")) {
    handleVoiceEvent(ev);
    return;
  }
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
          renderStreamingMarkdown(current);  // commit any pending block + final tail
          if (current.contentEl.parentElement)
            current.contentEl.parentElement.dataset.raw = current.content;
        } else if (current.content === "" && current.wrap.children.length <= 1) {
          // Only the .msg-actions bar -> the turn produced nothing; drop it.
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
      const el = buildToolEl(ev.name, ev.args || {}, ev.call_id || "");
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
    case "toast":  // transient side popup, never saved into the chat
      toast(ev.text, ev.level || "info", ev.level === "error" ? 8000 : 3500);
      break;
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
      noteBrowserAgent(ev);
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

// Voice mode hooks this to know when the spoken reply has fully finished
// playing (so it can resume listening). null in normal read-aloud.
let onTtsIdle = null;

function playNextTts() {
  if (ttsAudioQueue.length === 0) {
    ttsPlaying = false;
    ttsCurrentAudio = null;
    if (onTtsIdle) { try { onTtsIdle(); } catch (e) { /* ignore */ } }
    return;
  }
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
  const detail = (ev.status === "running" || ev.status === "paused")
    ? (ev.mission || ev.summary || "") : (ev.summary || "");
  row.querySelector(".subagent-detail").textContent = detail;
  const label = { running: "running…", paused: "paused — you drive",
                  done: "done", error: "failed" }[ev.status] || ev.status;
  row.querySelector(".subagent-status").textContent = label;
  // A Browser Agent can be paused so the human takes over the browser window,
  // then resumed (same agent). Only browser agents get this control.
  syncBrowserPauseBtn(row, ev);
  updateSubagentTabStatus(ev.id, ev.status);
}

function syncBrowserPauseBtn(row, ev) {
  const isBrowser = (ev.name || "") === "browser";
  const active = ev.status === "running" || ev.status === "paused";
  let btn = row.querySelector(".subagent-act");
  if (!isBrowser || !active) { if (btn) btn.remove(); return; }
  if (!btn) {
    btn = document.createElement("button");
    btn.className = "subagent-act";
    btn.addEventListener("click", async (e) => {
      e.stopPropagation(); // don't also open the inspector panel
      btn.disabled = true;
      const paused = row.dataset.status === "paused";
      let res;
      try { res = paused ? await api().resume_browser() : await api().pause_browser(); }
      catch (err) { res = { error: String(err) }; }
      btn.disabled = false;
      if (res && res.error) toast(res.error, "warn", 3000);
      else if (!paused) toast("Paused — the browser is yours. Click Resume when done.", "info", 5000);
    });
    row.appendChild(btn);
  }
  const paused = ev.status === "paused";
  btn.textContent = paused ? "Resume" : "Pause";
  btn.title = paused
    ? "Resume the browser agent — it re-reads the page first"
    : "Pause and take over the browser window yourself";
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

/* Live Browser panel: a viewport at the top of the sub-agent inspector that
   mirrors the Browser Agent's page (screenshots pushed as "browser_frame"
   events) with a Pause/Resume + take-over control. Shown only while the
   inspector is focused on a browser agent. */
const browserAgents = {};     // aid -> {url, image, paused, done, opened}

function noteBrowserAgent(ev) {
  if ((ev.name || "") !== "browser") return;
  const st = browserAgents[ev.id] ||
    (browserAgents[ev.id] = { url: "", image: "", paused: false, done: false });
  st.paused = ev.status === "paused";
  st.done = ev.status === "done" || ev.status === "error";
  // Auto-open the inspector on the browser the first time it runs, so the
  // user sees it working (and can pause it) without hunting for the row.
  if (ev.status === "running" && !st.opened) {
    st.opened = true;
    openSubagentPanel(ev.id, "browser", ev.status);
  }
  if (activeSubagentId === ev.id) refreshBrowserView();
}

function refreshBrowserView() {
  const view = $("browser-view");
  const st = activeSubagentId && browserAgents[activeSubagentId];
  if (!st) {
    view.hidden = true;
    // Fullscreen without a browser view would leave an empty half-screen.
    document.body.classList.remove("browser-full");
    return;
  }
  view.hidden = false;
  if (st.image) $("browser-view-img").src = st.image;
  $("browser-view-url").textContent = st.url || "…";
  const toggle = $("browser-view-toggle");
  toggle.hidden = st.done;
  toggle.textContent = st.paused ? "Resume" : "Pause";
  $("browser-view-takeover").hidden = !st.paused || st.done;
  view.classList.toggle("paused", st.paused && !st.done);
}

async function toggleBrowserPause() {
  const st = activeSubagentId && browserAgents[activeSubagentId];
  if (!st) return;
  const btn = $("browser-view-toggle");
  btn.disabled = true;
  let res;
  try { res = st.paused ? await api().resume_browser() : await api().pause_browser(); }
  catch (e) { res = { error: String(e) }; }
  btn.disabled = false;
  if (res && res.error) toast(res.error, "warn", 3000);
  else if (!st.paused) toast("Paused — the browser is yours. Resume when done.", "info", 5000);
}

$("browser-view-toggle").addEventListener("click", toggleBrowserPause);
$("browser-view-resume").addEventListener("click", toggleBrowserPause);

// Fullscreen browser mode: the live view takes the whole panel width and the
// sub-agent chat/actions shrink to a side column (see body.browser-full CSS).
$("browser-view-expand").addEventListener("click", () => {
  const on = document.body.classList.toggle("browser-full");
  $("browser-view-expand").title = on
    ? "Exit fullscreen (Esc)"
    : "Fullscreen — big browser view, chat at the side";
});

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
  if (ev.kind === "browser_frame") {
    const st = browserAgents[aid] ||
      (browserAgents[aid] = { url: "", image: "", paused: false, done: false });
    if (ev.url) st.url = ev.url;
    if (ev.image) st.image = ev.image;
    if (activeSubagentId === aid) refreshBrowserView();
    return;  // a live frame, not a transcript event
  }
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
      // call_id makes shell commands stoppable here too -- the foreground
      // process registry is process-global, so the same Stop plumbing the
      // main chat uses reaches a sub-agent's hung `npm run dev` as well.
      const el = buildToolEl(ev.name, ev.args || {}, ev.call_id || "");
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
    renderStreamingMarkdown(t);
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
  refreshBrowserView();  // show/hide the live viewport for the newly-active agent
  updateSubagentComposerState();
  renderSubagentSteerQueued();
  scrollSubagentPanel();
  // Read-aloud follows whichever thread is on screen: the main chat goes
  // silent while a sub-agent works anyway, so this is the one thing worth
  // hearing in the meantime.
  api().set_active_view(activeSubagentId || "");
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
  refreshBrowserView();  // reveal the live viewport iff this is a browser agent
  document.body.classList.add("subagent-open");
}

function closeSubagentPanel() {
  document.body.classList.remove("subagent-open");
  document.body.classList.remove("browser-full");
  api().set_active_view("");  // back to reading the main chat
}

function clearSubagentPanel() {
  document.body.classList.remove("subagent-open");
  document.body.classList.remove("browser-full");
  $("subagent-tabs").innerHTML = "";
  $("subagent-panel-body").innerHTML = "";
  for (const key of Object.keys(subagentThreads)) delete subagentThreads[key];
  for (const key of Object.keys(subagentSteerPending)) delete subagentSteerPending[key];
  for (const key of Object.keys(browserAgents)) delete browserAgents[key];
  activeSubagentId = null;
  $("browser-view").hidden = true;
  renderSubagentSteerQueued();
  api().set_active_view("");
}

$("subagent-panel-close").addEventListener("click", closeSubagentPanel);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !document.body.classList.contains("subagent-open")) return;
  // Two-stage Escape: leave fullscreen first, close the panel second.
  if (document.body.classList.contains("browser-full")) {
    document.body.classList.remove("browser-full");
    return;
  }
  closeSubagentPanel();
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

/* ---- @-mention file picker -------------------------------------------- --
   Type @ then part of a filename to fuzzy-search the project; pick a file and
   its current contents are attached to the message (server-side) so the agent
   gets exact context without hunting for it. */
let mention = { open: false, start: -1, items: [], sel: 0, seq: 0 };
let mentionTimer = null;

function mentionContext() {
  const pos = input.selectionStart;
  const upto = input.value.slice(0, pos);
  const at = upto.lastIndexOf("@");
  if (at < 0) return null;
  if (at > 0 && !/\s/.test(upto[at - 1])) return null; // @ must start a token
  const frag = upto.slice(at + 1);
  if (/\s/.test(frag)) return null;                     // whitespace ends it
  return { start: at, query: frag };
}

function scheduleMentionUpdate() {
  clearTimeout(mentionTimer);
  if (!mentionContext()) { closeMentionMenu(); return; }
  mentionTimer = setTimeout(updateMentionMenu, 70);
}

async function updateMentionMenu() {
  const ctx = mentionContext();
  if (!ctx) { closeMentionMenu(); return; }
  mention.start = ctx.start;
  const seq = ++mention.seq;
  let files = [];
  try { const r = await api().list_project_files(ctx.query); files = (r && r.files) || []; }
  catch (e) { files = []; }
  if (seq !== mention.seq) return;         // a newer keystroke already fired
  if (!files.length) { closeMentionMenu(); return; }
  mention.items = files;
  mention.sel = 0;
  mention.open = true;
  renderMentionMenu();
  $("mention-menu").hidden = false;
}

function renderMentionMenu() {
  const menu = $("mention-menu");
  menu.innerHTML = "";
  mention.items.forEach((f, i) => {
    const slash = f.lastIndexOf("/");
    const row = document.createElement("button");
    row.className = "mention-opt" + (i === mention.sel ? " sel" : "");
    row.setAttribute("role", "option");
    row.innerHTML = `<span class="mention-base"></span><span class="mention-dir"></span>`;
    row.querySelector(".mention-base").textContent = slash >= 0 ? f.slice(slash + 1) : f;
    row.querySelector(".mention-dir").textContent = slash >= 0 ? f.slice(0, slash + 1) : "";
    row.addEventListener("mousedown", (e) => { e.preventDefault(); chooseMention(i); });
    menu.appendChild(row);
  });
}

function moveMentionSel(delta) {
  mention.sel = (mention.sel + delta + mention.items.length) % mention.items.length;
  renderMentionMenu();
  const el = $("mention-menu").children[mention.sel];
  if (el) el.scrollIntoView({ block: "nearest" });
}

function chooseMention(i) {
  const file = mention.items[i];
  if (!file) return;
  const pos = input.selectionStart;
  const before = input.value.slice(0, mention.start);
  const insert = "@" + file + " ";
  input.value = before + insert + input.value.slice(pos);
  const caret = (before + insert).length;
  input.setSelectionRange(caret, caret);
  closeMentionMenu();
  input.focus();
  input.dispatchEvent(new Event("input")); // re-grow height
}

function closeMentionMenu() {
  mention.open = false;
  $("mention-menu").hidden = true;
}

/* ---- composer prompt history (ArrowUp / ArrowDown) -------------------- --
   Terminal-style recall of your previous messages, per chat. ArrowUp only
   takes over when it can't mean cursor movement (empty input, or caret at
   position 0); ArrowDown walks back toward the draft you were typing. */
let histIdx = null;   // null = not navigating
let histDraft = "";
let histApplying = false;

function histKey() { return "mnm-hist-" + (activeSessionId || "none"); }
function histList() {
  try { return JSON.parse(localStorage.getItem(histKey()) || "[]"); }
  catch (e) { return []; }
}
function histPush(text) {
  try {
    const h = histList();
    if (h[h.length - 1] !== text) h.push(text);
    localStorage.setItem(histKey(), JSON.stringify(h.slice(-50)));
  } catch (e) { /* localStorage unavailable -- history just won't persist */ }
  histIdx = null;
}
function histApply(v) {
  histApplying = true;
  input.value = v;
  input.dispatchEvent(new Event("input")); // re-grow height
  histApplying = false;
  const end = input.value.length;
  input.setSelectionRange(end, end);
}

input.addEventListener("keydown", (e) => {
  if (mention.open) return; // the mention menu owns the arrows while open
  if (e.key === "ArrowUp") {
    const h = histList();
    if (!h.length) return;
    if (histIdx === null) {
      if (input.value !== "" && input.selectionStart !== 0) return; // normal cursor move
      histDraft = input.value;
      histIdx = h.length - 1;
    } else if (histIdx > 0) {
      histIdx--;
    } else {
      e.preventDefault();
      return; // already at the oldest entry
    }
    e.preventDefault();
    histApply(h[histIdx]);
  } else if (e.key === "ArrowDown" && histIdx !== null) {
    const h = histList();
    e.preventDefault();
    if (histIdx < h.length - 1) {
      histIdx++;
      histApply(h[histIdx]);
    } else {
      histIdx = null;
      histApply(histDraft); // back to whatever was being typed
    }
  }
});
input.addEventListener("input", () => { if (!histApplying) histIdx = null; });

input.addEventListener("input", scheduleMentionUpdate);
input.addEventListener("blur", () => setTimeout(closeMentionMenu, 120));
// Capture phase so this runs BEFORE the Enter-to-send handler above: while the
// menu is open, Enter/Tab pick a file and Arrows navigate instead of sending.
input.addEventListener("keydown", (e) => {
  if (!mention.open) return;
  if (e.key === "ArrowDown") { e.preventDefault(); e.stopImmediatePropagation(); moveMentionSel(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); e.stopImmediatePropagation(); moveMentionSel(-1); }
  else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopImmediatePropagation(); chooseMention(mention.sel); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeMentionMenu(); }
}, true);

/* ---- slash commands (/) ----------------------------------------------- --
   Type / at the start of the composer to run a built-in action or one of
   your saved prompt commands. Menu is discovery; the actual dispatch happens
   on send (so `/review focus on auth` works when typed out and Entered). */
let slashCommands = [];  // custom {name, template} from the backend
let slash = { open: false, items: [], sel: 0 };

const BUILTIN_COMMANDS = [
  { name: "plan", hint: "plan the task before touching anything", builtin: true },
  { name: "compact", hint: "summarize older history to free context", builtin: true },
  { name: "new", hint: "start a fresh chat in this project", builtin: true },
];

function allSlashCommands() {
  return BUILTIN_COMMANDS.concat(
    (slashCommands || []).map((c) => ({ name: c.name, hint: c.template, template: c.template })));
}

function slashContext() {
  const m = /^\/([\w-]*)$/.exec(input.value); // only "/word" with nothing after
  return m ? { query: m[1].toLowerCase() } : null;
}

function updateSlashMenu() {
  const ctx = slashContext();
  if (!ctx) { closeSlashMenu(); return; }
  slash.items = allSlashCommands().filter((c) => c.name.toLowerCase().startsWith(ctx.query));
  if (!slash.items.length) { closeSlashMenu(); return; }
  slash.sel = 0; slash.open = true;
  renderSlashMenu();
  $("slash-menu").hidden = false;
}

function renderSlashMenu() {
  const menu = $("slash-menu");
  menu.innerHTML = "";
  slash.items.forEach((c, i) => {
    const row = document.createElement("button");
    row.className = "mention-opt" + (i === slash.sel ? " sel" : "");
    row.setAttribute("role", "option");
    row.innerHTML = `<span class="mention-base"></span><span class="mention-dir"></span>`;
    row.querySelector(".mention-base").textContent = "/" + c.name;
    row.querySelector(".mention-dir").textContent =
      (c.builtin ? "" : "custom · ") + (c.hint || "").replace(/\s+/g, " ").slice(0, 70);
    row.addEventListener("mousedown", (e) => { e.preventDefault(); chooseSlash(i); });
    menu.appendChild(row);
  });
}

function moveSlashSel(d) {
  slash.sel = (slash.sel + d + slash.items.length) % slash.items.length;
  renderSlashMenu();
  const el = $("slash-menu").children[slash.sel];
  if (el) el.scrollIntoView({ block: "nearest" });
}

function chooseSlash(i) {
  const c = slash.items[i];
  if (!c) return;
  closeSlashMenu();
  input.value = "/" + c.name + " ";   // tab-complete; Enter dispatches it
  input.focus();
  input.dispatchEvent(new Event("input"));
}

function closeSlashMenu() { slash.open = false; $("slash-menu").hidden = true; }

// Returns {send} (send this text), {consumed} (handled, nothing to send),
// or null (unknown command -- send as a normal message).
function dispatchSlash(name, args) {
  const custom = (slashCommands || []).find((c) => c.name === name);
  if (custom) {
    let t = custom.template;
    if (t.includes("$INPUT")) t = t.split("$INPUT").join(args);
    else if (args) t = t + "\n\n" + args;
    return { send: t.trim() };
  }
  if (name === "plan") {
    setPlanMode(true);
    return args ? { send: args } : { consumed: true };
  }
  if (name === "compact") { $("compact-btn").click(); return { consumed: true }; }
  if (name === "new" || name === "clear") { $("chat-clear").click(); return { consumed: true }; }
  return null;
}

input.addEventListener("input", updateSlashMenu);
input.addEventListener("blur", () => setTimeout(closeSlashMenu, 120));
input.addEventListener("keydown", (e) => {
  if (!slash.open) return;
  if (e.key === "ArrowDown") { e.preventDefault(); e.stopImmediatePropagation(); moveSlashSel(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); e.stopImmediatePropagation(); moveSlashSel(-1); }
  else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopImmediatePropagation(); chooseSlash(slash.sel); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeSlashMenu(); }
}, true);

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
  // Model can't change mid-turn (the backend refuses too) -- reflect that on
  // the top selector and never leave its menu open across a turn start.
  const mc = $("model-chip");
  if (mc) mc.disabled = b;
  if (b) closeModelMenu();
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
  // Slash command dispatch (only when idle and the whole input is /cmd …).
  if (!busy) {
    const sm = /^\/([\w-]+)(?:\s+([\s\S]*))?$/.exec(input.value.trim());
    if (sm) {
      const r = dispatchSlash(sm[1], (sm[2] || "").trim());
      if (r && r.consumed) { input.value = ""; input.style.height = "auto"; closeSlashMenu(); return; }
      if (r && r.send != null) { input.value = r.send; input.style.height = "auto"; closeSlashMenu(); }
      // r === null -> unknown command; fall through and send it verbatim.
    }
  }
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
    histPush(text);
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
  if (text) histPush(text);
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
function setPlanMode(on) {
  planMode = !!on;
  $("plan-toggle").classList.toggle("on", planMode);
  $("plan-toggle").setAttribute("aria-pressed", String(planMode));
  input.placeholder = planMode
    ? "Describe the task — the agent will plan it before touching anything…"
    : "Ask anything…  (type @ to add a file)";
  if (!planMode) $("plan-actions").hidden = true;
}
$("plan-toggle").addEventListener("click", () => setPlanMode(!planMode));
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

/* ---- dictation (voice -> composer text) -------------------------------- --
   Click the mic to record; click again to stop, transcribe locally (faster-
   whisper via Api.transcribe_audio), and drop the text into the composer. */
const mic = { rec: null, stream: null, chunks: [], busy: false };

function setMicState(state) {
  const btn = $("mic-btn");
  btn.classList.toggle("recording", state === "recording");
  btn.classList.toggle("busy", state === "busy");
  btn.title = state === "recording" ? "Stop & transcribe"
    : state === "busy" ? "Transcribing…"
    : "Dictate — click to record, click again to transcribe into the box";
}

async function startDictation() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("This build can't access the microphone.", "error", 5000);
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    toast("Microphone permission was denied.", "error", 5000);
    return;
  }
  mic.stream = stream;
  mic.chunks = [];
  const rec = new MediaRecorder(stream);
  mic.rec = rec;
  rec.addEventListener("dataavailable", (e) => { if (e.data.size) mic.chunks.push(e.data); });
  rec.addEventListener("stop", finishDictation);
  rec.start();
  setMicState("recording");
}

function finishDictation() {
  const stream = mic.stream;
  if (stream) stream.getTracks().forEach((t) => t.stop());
  mic.stream = null;
  const blob = new Blob(mic.chunks, { type: (mic.rec && mic.rec.mimeType) || "audio/webm" });
  mic.rec = null;
  mic.chunks = [];
  if (!blob.size) { setMicState("idle"); return; }
  setMicState("busy");
  mic.busy = true;
  const reader = new FileReader();
  reader.onload = async () => {
    let res;
    try { res = await api().transcribe_audio(reader.result); }
    catch (e) { res = { error: String(e) }; }
    mic.busy = false;
    setMicState("idle");
    if (res && res.error) { toast(res.error, "error", 6000); return; }
    const text = (res && res.text || "").trim();
    if (!text) { toast("Didn't catch anything — try again.", "info", 3000); return; }
    // Insert at the cursor (or append), then let the composer grow/refocus.
    const sep = input.value && !/\s$/.test(input.value) ? " " : "";
    input.value = input.value + sep + text;
    input.dispatchEvent(new Event("input"));
    input.focus();
  };
  reader.readAsDataURL(blob);
}

$("mic-btn").addEventListener("click", () => {
  if (mic.busy) return;
  if (mic.rec && mic.rec.state === "recording") mic.rec.stop();
  else startDictation();
});

/* ---- drag & drop attachments ------------------------------------------ --
   Drop files anywhere on the window to attach them. pywebview exposes each
   dropped File's real disk path as `pywebviewFullPath`; the backend turns
   those into the same {path, name, thumb} shape pick_files returns. */
let dragDepth = 0; // dragenter/leave fire per descendant element -- count them

function dropOverlay(show) {
  $("drop-overlay").hidden = !show;
}

window.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes("Files")) return;
  e.preventDefault();
  dragDepth++;
  dropOverlay(true);
});
window.addEventListener("dragover", (e) => {
  if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes("Files")) return;
  e.preventDefault();
});
window.addEventListener("dragleave", (e) => {
  if ($("drop-overlay").hidden) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropOverlay(false);
});
window.addEventListener("drop", (e) => {
  // The real disk paths of dropped files come from the PYTHON side (a
  // window.dom.document.events.drop handler in app.py). Browsers hide local
  // paths from JS for security, and pywebview injects them ONLY into the
  // Python drop event (as pywebviewFullPath) -- they are simply not readable
  // from JavaScript. So here we just stop the browser from opening the file
  // and hide the overlay; Python then calls window.__onDropResult with the
  // resolved attachments.
  e.preventDefault();
  dragDepth = 0;
  dropOverlay(false);
});

/* ---- paste a screenshot ----------------------------------------------- --
   Win+Shift+S puts a screenshot on the clipboard; Ctrl+V here attaches it.
   The image arrives as a Blob (no disk path), so it's handed to Python as a
   data URL, saved to a real file, and comes back in the same {path, name,
   thumb} shape every other attachment uses. Text pastes are untouched. */
document.addEventListener("paste", (e) => {
  const items = [...((e.clipboardData && e.clipboardData.items) || [])];
  const imgs = items.filter((it) => it.kind === "file" && it.type.startsWith("image/"));
  if (!imgs.length) return; // plain text paste -- let the browser handle it
  e.preventDefault();
  for (const it of imgs) {
    const file = it.getAsFile();
    if (!file) continue;
    const reader = new FileReader();
    reader.onload = async () => {
      let att;
      try { att = await api().paste_image(reader.result); }
      catch (err) { toast("Couldn't attach pasted image: " + err, "error", 4000); return; }
      if (!att || att.error) { toast((att && att.error) || "Couldn't attach pasted image.", "error", 4000); return; }
      attachments.push(att);
      renderAttachments();
      input.focus();
    };
    reader.readAsDataURL(file);
  }
});

// Called from Python's drop handler (app.py Api._on_drop) with the attachment
// objects it resolved from the dropped files' real paths.
window.__onDropResult = function (atts) {
  dragDepth = 0;
  dropOverlay(false);
  if (atts && atts.length) {
    attachments.push(...atts);
    renderAttachments();
    input.focus();
  }
};

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
const MODE_LABEL = { ask: "Ask", autoedit: "Auto-edit", yolo: "Full auto" };
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

const THINK_MODES = ["low", "medium", "high", "max"];
const THINK_LABEL = { low: "Low", medium: "Medium", high: "High", max: "Max" };
const THINK_TIP = {
  low: "Low — answers instantly, no reasoning",
  medium: "Medium — thinks before answering",
  high: "High — reviews and improves its own answer once",
  max: "Max — reviews and improves repeatedly (up to 3×)",
};
function applyThinkChip() {
  const chip = $("think-chip");
  if (!chip) return;
  const m = settings.thinking_mode || "medium";
  chip.textContent = THINK_LABEL[m] || m;
  chip.title = THINK_TIP[m] || "Thinking effort — click to cycle";
  chip.className = "chip chip-btn" + (m === "high" || m === "max" ? " think-hi" : "");
}
$("think-chip").addEventListener("click", async () => {
  const cur = settings.thinking_mode || "medium";
  const next = THINK_MODES[(THINK_MODES.indexOf(cur) + 1) % THINK_MODES.length];
  settings = await api().set_setting("thinking_mode", next);
  applyThinkChip();
  syncSettingsUI();
  toast(THINK_TIP[next], "info", 2600);
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
      const eng = currentTtsEngine() === "piper" ? "Piper" : "Kokoro";
      const proceed = confirm(
        `Reading replies aloud uses local text-to-speech (${eng}). The first time, this ` +
        "downloads the model (one-time) and installs in the background -- everything " +
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

function currentTtsEngine() { return (settings && settings.tts_engine) || "kokoro"; }
function currentTtsVoice() {
  return currentTtsEngine() === "piper"
    ? (settings.piper_voice || "en_US-amy-medium")
    : (settings.tts_voice || "af_heart");
}
async function populateVoiceSelect() {
  const engine = currentTtsEngine();
  $("voice-engine").value = engine;
  const sel = $("voice-select");
  const res = await api().tts_voices(engine);
  const voices = (res && res.voices) || [];
  sel.innerHTML = voices.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  const want = currentTtsVoice();
  if (voices.includes(want)) sel.value = want;
  const status = await api().tts_status();
  $("voice-first-use-note").hidden = !!status.ready;
}
$("voice-engine").addEventListener("change", async () => {
  settings = await api().set_setting("tts_engine", $("voice-engine").value);
  await populateVoiceSelect();
});
$("voice-select").addEventListener("change", async () => {
  const key = currentTtsEngine() === "piper" ? "piper_voice" : "tts_voice";
  settings = await api().set_setting(key, $("voice-select").value);
  const status = await api().tts_status();
  $("voice-first-use-note").hidden = !!status.ready;
});
$("voice-preview-btn").addEventListener("click", async () => {
  const btn = $("voice-preview-btn");
  if (btn.classList.contains("loading")) return;
  btn.classList.add("loading");
  try {
    const res = await api().preview_voice($("voice-select").value, currentTtsEngine());
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

/* ------------------------------------------------ dictation (STT) */

// faster-whisper sizes, smallest→largest. .en variants are English-only and a
// bit sharper for English; distil-small.en is small+fast. Labels carry the
// rough download size so the one-time cost isn't a surprise.
const STT_MODELS = [
  ["tiny",            "Tiny — fastest, least accurate (~75MB)"],
  ["base",            "Base — good balance (~145MB)"],
  ["small",           "Small — more accurate, slower (~480MB)"],
  ["medium",          "Medium — most accurate, slowest (~1.5GB)"],
  ["tiny.en",         "Tiny (English only) (~75MB)"],
  ["base.en",         "Base (English only) (~145MB)"],
  ["small.en",        "Small (English only) (~480MB)"],
  ["distil-small.en", "Distil-Small (English only) — fast (~330MB)"],
];
// Short list of common languages; empty value = auto-detect.
const STT_LANGS = [
  ["", "Auto-detect"], ["en", "English"], ["sv", "Swedish"], ["es", "Spanish"],
  ["fr", "French"], ["de", "German"], ["it", "Italian"], ["pt", "Portuguese"],
  ["nl", "Dutch"], ["pl", "Polish"], ["ru", "Russian"], ["uk", "Ukrainian"],
  ["zh", "Chinese"], ["ja", "Japanese"], ["ko", "Korean"], ["hi", "Hindi"],
  ["ar", "Arabic"], ["tr", "Turkish"],
];
async function populateSttSelect() {
  const mSel = $("stt-model");
  mSel.innerHTML = STT_MODELS.map(
    ([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
  mSel.value = settings.stt_model || "base";
  const lSel = $("stt-language");
  lSel.innerHTML = STT_LANGS.map(
    ([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
  lSel.value = settings.stt_language || "";
  try {
    const status = await api().stt_status(mSel.value);
    $("stt-first-use-note").hidden = !!status.ready;
  } catch { $("stt-first-use-note").hidden = true; }
}
$("stt-model").addEventListener("change", async () => {
  settings = await api().set_setting("stt_model", $("stt-model").value);
  try {
    const status = await api().stt_status($("stt-model").value);
    $("stt-first-use-note").hidden = !!status.ready;
  } catch { $("stt-first-use-note").hidden = true; }
});
$("stt-language").addEventListener("change", async () => {
  settings = await api().set_setting("stt_language", $("stt-language").value);
});

function sensitivityLabel(v) {
  if (v <= 0.7) return "Less sensitive — needs louder, clearer speech";
  if (v >= 1.6) return "Very sensitive — picks up quiet speech (and more noise)";
  if (v >= 1.2) return "More sensitive";
  return "Normal";
}
$("voice-sensitivity").addEventListener("input", () => {
  $("voice-sensitivity-label").textContent = sensitivityLabel(parseFloat($("voice-sensitivity").value));
});
$("voice-sensitivity").addEventListener("change", async () => {
  const v = parseFloat($("voice-sensitivity").value);
  settings = await api().set_setting("voice_sensitivity", v);
  voice.sens = v;  // take effect immediately if a voice session is open
});

$("voice-reply-lang").addEventListener("change", async () => {
  settings = await api().set_setting("voice_reply_language", $("voice-reply-lang").value);
});

function silenceLabel(ms) { return (ms / 1000).toFixed(2).replace(/0$/, "") + "s"; }
$("voice-silence").addEventListener("input", () => {
  $("voice-silence-label").textContent = silenceLabel(parseInt($("voice-silence").value, 10));
});
$("voice-silence").addEventListener("change", async () => {
  const v = parseInt($("voice-silence").value, 10);
  settings = await api().set_setting("voice_silence_ms", v);
  voice.endpointMs = v;
});
$("opt-earcons").addEventListener("click", async () => {
  const next = $("opt-earcons").getAttribute("aria-checked") !== "true";
  settings = await api().set_setting("voice_earcons", next);
  $("opt-earcons").setAttribute("aria-checked", String(next));
});
// Push-to-talk key capture: click, then press any key to bind it.
let pttKeyCapturing = false;
$("voice-ptt-key").addEventListener("click", () => {
  pttKeyCapturing = true;
  $("voice-ptt-key").textContent = "Press a key…";
});
window.addEventListener("keydown", async (e) => {
  if (!pttKeyCapturing) return;
  e.preventDefault();
  pttKeyCapturing = false;
  const code = e.code || "Space";
  settings = await api().set_setting("voice_ptt_key", code);
  voice.pttKey = code;
  $("voice-ptt-key").textContent = "Click, then press a key";
  $("voice-ptt-key-label").textContent = pttKeyName(code);
}, true);
function pttKeyName(code) {
  return String(code || "Space").replace(/^Key/, "").replace(/^Digit/, "").replace(/^Arrow/, "");
}
$("opt-wake").addEventListener("click", async () => {
  const next = $("opt-wake").getAttribute("aria-checked") !== "true";
  settings = await api().set_setting("voice_wake_enabled", next);
  $("opt-wake").setAttribute("aria-checked", String(next));
  $("wake-word-row").hidden = !next;
  refreshWake();
});
$("voice-wake-word").addEventListener("change", async () => {
  const v = $("voice-wake-word").value.trim() || "hey assistant";
  settings = await api().set_setting("voice_wake_word", v);
  if (wake.armed) { disarmWake(); refreshWake(); }  // pick up the new phrase
});

/* ---------------------------------------------- model providers (BYOM) -- */

let providersCache = null;
let provFormEditing = null; // original name of the API being edited; null = adding

function refreshModelFoot(res) {
  if (!res) return;
  const builtin = res.chat_provider === (providersCache?.providers?.[0]?.name || "z.ai (free)");
  $("model-foot").textContent = builtin
    ? `${res.chat_model} via z.ai — always $0.00`
    : `${res.chat_model} via ${res.chat_provider}`;
}

const PENCIL_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
const COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const CROSS_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

function apiRowSub(p) {
  if (p.builtin) {
    return p.has_key ? "free default — always $0.00"
      : "no API key yet — edit this row and paste yours";
  }
  return `${p.base_url} · ${p.models.length} model${p.models.length === 1 ? "" : "s"}`;
}

function renderApiList(res) {
  const list = $("api-list");
  list.innerHTML = "";
  for (const p of res.providers) {
    const selected = p.name === res.chat_provider;
    const row = document.createElement("div");
    row.className = "provider-row api-row" + (selected ? " selected" : "");
    row.innerHTML =
      `<span class="api-radio"></span>` +
      `<div class="provider-row-text"><span class="provider-name"></span>` +
      `<span class="provider-sub"></span></div>` +
      `<button class="icon-btn-mini api-edit" aria-label="Edit API" title="Edit">${PENCIL_SVG}</button>`;
    row.querySelector(".provider-name").textContent = p.name;
    row.querySelector(".provider-sub").textContent = apiRowSub(p);
    // The selected row exposes its model choice inline (built-in excluded:
    // its chat model is the free default, vision routes automatically).
    if (selected && !p.builtin && p.models.length) {
      const sel = document.createElement("select");
      sel.className = "voice-select api-model-select";
      sel.innerHTML = p.models.map((m) => `<option>${esc(m)}</option>`).join("");
      if (p.models.includes(res.chat_model)) sel.value = res.chat_model;
      sel.addEventListener("change", () => selectApi(p, sel.value));
      row.insertBefore(sel, row.querySelector(".api-edit"));
    }
    row.querySelector(".api-edit").addEventListener("click", (e) => {
      e.stopPropagation();
      openApiForm(p, p.name);
    });
    if (!p.builtin) {
      const del = document.createElement("button");
      del.className = "icon-btn-mini";
      del.setAttribute("aria-label", "Delete API");
      del.title = "Delete";
      del.innerHTML = CROSS_SVG;
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        const res2 = await api().delete_provider(p.name);
        populateModelPicker(res2);
      });
      row.appendChild(del);
    }
    if (!selected) row.addEventListener("click", () => selectApi(p));
    list.appendChild(row);
  }
}

async function populateModelPicker(data) {
  const res = data || await api().providers();
  if (!res || !res.providers) return;
  providersCache = res;
  renderApiList(res);
  refreshModelFoot(res);
  renderModelChip(res);
  buildModelMenu(res);
}

/* ---- top-of-chat model selector: one flat list of every model ---------- */
// Each configured model is its own equal entry -- no grouping by provider.
// The built-in z.ai row contributes only its chat model (its vision model
// routes automatically and isn't a chat choice); custom APIs contribute
// every model they list.
function modelEntries(res) {
  const out = [];
  for (const p of res.providers || []) {
    const models = p.builtin ? (p.models || []).slice(0, 1) : (p.models || []);
    for (const m of models) out.push({ provider: p.name, model: m, builtin: !!p.builtin });
  }
  return out;
}

function renderModelChip(res) {
  $("model-chip-label").textContent = res.chat_model || "model";
  $("model-chip").title = `Model: ${res.chat_model} (via ${res.chat_provider}) — click to switch`;
}

const CHECK_SVG = '<svg class="model-opt-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

function buildModelMenu(res) {
  const menu = $("model-menu");
  menu.innerHTML = "";
  const entries = modelEntries(res);
  if (!entries.length) {
    menu.innerHTML = '<div class="model-menu-empty">No models configured.</div>';
  }
  for (const e of entries) {
    const selected = e.provider === res.chat_provider && e.model === res.chat_model;
    const opt = document.createElement("button");
    opt.className = "model-opt" + (selected ? " selected" : "");
    opt.setAttribute("role", "option");
    opt.setAttribute("aria-selected", String(selected));
    opt.innerHTML = CHECK_SVG +
      `<span class="model-opt-text"><span class="model-opt-name"></span>` +
      `<span class="model-opt-prov"></span></span>`;
    opt.querySelector(".model-opt-name").textContent = e.model;
    opt.querySelector(".model-opt-prov").textContent = e.provider;
    opt.addEventListener("click", () => selectModel(e));
    menu.appendChild(opt);
  }
  // A quick path to configure more, since this menu is where you'd look.
  const foot = document.createElement("div");
  foot.className = "model-menu-foot";
  foot.innerHTML = '<button class="model-menu-add">+ Add or manage APIs…</button>';
  foot.querySelector("button").addEventListener("click", () => {
    closeModelMenu();
    openSettingsToApis();
  });
  menu.appendChild(foot);
}

function openModelMenu() {
  if (busy) return;  // model can't change mid-turn
  $("model-menu").hidden = false;
  $("model-chip").setAttribute("aria-expanded", "true");
}
function closeModelMenu() {
  $("model-menu").hidden = true;
  $("model-chip").setAttribute("aria-expanded", "false");
}
function toggleModelMenu() {
  $("model-menu").hidden ? openModelMenu() : closeModelMenu();
}

async function selectModel(entry) {
  closeModelMenu();
  const res = await api().set_chat_model(entry.provider, entry.builtin ? "" : entry.model);
  if (res && res.error) { toast(res.error, "error", 5000); return; }
  populateModelPicker(res);
  toast(`This chat now uses ${res.chat_model}.`, "info", 2000);
}

function openSettingsToApis() {
  openSettings();
  showSettingsTab("models");
  setTimeout(() => {
    const el = $("api-list");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 60);
}

$("model-chip").addEventListener("click", (e) => { e.stopPropagation(); toggleModelMenu(); });
document.addEventListener("click", (e) => {
  if (!$("model-menu").hidden && !$("model-select-wrap").contains(e.target)) closeModelMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("model-menu").hidden) closeModelMenu();
});

async function selectApi(p, model) {
  const res = await api().set_chat_model(
    p.name, model || (p.builtin ? "" : (p.models[0] || "")));
  if (res && res.error) {
    toast(res.error, "error", 5000);
    populateModelPicker();
    return;
  }
  populateModelPicker(res);
  toast(`This chat now uses ${res.chat_model}.`, "info", 2500);
}

function openApiForm(prefill, editingName) {
  provFormEditing = editingName || null;
  const isBuiltin = !!prefill.builtin;
  $("api-form").hidden = false;
  $("prov-name").value = prefill.name || "";
  $("prov-url").value = prefill.base_url || "";
  $("prov-models").value = (prefill.models || []).join(", ");
  $("prov-key").value = "";
  // The built-in z.ai API is fixed except for the key.
  for (const id of ["prov-name", "prov-url", "prov-models"]) $(id).disabled = isBuiltin;
  $("prov-key").placeholder = isBuiltin
    ? "Paste your free z.ai API key (z.ai → profile → API Keys)"
    : editingName ? "New API key (empty = keep the current one)"
      : "API key (leave empty for local servers)";
  $("prov-key").focus();
}

function closeApiForm() {
  provFormEditing = null;
  $("api-form").hidden = true;
  for (const id of ["prov-name", "prov-url", "prov-key", "prov-models"]) {
    $(id).value = "";
    $(id).disabled = false;
  }
}

$("api-add").addEventListener("click", () => {
  const ps = providersCache?.providers || [];
  const builtin = ps.find((p) => p.builtin);
  // First time here with nothing configured at all: pre-fill the z.ai
  // template so the only thing left to type is the key.
  if (builtin && !builtin.has_key && ps.length <= 1) openApiForm(builtin, builtin.name);
  else openApiForm({}, null);
});
$("prov-cancel").addEventListener("click", closeApiForm);

$("prov-save").addEventListener("click", async () => {
  const res = await api().save_provider(provFormEditing || "", $("prov-name").value,
    $("prov-url").value, $("prov-key").value, $("prov-models").value);
  if (res && res.error) { toast(res.error, "error", 6000); return; }
  closeApiForm();
  populateModelPicker(res);
  if (res.persisted_env === false) {
    toast("Key saved to config (couldn't set the ZAI_API_KEY environment variable).", "info", 5000);
  } else {
    toast("API saved.", "info", 3000);
  }
});

/* ---- MCP servers (Settings) ------------------------------------------- */

const RELOAD_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>';

function renderMcpList(res) {
  const list = $("mcp-list");
  list.innerHTML = "";
  for (const s of (res && res.servers) || []) {
    const row = document.createElement("div");
    row.className = "provider-row";
    row.innerHTML =
      `<span class="mcp-dot"></span>` +
      `<div class="provider-row-text"><span class="provider-name"></span>` +
      `<span class="provider-sub"></span></div>` +
      `<button class="icon-btn-mini mcp-restart" title="Restart">${RELOAD_SVG}</button>` +
      `<button class="icon-btn-mini mcp-del" title="Delete">${CROSS_SVG}</button>`;
    row.querySelector(".mcp-dot").classList.add(s.running ? "on" : "off");
    row.querySelector(".provider-name").textContent = s.name;
    row.querySelector(".provider-sub").textContent = s.running
      ? `${s.tools.length} tool${s.tools.length === 1 ? "" : "s"} · ${s.tools.slice(0, 4).join(", ")}${s.tools.length > 4 ? "…" : ""}`
      : (s.error ? `error: ${s.error}` : "starting…");
    row.querySelector(".mcp-restart").addEventListener("click", async () => {
      await api().mcp_restart(s.name);
      toast(`Restarting ${s.name}…`, "info", 2500);
      setTimeout(populateMcp, 2500);
    });
    row.querySelector(".mcp-del").addEventListener("click", async () => {
      renderMcpList(await api().mcp_delete(s.name));
    });
    list.appendChild(row);
  }
  if (!list.children.length) {
    list.innerHTML = '<div class="row-sub">No MCP servers configured.</div>';
  }
}

async function populateMcp() {
  try { renderMcpList(await api().mcp_status()); } catch (e) { /* ignore */ }
}

$("mcp-add").addEventListener("click", async () => {
  const res = await api().mcp_add($("mcp-name").value, $("mcp-command").value);
  if (res && res.error) { toast(res.error, "error", 5000); return; }
  $("mcp-name").value = ""; $("mcp-command").value = "";
  renderMcpList(res);
  toast("MCP server added — starting it now…", "info", 3500);
  setTimeout(populateMcp, 3000); // refresh once it has had a moment to boot
});

/* ---- custom slash commands (Settings) --------------------------------- */

function renderCommandsList(res) {
  slashCommands = (res && res.commands) || [];
  const list = $("commands-list");
  list.innerHTML = "";
  for (const c of slashCommands) {
    const row = document.createElement("div");
    row.className = "provider-row";
    row.innerHTML =
      `<div class="provider-row-text"><span class="provider-name"></span>` +
      `<span class="provider-sub"></span></div>` +
      `<button class="icon-btn-mini cmd-del" title="Delete">${CROSS_SVG}</button>`;
    row.querySelector(".provider-name").textContent = "/" + c.name;
    row.querySelector(".provider-sub").textContent = c.template.replace(/\s+/g, " ").slice(0, 80);
    row.querySelector(".cmd-del").addEventListener("click", async () => {
      renderCommandsList(await api().delete_command(c.name));
    });
    list.appendChild(row);
  }
  if (!list.children.length) {
    list.innerHTML = '<div class="row-sub">No custom commands yet.</div>';
  }
}

async function populateCommands() {
  try { renderCommandsList(await api().commands()); } catch (e) { /* ignore */ }
}

$("cmd-add").addEventListener("click", async () => {
  const res = await api().add_command($("cmd-name").value, $("cmd-template").value);
  if (res && res.error) { toast(res.error, "error", 5000); return; }
  $("cmd-name").value = ""; $("cmd-template").value = "";
  renderCommandsList(res);
  toast("Command saved — run it with /name in the composer.", "info", 3500);
});

/* ---- scoped autonomy: per-path access rules (Settings) ---------------- */

let pathRuleAction = "allow";
document.querySelectorAll("#pathrule-action button").forEach((b) =>
  b.addEventListener("click", () => {
    pathRuleAction = b.dataset.v;
    document.querySelectorAll("#pathrule-action button").forEach((x) => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-checked", x === b);
    });
  }));

const RULE_LABEL = { allow: "Allow", ask: "Ask", deny: "Deny" };
function ruleHint(a) {
  return a === "deny" ? "edits blocked — even in Full auto"
    : a === "ask" ? "always prompts — even in Full auto"
    : "auto-approved — even in Ask mode";
}

function renderPathRules() {
  const list = $("pathrules-list");
  if (!list) return;
  const rules = (settings && settings.path_rules) || [];
  list.innerHTML = "";
  for (const r of rules) {
    const row = document.createElement("div");
    row.className = "provider-row";
    row.innerHTML =
      `<span class="rule-badge rule-${r.action}"></span>` +
      `<div class="provider-row-text"><span class="provider-name mono"></span>` +
      `<span class="provider-sub"></span></div>` +
      `<button class="icon-btn-mini rule-del" title="Delete">${CROSS_SVG}</button>`;
    row.querySelector(".rule-badge").textContent = RULE_LABEL[r.action] || r.action;
    row.querySelector(".provider-name").textContent = r.glob;
    row.querySelector(".provider-sub").textContent = ruleHint(r.action);
    row.querySelector(".rule-del").addEventListener("click", async () => {
      const next = rules.filter((x) => !(x.glob === r.glob && x.action === r.action));
      settings = await api().set_setting("path_rules", next);
      renderPathRules();
    });
    list.appendChild(row);
  }
  if (!list.children.length) {
    list.innerHTML =
      '<div class="row-sub">No access rules — the permission mode applies everywhere.</div>';
  }
}

$("pathrule-add").addEventListener("click", async () => {
  const glob = $("pathrule-glob").value.trim();
  if (!glob) { toast("Enter a path or glob first.", "error", 3000); return; }
  const rules = ((settings && settings.path_rules) || []).slice();
  if (rules.some((x) => x.glob === glob && x.action === pathRuleAction)) {
    toast("That rule already exists.", "info", 2500); return;
  }
  rules.push({ glob, action: pathRuleAction });
  settings = await api().set_setting("path_rules", rules);
  $("pathrule-glob").value = "";
  renderPathRules();
  toast(`Rule added: ${pathRuleAction} ${glob}`, "info", 2800);
});

/* ---- scheduled & watched tasks (Settings) ----------------------------- */

let taskKind = "interval";
function applyTaskKindUI() {
  $("task-interval-row").hidden = taskKind !== "interval";
  $("task-daily-row").hidden = taskKind !== "daily";
  $("task-watch-row").hidden = taskKind !== "watch";
  document.querySelectorAll("#task-kind button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === taskKind);
    b.setAttribute("aria-checked", String(b.dataset.v === taskKind));
  });
}
document.querySelectorAll("#task-kind button").forEach((b) =>
  b.addEventListener("click", () => { taskKind = b.dataset.v; applyTaskKindUI(); }));

function renderTasksList(res) {
  const list = $("tasks-list");
  const tasks = (res && res.tasks) || [];
  list.innerHTML = "";
  for (const t of tasks) {
    const row = document.createElement("div");
    row.className = "provider-row";
    row.innerHTML =
      `<div class="provider-row-text"><span class="provider-name"></span>` +
      `<span class="provider-sub"></span></div>` +
      `<button class="switch task-en" role="switch"><span></span></button>` +
      `<button class="icon-btn-mini task-run" title="Run now">▶</button>` +
      `<button class="icon-btn-mini task-del" title="Delete">${CROSS_SVG}</button>`;
    row.querySelector(".provider-name").textContent = t.name || "(task)";
    const last = t.last_run ? " · last run " + new Date(t.last_run * 1000).toLocaleString() : "";
    row.querySelector(".provider-sub").textContent = (t.desc || "") + last;
    const en = row.querySelector(".task-en");
    en.setAttribute("aria-checked", String(t.enabled !== false));
    en.addEventListener("click", async () =>
      renderTasksList(await api().set_scheduled_enabled(t.id, en.getAttribute("aria-checked") !== "true")));
    row.querySelector(".task-run").addEventListener("click", async () => {
      const r = await api().run_scheduled_task_now(t.id);
      toast(r && r.error ? r.error : `Running “${t.name}” now…`, r && r.error ? "error" : "info", 3500);
    });
    row.querySelector(".task-del").addEventListener("click", async () =>
      renderTasksList(await api().delete_scheduled_task(t.id)));
    list.appendChild(row);
  }
  if (!list.children.length)
    list.innerHTML = '<div class="row-sub">No scheduled tasks yet.</div>';
}
async function populateTasks() {
  try { renderTasksList(await api().scheduled_tasks()); } catch (e) { /* ignore */ }
  if (!$("task-folder").value && settings.cwd) $("task-folder").value = settings.cwd;
  applyTaskKindUI();
}
$("task-folder-pick").addEventListener("click", async () => {
  const r = await api().pick_task_folder();
  if (r && r.path) $("task-folder").value = r.path;
});
$("task-save").addEventListener("click", async () => {
  const prompt = $("task-prompt").value.trim();
  const cwd = $("task-folder").value.trim();
  if (!prompt) { toast("Describe what the task should do.", "error", 3000); return; }
  if (!cwd) { toast("Choose a project folder.", "error", 3000); return; }
  let schedule = { kind: taskKind };
  if (taskKind === "interval") schedule.minutes = parseInt($("task-minutes").value, 10) || 60;
  else if (taskKind === "daily") schedule.at = $("task-at").value || "09:00";
  else schedule.path = cwd;
  const res = await api().save_scheduled_task({ name: $("task-name").value.trim(), prompt, cwd, schedule });
  if (res && res.error) { toast(res.error, "error", 5000); return; }
  $("task-name").value = ""; $("task-prompt").value = "";
  renderTasksList(res);
  $("task-editor").open = false;
  toast("Task saved.", "info", 2500);
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
  const tm = settings.thinking_mode || "medium";
  document.querySelectorAll("#seg-think button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === tm);
    b.setAttribute("aria-checked", b.dataset.v === tm);
  });
  applyThinkChip();
  $("opt-reasoning").setAttribute("aria-checked", !!settings.show_reasoning);
  $("opt-verify").setAttribute("aria-checked", settings.verify_edits !== false);
  $("opt-green").setAttribute("aria-checked", !!settings.auto_fix_tests);
  $("opt-neural").setAttribute("aria-checked", !!settings.codebase_memory_neural);
  $("opt-notify").setAttribute("aria-checked", !!settings.notifications);
  $("opt-reduce-fx").setAttribute("aria-checked", !!settings.reduce_effects);
  $("opt-browser-headless").setAttribute("aria-checked", !!settings.browser_headless);
  $("opt-browser-logins").setAttribute("aria-checked", !!settings.browser_keep_logins);
  $("gh-auto-pull").setAttribute("aria-checked", settings.github_auto_pull !== false);
  $("gh-auto-push").setAttribute("aria-checked", settings.github_auto_push !== false);
  if (document.activeElement !== $("gh-clone-root"))
    $("gh-clone-root").value = settings.github_clone_root || "";
  if (githubEnv && githubEnv.clone_root)
    $("gh-clone-root-note").textContent = "Cloned repositories go here: " + githubEnv.clone_root;
  applyPerfMode();
  $("cwd-label").textContent = settings.cwd || "No chat selected";
  $("cwd-label").title = settings.cwd || "";
  $("tb-cwd").textContent = shortPath(settings.cwd || "");
  $("tb-cwd").title = settings.cwd || "";
  const spd = settings.tts_speed || 1.0;
  $("voice-speed").value = spd;
  $("voice-speed-label").textContent = spd.toFixed(1) + "x";
  const vs = settings.voice_sensitivity || 1.0;
  $("voice-sensitivity").value = vs;
  $("voice-sensitivity-label").textContent = sensitivityLabel(vs);
  const sil = settings.voice_silence_ms || 750;
  $("voice-silence").value = sil;
  $("voice-silence-label").textContent = silenceLabel(sil);
  $("opt-earcons").setAttribute("aria-checked", settings.voice_earcons !== false);
  $("voice-ptt-key-label").textContent = pttKeyName(settings.voice_ptt_key || "Space");
  $("voice-reply-lang").value = settings.voice_reply_language || "en";
  const wakeOn = !!settings.voice_wake_enabled;
  $("opt-wake").setAttribute("aria-checked", wakeOn);
  $("wake-word-row").hidden = !wakeOn;
  $("voice-wake-word").value = settings.voice_wake_word || "hey assistant";
  applyModeChip();
  applyReadAloudChip();
  renderComposerOpts();
}
function shortPath(p) {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts.length > 2 ? "…\\" + parts.slice(-2).join("\\") : p;
}

let settingsTab = "general";
function showSettingsTab(name) {
  settingsTab = name;
  document.querySelectorAll("#settings-backdrop section[data-tab]").forEach((s) => {
    s.hidden = s.dataset.tab !== name;
  });
  document.querySelectorAll(".settings-tab-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === name);
  });
  // Re-apply toggle/segment state now the tab's controls are visible -- some
  // WebView2 builds don't "stick" styling applied while a subtree was hidden.
  syncSettingsUI();
  const sheet = document.querySelector("#settings-backdrop .sheet");
  if (sheet) sheet.scrollTop = 0;
}
document.querySelectorAll(".settings-tab-btn").forEach((b) => {
  b.addEventListener("click", () => showSettingsTab(b.dataset.tab));
});

async function openSettings() {
  // Re-sync the toggles/segments to the CURRENT settings every time the sheet
  // opens. It's cheap and idempotent, and it makes the sheet correct no matter
  // what happened at boot -- the boot-time sync could silently not "stick" on
  // some WebView2 builds (controls styled while their subtree is still hidden),
  // which left the first open showing nothing selected until a change re-ran it.
  syncSettingsUI();
  $("settings-backdrop").hidden = false;
  showSettingsTab(settingsTab);
  populateVoiceSelect();
  populateSttSelect();
  populateBackups();
  populateModelPicker();
  populateBrowserModelSelect();
  populateMcp();
  populateCommands();
  renderPathRules();
  refreshGithubEnv();
  refreshGithubRepo();
  populateTasks();
  try {
    const u = await api().usage();
    $("session-usage").textContent =
      `${fmtTokens(u.completion_tokens)} output · ${fmtTokens(u.prompt_tokens)} sent · context ~${fmtTokens(u.context)} · $0.00`;
  } catch (e) { /* usage line is non-critical */ }
}
$("settings-btn").addEventListener("click", openSettings);

/* Dedicated Browser Agent model: every configured provider's models, plus
   "Same as chat". Options carry [provider, model] as JSON in their value. */
async function populateBrowserModelSelect() {
  const sel = $("opt-browser-model");
  let res;
  try { res = await api().providers(); } catch (e) { return; }
  const cur = JSON.stringify([settings.browser_provider || "", settings.browser_model || ""]);
  sel.innerHTML = "";
  const same = document.createElement("option");
  same.value = JSON.stringify(["", ""]);
  same.textContent = "Same as chat";
  sel.appendChild(same);
  for (const p of (res.providers || [])) {
    for (const m of (p.models || [])) {
      const o = document.createElement("option");
      o.value = JSON.stringify([p.name, m]);
      o.textContent = `${m} — ${p.name}`;
      sel.appendChild(o);
    }
  }
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : JSON.stringify(["", ""]);
}

$("opt-browser-model").addEventListener("change", async (e) => {
  let prov = "", model = "";
  try { [prov, model] = JSON.parse(e.target.value); } catch (err) { /* same as chat */ }
  const res = await api().set_browser_model(prov, model);
  if (res && res.error) { toast(res.error, "error", 4000); return; }
  settings.browser_provider = res.browser_provider;
  settings.browser_model = res.browser_model;
  toast(model ? `Browser agent will use ${model}.` : "Browser agent uses the chat's model.",
        "info", 3000);
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
document.querySelectorAll("#seg-think button").forEach((b) =>
  b.addEventListener("click", async () => {
    settings = await api().set_setting("thinking_mode", b.dataset.v);
    syncSettingsUI();
  }));

function bindSwitch(id, key) {
  $(id).addEventListener("click", async () => {
    const now = $(id).getAttribute("aria-checked") === "true";
    settings = await api().set_setting(key, !now);
    syncSettingsUI();
  });
}
bindSwitch("opt-reasoning", "show_reasoning");
bindSwitch("opt-verify", "verify_edits");
bindSwitch("opt-green", "auto_fix_tests");
bindSwitch("opt-neural", "codebase_memory_neural");

/* ---- composer message-options popover (per-message behavior) ---------- */
function renderComposerOpts() {
  const green = !!settings.auto_fix_tests;
  const verify = settings.verify_edits === true;
  const attempts = settings.parallel_attempts || 1;
  $("opt-green2").setAttribute("aria-checked", String(green));
  $("opt-verify2").setAttribute("aria-checked", String(verify));
  document.querySelectorAll("#seg-attempts button").forEach((b) => {
    const on = String(attempts) === b.dataset.v;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  });
  const active = green || verify || attempts > 1;
  $("opts-dot").hidden = !active;
  $("opts-btn").classList.toggle("has-active", active);
}
async function setOpt(key, val) {
  settings = await api().set_setting(key, val);
  renderComposerOpts();
  syncSettingsUI();   // keep the Settings-panel copies in sync
}
function closeOptsMenu() {
  $("opts-menu").hidden = true;
  $("opts-btn").setAttribute("aria-expanded", "false");
}
$("opts-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const show = $("opts-menu").hidden;
  $("opts-menu").hidden = !show;
  $("opts-btn").setAttribute("aria-expanded", String(show));
  if (show) renderComposerOpts();
});
$("opt-green2").addEventListener("click", () =>
  setOpt("auto_fix_tests", $("opt-green2").getAttribute("aria-checked") !== "true"));
$("opt-verify2").addEventListener("click", () =>
  setOpt("verify_edits", $("opt-verify2").getAttribute("aria-checked") !== "true"));
document.querySelectorAll("#seg-attempts button").forEach((b) =>
  b.addEventListener("click", () => setOpt("parallel_attempts", parseInt(b.dataset.v, 10))));
document.addEventListener("click", (e) => {
  if ($("opts-menu").hidden) return;
  if (!$("opts-menu").contains(e.target) && !$("opts-btn").contains(e.target)) closeOptsMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("opts-menu").hidden) closeOptsMenu();
});
bindSwitch("opt-notify", "notifications");
bindSwitch("opt-reduce-fx", "reduce_effects");
bindSwitch("opt-browser-headless", "browser_headless");
bindSwitch("opt-browser-logins", "browser_keep_logins");
bindSwitch("gh-auto-pull", "github_auto_pull");
bindSwitch("gh-auto-push", "github_auto_push");

/* ---- GitHub integration ---------------------------------------------- */

let githubEnv = {};
let ghNewVisibility = "private";
let ghRemoteUrl = "";

async function refreshGithubEnv() {
  try { githubEnv = await api().github_env(); } catch (e) { githubEnv = { available: false }; }
  renderGithubSettings();
}

function renderGithubSettings() {
  const e = githubEnv || {};
  const connected = !!e.token_present;
  $("gh-disconnected").hidden = connected;
  $("gh-connected").hidden = !connected;
  $("gh-login").textContent = e.login ? ("@" + e.login) : "connected";
  const note = $("gh-storage-note");
  if (connected && e.secure === false) {
    note.hidden = false;
    note.textContent = "No OS keyring was found, so your token is kept in an encrypted file " +
      "instead — weaker, since someone with your user account could read it. Install the " +
      "'keyring' package for OS-backed storage.";
  } else { note.hidden = true; }
  $("gh-unavailable").hidden = e.available !== false;
  $("newchat-gh-hint").hidden = connected;
}

function ghStatusText(st) {
  const bits = ["branch " + (st.branch || "?")];
  if (st.ahead) bits.push("↑" + st.ahead + " to push");
  if (st.behind) bits.push("↓" + st.behind + " to pull");
  if (st.dirty) bits.push("uncommitted changes");
  if (!st.ahead && !st.behind && !st.dirty) bits.push("up to date");
  return bits.join(" · ");
}

async function refreshGithubRepo() {
  let st = { connected: false };
  try { st = await api().github_status(); } catch (e) { /* ignore */ }
  const connected = !!st.connected;
  const rc = $("gh-repo-connected"), rd = $("gh-repo-disconnected");
  if (rc) rc.hidden = !connected;
  if (rd) rd.hidden = connected;
  if (connected) {
    const name = (st.owner && st.repo) ? `${st.owner}/${st.repo}` : (st.remote_url || "repository");
    if ($("gh-repo-name")) $("gh-repo-name").textContent = name;
    if ($("gh-repo-status")) $("gh-repo-status").textContent = ghStatusText(st);
    ghRemoteUrl = (st.owner && st.repo) ? `https://${st.host || "github.com"}/${st.owner}/${st.repo}` : "";
  }
  renderGithubFoot(st);
}

function renderGithubFoot(st) {
  const foot = $("gh-foot");
  if (!foot) return;
  if (!st || !st.connected) { foot.hidden = true; return; }
  foot.hidden = false;
  $("gh-foot-name").textContent = (st.owner && st.repo) ? `${st.owner}/${st.repo}` : "repo";
  const c = [];
  if (st.ahead) c.push("↑" + st.ahead);
  if (st.behind) c.push("↓" + st.behind);
  if (st.dirty) c.push("●");
  $("gh-foot-counts").textContent = c.join(" ");
}

async function ghAction(btn, fn, restore) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; if (restore !== false) btn.textContent = "…"; }
  try {
    const res = await fn();
    if (res && res.error) { toast(res.error, "error", 6000); return null; }
    return res;
  } finally {
    if (btn) { btn.disabled = false; if (restore !== false) btn.textContent = label; }
  }
}

$("gh-token-save").addEventListener("click", async () => {
  const t = $("gh-token").value.trim();
  if (!t) { toast("Paste a token first.", "error", 3000); return; }
  const res = await ghAction($("gh-token-save"), () => api().github_set_token(t));
  if (!res) return;
  $("gh-token").value = "";
  githubEnv = res; renderGithubSettings();
  toast("GitHub connected" + (res.login ? ` as @${res.login}` : "") + ".", "info", 3500);
});
$("gh-token-forget").addEventListener("click", async () => {
  githubEnv = await api().github_forget_token();
  renderGithubSettings();
  toast("GitHub token removed.", "info", 2500);
});
$("gh-token-help").addEventListener("click", (e) => {
  e.preventDefault();
  api().open_external("https://github.com/settings/tokens?type=beta");
});
$("gh-clone-root").addEventListener("change", async () => {
  settings = await api().set_setting("github_clone_root", $("gh-clone-root").value.trim());
  refreshGithubEnv();
});

async function ghClone(url, btn) {
  if (!url) { toast("Enter a repository.", "error", 3000); return; }
  const res = await ghAction(btn, () => api().github_clone(url));
  if (!res) return;
  $("settings-backdrop").hidden = true;
  $("newchat-backdrop").hidden = true;
  applySession(res);
  input.focus();
}
$("gh-clone-go").addEventListener("click", () => ghClone($("gh-clone-url").value.trim(), $("gh-clone-go")));
$("newchat-gh-go").addEventListener("click", () => ghClone($("newchat-gh-url").value.trim(), $("newchat-gh-go")));
$("newchat-gh-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("newchat-gh-go").click(); }
});

$("gh-clone-pick").addEventListener("click", async () => {
  const list = $("gh-repo-list");
  const res = await ghAction($("gh-clone-pick"), () => api().github_list_repos(), false);
  if (!res) return;
  list.hidden = false;
  list.innerHTML = "";
  for (const r of res.repos || []) {
    const row = document.createElement("button");
    row.className = "gh-repo-row";
    row.innerHTML = `<span class="gh-repo-name mono"></span>` +
      `<span class="gh-repo-tag">${r.private ? "private" : "public"}${r.empty ? " · empty" : ""}</span>`;
    row.querySelector(".gh-repo-name").textContent = r.full_name;
    row.addEventListener("click", () => ghClone(r.full_name, row));
    list.appendChild(row);
  }
  if (!list.children.length) list.innerHTML = '<div class="row-sub">No repositories found for this token.</div>';
});

$("gh-connect-go").addEventListener("click", async () => {
  const url = $("gh-connect-url").value.trim();
  if (!url) { toast("Enter the repo to connect to.", "error", 3000); return; }
  const res = await ghAction($("gh-connect-go"), () => api().github_connect(url));
  if (!res) return;
  $("gh-connect-url").value = "";
  refreshGithubRepo();
});

document.querySelectorAll("#gh-new-vis button").forEach((b) =>
  b.addEventListener("click", () => {
    ghNewVisibility = b.dataset.v;
    document.querySelectorAll("#gh-new-vis button").forEach((x) => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-checked", x === b);
    });
  }));

$("gh-create-go").addEventListener("click", async () => {
  const name = $("gh-new-name").value.trim();
  if (!name) { toast("Name the new repository.", "error", 3000); return; }
  const res = await ghAction($("gh-create-go"),
    () => api().github_create_and_connect(name, ghNewVisibility === "private"));
  if (!res) return;
  $("gh-new-name").value = "";
  refreshGithubRepo();
});

$("gh-pull").addEventListener("click", async () => {
  if (await ghAction($("gh-pull"), () => api().github_pull())) refreshGithubRepo();
});
$("gh-sync").addEventListener("click", async () => {
  if (await ghAction($("gh-sync"), () => api().github_sync())) refreshGithubRepo();
});
$("gh-repo-disconnect").addEventListener("click", async () => {
  if (!confirm("Stop syncing this folder with GitHub? Your files are kept.")) return;
  if (await ghAction($("gh-repo-disconnect"), () => api().github_disconnect())) refreshGithubRepo();
});
$("gh-open-remote").addEventListener("click", () => { if (ghRemoteUrl) api().open_external(ghRemoteUrl); });

$("gh-phone-setup").addEventListener("click", async () => {
  const res = await ghAction($("gh-phone-setup"), () => api().github_setup_phone_access(), false);
  if (!res) return;
  toast(`Added ${res.path}. Now: Sync it up, add a ZAI_API_KEY secret (opening that page), ` +
        `then comment "/agent …" on any issue from your phone.`, "info", 9000);
  if (res.secrets_url) api().open_external(res.secrets_url);
  refreshGithubRepo();
});

// --- Get the phone app (QR + URL) ---
async function openPhoneApp() {
  const box = $("phoneapp-qr"), urlEl = $("phoneapp-url"), err = $("phoneapp-error");
  box.innerHTML = ""; err.hidden = true;
  urlEl.textContent = "loading…"; urlEl.removeAttribute("href");
  $("phoneapp-backdrop").hidden = false;
  let res;
  try { res = await api().get_phone_app(); }
  catch (e) { res = { error: "Couldn't reach the app." }; }
  urlEl.textContent = res.url || "";
  if (res.url) { urlEl.href = res.url; $("phoneapp-url-input").value = res.url; }
  // The SVG is generated locally by our own code (segno), not user/model input.
  if (res.svg) box.innerHTML = res.svg;
  else { err.textContent = res.error || "Couldn't build the QR code."; err.hidden = false; }
}
$("gh-get-app").addEventListener("click", openPhoneApp);
$("phoneapp-close").addEventListener("click", () => { $("phoneapp-backdrop").hidden = true; });
$("phoneapp-backdrop").addEventListener("click", (e) => {
  if (e.target === $("phoneapp-backdrop")) $("phoneapp-backdrop").hidden = true;
});
$("phoneapp-open").addEventListener("click", () => {
  const u = $("phoneapp-url").textContent.trim(); if (u.startsWith("http")) api().open_external(u);
});
$("phoneapp-copy").addEventListener("click", async () => {
  const u = $("phoneapp-url").textContent.trim(); if (!u.startsWith("http")) return;
  try { await copyText(u); toast("Link copied.", "info", 2000); }
  catch { toast("Couldn't copy to clipboard.", "error", 3000); }
});
$("phoneapp-url-save").addEventListener("click", async () => {
  const v = $("phoneapp-url-input").value.trim();
  try { await api().set_setting("phone_app_url", v); } catch (e) {}
  openPhoneApp();
});

$("gh-pr-load").addEventListener("click", async () => {
  const res = await ghAction($("gh-pr-load"), () => api().github_open_pulls(), false);
  if (!res) return;
  const list = $("gh-pr-list");
  list.hidden = false;
  list.innerHTML = "";
  for (const pr of res.pulls || []) {
    const row = document.createElement("button");
    row.className = "gh-repo-row";
    row.innerHTML = `<span class="gh-repo-name">#${pr.number} ${esc(pr.title)}</span>` +
      `<span class="gh-repo-tag">${esc(pr.author)}${pr.draft ? " · draft" : ""}</span>`;
    row.addEventListener("click", () => { $("gh-pr-number").value = pr.number; });
    list.appendChild(row);
  }
  if (!list.children.length) list.innerHTML = '<div class="row-sub">No open pull requests.</div>';
});
async function ghPrAction(btn, fn) {
  const n = parseInt($("gh-pr-number").value, 10);
  if (!n) { toast("Enter a PR number (or load and pick one).", "error", 3000); return; }
  const res = await ghAction(btn, () => fn(n));
  if (res && res.ok) { $("settings-backdrop").hidden = true; }
}
$("gh-pr-review").addEventListener("click", () =>
  ghPrAction($("gh-pr-review"), (n) => api().github_review_pr(n)));
$("gh-pr-address").addEventListener("click", () =>
  ghPrAction($("gh-pr-address"), (n) => api().github_address_pr(n)));

$("gh-foot-pull").addEventListener("click", async () => {
  if (await ghAction($("gh-foot-pull"), () => api().github_pull())) refreshGithubRepo();
});
$("gh-foot-sync").addEventListener("click", async () => {
  if (await ghAction($("gh-foot-sync"), () => api().github_sync())) refreshGithubRepo();
});

$("browser-clear-data").addEventListener("click", async () => {
  if (!confirm("Log the agent browser out of everything and delete its saved data?")) return;
  const res = await api().clear_browser_profile();
  if (res && res.error) toast(res.error, "error", 5000);
  else toast("Saved browser data cleared.", "info", 3000);
});

function applyPerfMode() {
  document.body.classList.toggle("perf-mode", !!(settings && settings.reduce_effects));
}

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
  // A custom background overrides the CSS default via inline style; an empty
  // uri clears that override so the default (loaded from disk in style.css)
  // shows through -- never blanks the screen.
  if (uri) {
    $("bg").style.backgroundImage = `url("${uri}")`;
    $("bg-preview").src = uri;
  } else {
    $("bg").style.backgroundImage = "";
    $("bg-preview").src = "bg-default.jpg";
  }
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
  $("learn-project").hidden = !(res.needs_notes && !hasItems);
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
  refreshGithubRepo();   // update the sync chip for this chat's folder
}

$("learn-project").addEventListener("click", async () => {
  $("learn-project").hidden = true;
  const r = await api().generate_project_notes();
  if (r && r.error) toast(r.error, "error", 4000);
});

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
  const btn = $("key-save");
  const key = $("key-input").value.trim();
  if (!key || btn.disabled) return;
  // Persisting the key can take a moment (or a locked-down machine can stall
  // the env-var write), so show progress and NEVER leave the button looking
  // dead. A bridge failure surfaces as an error instead of silently nothing.
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Starting…";
  let res = null;
  try {
    res = await api().save_api_key(key);
  } catch (e) {
    res = null;
  }
  btn.disabled = false;
  btn.textContent = label;
  if (res && res.ok) {
    $("key-backdrop").hidden = true;
    toast(res.persisted ? "API key saved to your user environment" :
      "Key active for this session", "info", 4000);
    if (res.sessions) sessions = res.sessions;
    if (res.session) applySession(res.session);
    else showNoSession();
  } else {
    toast((res && res.error) ||
      "Couldn't start — the key is set for now, try pressing Start again.", "error", 6000);
  }
});
$("key-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("key-save").click();
});
$("zai-link").addEventListener("click", (e) => {
  e.preventDefault();
  api().open_external("https://z.ai");
});

/* ------------------------------------------------ command palette (Ctrl+K) */

let palItems = [];   // filtered, currently shown
let palSel = 0;

function paletteAllItems() {
  const items = [];
  items.push({ label: "New chat…", hint: "pick a project folder", run: () => newChat() });
  items.push({ label: "Open whiteboard", hint: "scratch project", run: () => openWhiteboard() });
  items.push({ label: busy ? "Stop the agent" : "Toggle plan mode",
               hint: busy ? "interrupt the running turn" : "explore read-only, then propose a plan",
               run: () => (busy ? $("stop-btn") : $("plan-toggle")).click() });
  items.push({ label: "Compact conversation", hint: "summarize older history", run: () => $("compact-btn").click() });
  items.push({ label: "Export chat to Markdown…", hint: "save this conversation", run: () => exportChat() });
  items.push({ label: "Settings", hint: "", run: () => $("settings-btn").click() });
  items.push({ label: "Toggle sidebar", hint: "chat history", run: () => $("sidebar-toggle").click() });
  for (const s of sessions || []) {
    if (s.id === activeSessionId) continue;
    items.push({ label: "Open: " + (s.title || s.id), hint: s.cwd || "",
                 run: () => openSession(s.id) });
  }
  for (const e of modelEntries(providersCache || { providers: [] })) {
    items.push({ label: "Model: " + e.model, hint: "via " + e.provider,
                 run: () => selectModel(e) });
  }
  return items;
}

function paletteScore(q, label) {
  const l = label.toLowerCase();
  if (!q) return 0;
  if (l.startsWith(q)) return 1;
  if (l.includes(q)) return 2 + l.indexOf(q);
  let i = 0;
  for (const ch of l) if (i < q.length && ch === q[i]) i++;
  return i === q.length ? 100 + l.length : null;
}

function renderPalette() {
  const list = $("palette-list");
  list.innerHTML = "";
  palItems.forEach((it, i) => {
    const row = document.createElement("button");
    row.className = "pal-opt" + (i === palSel ? " sel" : "");
    row.setAttribute("role", "option");
    row.innerHTML = `<span class="pal-label"></span><span class="pal-hint"></span>`;
    row.querySelector(".pal-label").textContent = it.label;
    row.querySelector(".pal-hint").textContent = it.hint || "";
    row.addEventListener("mousedown", (e) => { e.preventDefault(); runPalette(i); });
    list.appendChild(row);
  });
  if (!palItems.length) {
    list.innerHTML = '<div class="pal-empty">No matches.</div>';
  }
}

function filterPalette() {
  const q = $("palette-input").value.trim().toLowerCase();
  palItems = paletteAllItems()
    .map((it) => ({ it, s: paletteScore(q, it.label + " " + (it.hint || "")) }))
    .filter((x) => x.s !== null)
    .sort((a, b) => a.s - b.s)
    .map((x) => x.it)
    .slice(0, 12);
  palSel = 0;
  renderPalette();
}

async function exportChat() {
  try {
    const res = await api().export_chat();
    if (res && res.error) toast(res.error, "error", 5000);
    else if (res && res.ok) toast("Saved to " + res.path, "info", 5000);
  } catch (e) { toast("Bridge error: " + e, "error", 5000); }
}

function openPalette() {
  $("palette-backdrop").hidden = false;
  $("palette-input").value = "";
  filterPalette();
  $("palette-input").focus();
}
function closePalette() { $("palette-backdrop").hidden = true; }
function runPalette(i) {
  const it = palItems[i];
  closePalette();
  if (it) it.run();
}

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
    e.preventDefault();
    $("palette-backdrop").hidden ? openPalette() : closePalette();
  }
});
$("palette-input").addEventListener("input", filterPalette);
$("palette-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); palSel = Math.min(palSel + 1, palItems.length - 1); renderPalette(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palSel = Math.max(palSel - 1, 0); renderPalette(); }
  else if (e.key === "Enter") { e.preventDefault(); runPalette(palSel); }
  else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
});
$("palette-backdrop").addEventListener("click", (e) => {
  if (e.target === $("palette-backdrop")) closePalette();
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
  applyPerfMode();  // before first paint of the session, so no heavy-then-flat flash
  sessions = b.sessions || [];
  if (b.contextLimit) contextLimit = b.contextLimit;
  try { const c = await api().commands(); slashCommands = (c && c.commands) || []; } catch (e) { /* ignore */ }
  setBackground(b.background);
  $("about-version").textContent = "v" + b.version;
  syncSettingsUI();
  refreshWake();  // start listening for the wake word if it's enabled

  if (b.needsKey) {
    showNoSession();
    $("key-backdrop").hidden = false;
  } else if (b.session) {
    applySession(b.session);
    input.focus();
  } else {
    showNoSession();
  }
  // OS-level notifications (permission prompts, finished turns) only fire
  // while the user is away in another app -- keep Python's picture of
  // window focus current, starting from the real state right now.
  const reportFocus = (f) => { try { api().set_window_focus(f); } catch (e) { /* ignore */ } };
  window.addEventListener("focus", () => reportFocus(true));
  window.addEventListener("blur", () => reportFocus(false));
  reportFocus(document.hasFocus());
  try { api().log && api().log("boot:done"); } catch (e) { /* ignore */ }
}

/* ============================================================ voice mode ==
   Speech-to-speech: talk to a delegator agent hands-free. It hands real work
   to background workers (which act on the project) and keeps listening, so you
   can queue work by voice without touching the keyboard. When a worker finishes
   it tells you out loud.

   Endpointing (when did you stop talking) uses an ADAPTIVE energy VAD: it
   calibrates to the room's noise floor at start and keeps tracking it, so it
   works in a quiet room or a noisy one without hand-tuning. The recorder starts
   on the very first loud frame (so the first word isn't clipped) and the clip
   is kept only once enough real speech is seen. Barge-in (talking over its
   reply) cancels the reply on the backend AND stops playback, so it actually
   yields to you. A push-to-talk fallback (hold Space / the button) sidesteps
   the whole VAD when a mic or room makes hands-free unreliable. */

const voice = {
  active: false, ptt: false, stream: null, ctx: null, analyser: null, data: null,
  timer: 0, rec: null, chunks: [], recording: false, confirmed: false, discard: false,
  speaking: false, thinking: false, transcribing: false, turnComplete: true,
  voiceFrames: 0, voicedMs: 0, silenceMs: 0, recMs: 0, peak: 0,
  noiseFloor: 0.01, sens: 1.0, announceQ: [], workers: {}, sendTries: 0,
  perm: null, permQ: [], muted: false, pttKey: "Space", endpointMs: 750,
  waveRaf: 0, lastReply: [], replyChunks: [], curTurnEl: null, replyEl: null,
  gated: false, gateOpen: true,  // wake-gated turns: each request needs the wake word
  thinkTimer: 0, acking: false, ackAudio: null,
};
// True when we're in a session but "soft-muted" -- listening only for the wake
// word before we'll accept another request (vs the open state where we take
// whatever you say). Kept as a helper so every check reads the same.
const wakeGating = () => voice.gated && !voice.gateOpen && !voice.perm;
const wakePhrase = () => (settings && settings.voice_wake_word) || "hey assistant";
const V_FRAME_MS = 50;         // VAD poll cadence
const V_CAL_MS = 500;          // ambient sampling at start to seed the noise floor
const V_START_MULT = 3.0;      // start threshold  = noiseFloor * this
const V_STOP_MULT = 1.8;       // stop threshold   = noiseFloor * this (hysteresis)
const V_BARGE_MULT = 5.0;      // to cut in over our own TTS, clear noiseFloor * this
const V_ABS_MIN = 0.006;       // threshold floor, so a silent room still needs SOME sound
const V_ONSET_MS = 100;        // voiced time before an utterance is "confirmed" (kept)
const V_SILENCE_MS = 750;      // trailing silence that ends a confirmed utterance
const V_BLIP_MS = 300;         // unconfirmed clip abandoned after this much silence
const V_MAX_UNCONFIRMED_MS = 1500;  // give up on a clip that never becomes real speech
const V_MAX_UTTERANCE_MS = 15000;   // hard cap: never record forever, always send what we have
const V_SILENCE_FRAC = 0.15;   // silence = energy below this fraction of how loud you actually got
const V_NF_ADAPT = 0.02;       // how fast the idle noise-floor estimate tracks the room

const startThresh = () => Math.max(V_ABS_MIN, voice.noiseFloor * V_START_MULT) / voice.sens;
const stopThresh = () => Math.max(V_ABS_MIN * 0.7, voice.noiseFloor * V_STOP_MULT) / voice.sens;
const bargeThresh = () => Math.max(V_ABS_MIN * 2, voice.noiseFloor * V_BARGE_MULT) / voice.sens;

function setVoiceStatus(text) { const el = $("voice-status"); if (el) el.textContent = text; }
function setVoiceOrb(state) {
  const orb = $("voice-orb");
  if (orb) orb.className = "voice-orb voice-orb-" + state;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startVoice(viaWake = false) {
  if (voice.active) return;
  disarmWake();  // release the wake listener's mic before opening our own
  let res;
  try { res = await api().voice_mode(true); } catch (e) { res = { error: String(e) }; }
  if (!res || res.error) { toast(res && res.error ? res.error : "Couldn't start voice mode.", "error", 5000); return; }
  voice.sid = res.voice_sid || "";
  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    toast("Microphone access is needed for voice mode.", "error", 6000);
    try { await api().voice_mode(false); } catch (_) { /* ignore */ }
    return;
  }
  voice.active = true;
  voice.speaking = voice.thinking = voice.recording = voice.transcribing = false;
  voice.announceQ = [];
  voice.workers = {};
  voice.perm = null;
  voice.permQ = [];
  voice.muted = false;
  voice.lastReply = [];
  voice.replyChunks = [];
  voice.curTurnEl = null;
  voice.replyEl = null;
  $("voice-perm").hidden = true;
  $("voice-replay").hidden = true;
  $("voice-mute").setAttribute("aria-pressed", "false");
  $("voice-mute").classList.remove("active");
  $("voice-caption").innerHTML = "";
  voice.sens = (settings && settings.voice_sensitivity) || 1.0;
  voice.pttKey = (settings && settings.voice_ptt_key) || "Space";
  voice.endpointMs = (settings && settings.voice_silence_ms) || 750;
  // Wake-gated turns default by HOW you started: opening by wake word implies
  // hands-free-in-a-room, so gating is ON (say the word before each request);
  // opening by clicking the button implies you're at the machine, so it's OFF
  // (continuous). Toggle it live from the voice screen either way.
  voice.gated = !!viaWake;
  voice.gateOpen = true;
  applyGateToggleUI();
  renderVoiceWorkers();
  $("voice-caption").textContent = "";
  $("voice-overlay").hidden = false;
  $("voice-chip").setAttribute("aria-pressed", "true");
  $("voice-chip").classList.add("active");
  // Web Audio analyser for the energy VAD.
  const Ctx = window.AudioContext || window.webkitAudioContext;
  voice.ctx = new Ctx();
  const src = voice.ctx.createMediaStreamSource(voice.stream);
  voice.analyser = voice.ctx.createAnalyser();
  voice.analyser.fftSize = 1024;
  voice.data = new Uint8Array(voice.analyser.fftSize);
  src.connect(voice.analyser);
  onTtsIdle = onVoiceTtsIdle;
  startWaveform();
  // Calibrate to the room: sample ambient energy briefly and seed the noise
  // floor from it, so the very first utterance already has sane thresholds.
  if (!voice.ptt) { setVoiceStatus("Calibrating to the room…"); setVoiceOrb("thinking"); }
  await calibrateNoiseFloor();
  if (!voice.active) return;  // closed during calibration
  if (voice.ptt) {
    setVoiceStatus("Push-to-talk — hold Space or the button");
    setVoiceOrb("idle");
  } else {
    setVoiceStatus("Listening… just start talking");
    setVoiceOrb("listening");
  }
  voice.timer = setInterval(vadTick, V_FRAME_MS);
}

async function calibrateNoiseFloor() {
  const samples = [];
  const n = Math.max(4, Math.round(V_CAL_MS / V_FRAME_MS));
  for (let i = 0; i < n && voice.active; i++) {
    samples.push(micEnergy());
    await sleep(V_FRAME_MS);
  }
  if (!samples.length) return;
  samples.sort((a, b) => a - b);
  const median = samples[Math.floor(samples.length / 2)];
  // A little above the median ambient reading, clamped to a sane band.
  voice.noiseFloor = Math.min(0.05, Math.max(0.004, median * 1.3 || 0.01));
}

function stopVoice() {
  if (!voice.active && !voice.stream) return;
  voice.active = false;
  onTtsIdle = null;
  resetTtsPlayback();
  stopThinkCue();
  stopAck();
  voice.speaking = voice.thinking = false;
  if (voice.waveRaf) { cancelAnimationFrame(voice.waveRaf); voice.waveRaf = 0; }
  if (voice.timer) { clearInterval(voice.timer); voice.timer = 0; }
  try { if (voice.rec && voice.recording) voice.rec.stop(); } catch (e) { /* ignore */ }
  voice.recording = false;
  if (voice.stream) { voice.stream.getTracks().forEach((t) => t.stop()); voice.stream = null; }
  if (voice.ctx) { try { voice.ctx.close(); } catch (e) { /* ignore */ } voice.ctx = null; }
  $("voice-overlay").hidden = true;
  $("voice-chip").setAttribute("aria-pressed", "false");
  $("voice-chip").classList.remove("active");
  try { api().voice_mode(false); } catch (e) { /* ignore */ }
  // Re-arm the wake listener AFTER the voice mic has fully released. Grabbing
  // the device again in the same tick can fail (still busy), and that failure
  // used to be swallowed -- leaving the wake word silently not listening after
  // a session, which looks like the setting turned itself off.
  setTimeout(refreshWake, 300);
}

function micEnergy() {
  voice.analyser.getByteTimeDomainData(voice.data);
  let sum = 0;
  for (let i = 0; i < voice.data.length; i++) {
    const v = (voice.data[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / voice.data.length);
}

// Adaptive endpointing state machine. Skipped in push-to-talk mode (there the
// button/Space drives start/stop directly).
function vadTick() {
  if (!voice.active || voice.ptt || voice.muted) return;
  const e = micEnergy();
  const orb = $("voice-orb");
  if (orb) orb.style.setProperty("--amp", Math.min(1, e / (voice.noiseFloor * 12 + 0.04)).toFixed(3));
  if (voice.recording) { recordingTick(e); return; }
  if (voice.transcribing) return;  // busy sending the last utterance
  // Idle: while it's speaking, only a much louder input (barge-in) starts a
  // recording, so its own voice out of the speakers doesn't trip us. Otherwise
  // the recorder starts on the first loud frame -- we capture the onset and
  // decide later whether to keep it. When soft-muted (wake-gating) we still
  // record bursts, but only to check them for the wake word -- so we listen
  // through our own speech too (echo cancellation + the phrase check keep the
  // TTS from self-triggering).
  const gate = ((voice.speaking || voice.acking) && !wakeGating()) ? bargeThresh() : startThresh();
  if (e > gate) {
    startUtterance();
  } else {
    // Track the room's noise floor from quiet frames (slow, so speech pauses
    // don't drag it up).
    voice.noiseFloor = (1 - V_NF_ADAPT) * voice.noiseFloor + V_NF_ADAPT * e;
    voice.noiseFloor = Math.min(0.06, Math.max(0.003, voice.noiseFloor));
  }
}

function recordingTick(e) {
  voice.recMs += V_FRAME_MS;
  // Track how loud this utterance actually got (decaying, so an early loud
  // burst doesn't make everything after look like silence).
  voice.peak = Math.max(e, (voice.peak || 0) * 0.995);
  // Silence = energy has dropped below BOTH the calibrated floor and a fraction
  // of your own speaking level. The relative part is the fix for "it never
  // responds": if the room is a bit noisier than calibration assumed, ambient
  // can sit above the fixed threshold forever -- but it's still far below how
  // loud you were talking, so the relative test still detects that you stopped.
  const silenceLevel = Math.max(stopThresh(), voice.peak * V_SILENCE_FRAC);
  if (e > silenceLevel) { voice.voicedMs += V_FRAME_MS; voice.silenceMs = 0; }
  else { voice.silenceMs += V_FRAME_MS; }
  // Enough real voiced audio -> this is a genuine utterance. If it arrived
  // while the agent was talking or thinking, that's a barge-in: cut its reply.
  if (!voice.confirmed && voice.voicedMs >= V_ONSET_MS) {
    voice.confirmed = true;
    stopAck();  // you're talking now -- cut the "Yes?" short
    if (wakeGating()) {
      // Soft-muted: don't cut the reply or claim we're taking a request -- we
      // don't know yet whether this burst is the wake word. Decide after STT.
    } else {
      if (voice.speaking || voice.thinking) interruptReply();
      setVoiceOrb("hearing");
      setVoiceStatus("Listening…");
    }
  }
  if (voice.confirmed) {
    // End on trailing silence, OR a hard cap so a stuck endpoint can never
    // leave it recording forever with no response.
    if (voice.silenceMs >= voice.endpointMs || voice.recMs >= V_MAX_UTTERANCE_MS) endUtterance();
  } else if (voice.silenceMs >= V_BLIP_MS || voice.recMs >= V_MAX_UNCONFIRMED_MS) {
    endUtterance();  // never became real speech -- drop it
  }
}

function startUtterance() {
  if (voice.recording || !voice.stream) return;
  let mime = "";
  if (window.MediaRecorder) {
    if (MediaRecorder.isTypeSupported("audio/webm")) mime = "audio/webm";
    else if (MediaRecorder.isTypeSupported("audio/ogg")) mime = "audio/ogg";
  }
  try {
    voice.rec = mime ? new MediaRecorder(voice.stream, { mimeType: mime })
                     : new MediaRecorder(voice.stream);
  } catch (e) { return; }
  voice.chunks = [];
  voice.recording = true;
  voice.confirmed = false;
  voice.voicedMs = 0;
  voice.silenceMs = 0;
  voice.recMs = 0;
  voice.peak = 0;
  voice.rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) voice.chunks.push(ev.data); };
  voice.rec.onstop = finishUtterance;
  // Small timeslices so short utterances still flush at least one data chunk.
  voice.rec.start(200);
  if (!voice.speaking) setVoiceOrb("hearing");
}

function endUtterance() {
  if (!voice.recording) return;
  voice.recording = false;
  // Keep only confirmed clips with enough voiced audio; blips are dropped.
  voice.discard = !voice.confirmed;
  if (!voice.discard && !wakeGating()) earcon("endpoint");  // "heard you" (not while soft-muted)
  try { voice.rec.stop(); } catch (e) { /* onstop still fires */ }
}

async function finishUtterance() {
  voice.voiceFrames = 0;
  if (!voice.active) return;  // session closed while recording
  if (voice.discard || !voice.chunks.length) {
    idleOrListen();
    return;
  }
  voice.transcribing = true;
  setVoiceOrb("thinking");
  setVoiceStatus("Transcribing…");
  const blob = new Blob(voice.chunks, { type: voice.chunks[0].type || "audio/webm" });
  voice.chunks = [];
  const dataUrl = await new Promise((resolve) => {
    const r = new FileReader();
    r.onloadend = () => resolve(r.result);
    r.onerror = () => resolve("");
    r.readAsDataURL(blob);
  });
  let text = "";
  try {
    const res = await api().transcribe_audio(dataUrl);
    if (res && res.text) text = res.text.trim();
    else if (res && res.error) toast(res.error, "warn", 4000);
  } catch (e) { /* ignore */ }
  voice.transcribing = false;
  if (!text || !voice.active) { idleOrListen(); return; }
  // A pending worker permission takes priority even when soft-muted: the app
  // asked YOU a yes/no, so it's listening for the answer without a wake word.
  if (voice.perm) {
    const ans = classifyPermission(text);
    if (ans) { addVoiceTurn(text, false); resolvePerm(ans); return; }
    toast("Say “yes”, “no”, or “always” — or tap a button.", "info", 3500);
    setVoiceStatus("Needs your OK — say “yes” or “no”");
    return;
  }
  // Soft-muted (wake-gated): this burst only counts if it's the wake word.
  // Anything else -- talking to someone else, thinking aloud -- is ignored, so
  // it never gets the wrong instruction.
  if (voice.gated && !voice.gateOpen) {
    const m = wakeMatches(text);
    if (!m) { idleOrListen(); return; }        // not for us -- keep listening for the wake word
    if (voice.speaking || voice.thinking) interruptReply();  // summoned mid-reply: stop and listen
    voice.gateOpen = true;
    if (m.command) { earcon("wake"); submitVoiceRequest(m.command); }  // "wake + request" in one breath
    else { acknowledgeWake(); setVoiceOrb("listening"); setVoiceStatus("Yes? Go ahead…"); }
    return;
  }
  // "Say that again" / "repeat" -> replay the last reply instead of a new turn.
  if (voice.lastReply.length && classifyReplay(text)) { replayLastReply(); return; }
  submitVoiceRequest(text);
}

// Send one request to the delegator, then -- in wake-gated mode -- soft-mute
// again until the wake word is heard.
function submitVoiceRequest(text) {
  addVoiceTurn(text, false);
  voice.sendTries = 0;
  sendVoiceTurn(text);
  if (voice.gated) {
    voice.gateOpen = false;   // one request per wake word
    earcon("stop");           // "got it, muting" cue
  }
}

// -- scrolling transcript in the overlay ---------------------------------- //
function addVoiceTurn(text, isReply) {
  const cap = $("voice-caption");
  if (!isReply) {
    const block = document.createElement("div");
    block.className = "voice-turn";
    const you = document.createElement("div");
    you.className = "voice-you";
    you.textContent = text;
    block.appendChild(you);
    cap.appendChild(block);
    voice.curTurnEl = block;
    voice.replyEl = null;
  }
  cap.scrollTop = cap.scrollHeight;
}
// Returns the live <.voice-it> element for the reply being spoken, creating a
// block for it (announcements have no preceding user turn).
function voiceReplyEl() {
  if (voice.replyEl) return voice.replyEl;
  let block = voice.curTurnEl;
  if (!block || block.querySelector(".voice-it")) {
    block = document.createElement("div");
    block.className = "voice-turn";
    $("voice-caption").appendChild(block);
    voice.curTurnEl = block;
  }
  const it = document.createElement("div");
  it.className = "voice-it";
  block.appendChild(it);
  voice.replyEl = it;
  return it;
}
function classifyReplay(text) {
  const t = " " + text.toLowerCase().replace(/[^a-z\s]/g, " ") + " ";
  return /\b(say that again|repeat that|repeat|come again|what did you say|again please)\b/.test(t);
}

function idleOrListen() {
  if (!voice.active) return;
  if (voice.perm) { setVoiceOrb("listening"); setVoiceStatus("Needs your OK — say “yes” or “no”"); return; }
  if (voice.speaking) { setVoiceOrb("speaking"); setVoiceStatus("Speaking…"); return; }
  if (voice.thinking) { setVoiceOrb("thinking"); setVoiceStatus("Thinking…"); return; }
  // Soft-muted between requests: won't take instructions until the wake word.
  if (voice.gated && !voice.gateOpen) {
    setVoiceOrb("gated");
    setVoiceStatus(`Muted — say “${wakePhrase()}” to talk`);
    return;
  }
  if (voice.ptt) { setVoiceOrb("idle"); setVoiceStatus("Your turn — hold to talk"); }
  else { setVoiceOrb("listening"); setVoiceStatus("Listening…"); }
}

function sendVoiceTurn(text) {
  voice.thinking = true;
  voice.turnComplete = false;
  setVoiceOrb("thinking");
  setVoiceStatus("Thinking…");
  startThinkCue();
  api().send_voice(text).then((res) => {
    if (res && res.error === "busy" && voice.sendTries < 12) {
      // Still wrapping up the previous reply; hold this one briefly.
      voice.sendTries++;
      setTimeout(() => { if (voice.active) sendVoiceTurn(text); }, 300);
    }
  }).catch(() => {});
}

// Barge-in: the user cut in. Stop playback AND tell the backend to abandon the
// reply it's generating, so it doesn't resume talking a moment later.
function interruptReply() {
  resetTtsPlayback();
  stopThinkCue();
  voice.speaking = false;
  voice.thinking = false;
  voice.turnComplete = true;  // reply abandoned; don't leave a dangling gap
  try { api().cancel_voice(); } catch (e) { /* ignore */ }
}

// The TTS queue drained. But sentence chunks synthesize one at a time, so an
// empty queue MID-reply is just a gap before the next sentence -- not the end.
// Only truly hand the mic back once the reply turn is also complete; otherwise
// stay in "speaking" so we don't reopen the mic (and catch our own echo).
function onVoiceTtsIdle() {
  if (!voice.active) return;
  if (!voice.turnComplete) return;  // between sentences -- keep waiting
  voice.speaking = false;
  voice.thinking = false;
  stopThinkCue();
  idleOrListen();
  processAnnounceQueue();
}

// A worker finished while we may have been mid-conversation. Announcements run
// as short convo turns; if the agent is busy, hold and retry when it frees up.
function processAnnounceQueue() {
  if (!voice.active || voice.speaking || voice.transcribing || voice.recording) return;
  if (!voice.announceQ.length) return;
  const a = voice.announceQ.shift();
  api().announce_worker(a.name, a.status, a.result).then((res) => {
    if (res && res.error === "busy") { voice.announceQ.unshift(a); setTimeout(processAnnounceQueue, 600); }
  }).catch(() => {});
}

// -- approve-by-voice: a worker needs an OK for a gated action ------------- //
function showNextPerm() {
  if (voice.perm || !voice.permQ.length) return;
  voice.perm = voice.permQ.shift();
  const p = voice.perm;
  $("voice-perm-q").textContent = `“${p.worker}” wants to ${spokenTitle(p.title)}`;
  $("voice-perm-detail").textContent = (p.preview || "").slice(0, 400);
  $("voice-perm-always").hidden = !p.always;
  $("voice-perm-always").textContent = p.always || "Always";
  $("voice-perm").hidden = false;
  setVoiceStatus("Needs your OK — say “yes” or “no”");
}
function spokenTitle(t) {
  t = String(t || "do that").split("\n")[0];
  return t.length > 60 ? t.slice(0, 60) + "…" : t;
}
function hidePermCard() {
  voice.perm = null;
  $("voice-perm").hidden = true;
  if (voice.permQ.length) showNextPerm();
  else idleOrListen();
}
function resolvePerm(answer) {
  if (!voice.perm) return;
  const rid = voice.perm.rid;
  try { api().resolve_worker_permission(rid, answer, ""); } catch (e) { /* ignore */ }
  hidePermCard();
}
// Map a spoken reply to an approval answer, or null if it isn't a clear yes/no.
function classifyPermission(text) {
  const t = " " + text.toLowerCase().replace(/[^a-z\s]/g, " ") + " ";
  if (/\balways\b/.test(t)) return "a";
  if (/\b(yes|yeah|yep|yup|sure|ok|okay|okey|fine|allow|approve|approved|go ahead|do it|go for it|sounds good|please do|affirmative)\b/.test(t)) return "y";
  if (/\b(no|nope|nah|dont|do not|deny|denied|stop|cancel|skip|don t|negative)\b/.test(t)) return "n";
  return null;
}

function renderVoiceWorkers() {
  const el = $("voice-workers");
  if (!el) return;
  const ws = Object.values(voice.workers);
  if (!ws.length) { el.innerHTML = ""; return; }
  el.innerHTML = ws.map((w) => {
    const cls = w.status === "running" ? "vw-run"
      : (w.status === "done" ? "vw-done" : (w.status === "stopped" ? "vw-stop" : "vw-err"));
    const icon = w.status === "running" ? "●"
      : (w.status === "done" ? "✓" : (w.status === "stopped" ? "◼" : "✕"));
    const act = (w.status === "running" && w.activity)
      ? `<span class="vw-activity">${esc(w.activity)}</span>` : "";
    return `<div class="voice-worker ${cls}"><span class="vw-icon">${icon}</span>` +
           `<span class="vw-name">${esc(w.name)}</span>${act}</div>`;
  }).join("");
}

// Turn a worker's streamed action into a short human activity line.
function workerActivity(kind, data) {
  if (kind === "tool_call") {
    const n = data.name || "working";
    const map = {
      edit_file: "editing", write_file: "writing", read_file: "reading",
      run_powershell: "running a command", run_tests: "running tests",
      run_test_file: "running tests", grep: "searching", glob: "searching",
      list_dir: "looking around", web_search: "searching the web",
    };
    const verb = map[n] || n.replace(/_/g, " ");
    const a = data.args || {};
    const target = a.path || a.pattern || a.query || a.command || "";
    return target ? `${verb} ${String(target).split(/[\\/]/).pop().slice(0, 40)}` : verb;
  }
  if (kind === "stream_start") return "thinking…";
  return null;
}

function handleVoiceEvent(ev) {
  switch (ev.type) {
    case "stream_start":
      voice._replyBuf = "";
      voice.replyEl = null;
      voice.replyChunks = [];
      voice.turnComplete = false;  // a reply is now being generated + spoken
      break;
    case "content": {
      voice._replyBuf = (voice._replyBuf || "") + (ev.text || "");
      voiceReplyEl().textContent = voice._replyBuf;
      $("voice-caption").scrollTop = $("voice-caption").scrollHeight;
      break;
    }
    case "play_audio":
      voice.speaking = true;
      voice.thinking = false;
      stopThinkCue();
      if (ev.src) voice.replyChunks.push(ev.src);  // for "say that again"
      setVoiceOrb("speaking");
      setVoiceStatus("Speaking…");
      handlePlayAudio(ev);
      break;
    case "tts_reset":
      resetTtsPlayback();
      break;
    case "voice_ack": {
      // Short spoken "Yes?" after the wake word. Gate the mic (acking) while it
      // plays so it isn't recorded as your request, but a loud barge-in still
      // gets through.
      if (!voice.active) break;
      try {
        voice.acking = true;
        const a = new Audio(ev.src);
        voice.ackAudio = a;
        const done = () => { voice.acking = false; voice.ackAudio = null; };
        a.addEventListener("ended", done);
        a.addEventListener("error", done);
        a.play().catch(done);
      } catch (e) { voice.acking = false; }
      break;
    }
    case "tool_call": {
      // The delegator peeking at the code to answer you -- show what it's on.
      const LOOK = { read_file: "Reading", grep: "Searching", glob: "Searching",
                     list_dir: "Looking in", find_references: "Searching",
                     review_changes: "Checking changes" };
      if (LOOK[ev.name]) {
        const a = ev.args || {};
        const t = a.path || a.pattern || a.query || a.dir || "";
        setVoiceStatus(t ? `${LOOK[ev.name]} ${String(t).split(/[\\/]/).pop().slice(0, 40)}…`
                         : `${LOOK[ev.name]}…`);
      }
      break;
    }
    case "worker_update": {
      const prev = voice.workers[ev.id] || {};
      voice.workers[ev.id] = { name: ev.name, status: ev.status, activity: prev.activity || "" };
      renderVoiceWorkers();
      if (ev.status === "done" || ev.status === "error") {
        earcon(ev.status === "done" ? "done" : "stop");  // a cue before it speaks up
        voice.announceQ.push({ name: ev.name, status: ev.status, result: ev.result || "" });
        processAnnounceQueue();
      }
      break;
    }
    case "subagent_stream": {
      // Live activity from a background worker's own thread.
      const w = voice.workers[ev.id];
      if (w && w.status === "running") {
        const line = workerActivity(ev.kind, ev);
        if (line) { w.activity = line; renderVoiceWorkers(); }
      }
      break;
    }
    case "worker_permission":
      voice.permQ.push({ rid: ev.rid, worker: ev.worker, title: ev.title,
                         preview: ev.preview, always: ev.always });
      showNextPerm();
      break;
    case "voice_turn_complete":
      // The reply is fully generated. Spoken audio may still be draining from
      // the queue; if so, its drain will hand the mic back. If nothing is
      // playing (a silent worker dispatch, or audio already finished), resume
      // now.
      voice.turnComplete = true;
      if (voice.replyChunks.length) {
        voice.lastReply = voice.replyChunks.slice();
        $("voice-replay").hidden = false;
      }
      if (!ttsPlaying && voice.active) onVoiceTtsIdle();
      break;
    case "error":
      toast("Voice: " + (ev.message || "error"), "warn", 5000);
      break;
  }
}

function setVoicePtt(on) {
  voice.ptt = on;
  const t = $("voice-ptt-toggle");
  t.setAttribute("aria-checked", String(on));
  t.textContent = on ? "Push-to-talk" : "Hands-free";
  $("voice-ptt-btn").hidden = !on;
  if (voice.active) {
    if (on) { setVoiceStatus("Hold Space or the button to talk"); setVoiceOrb("idle"); }
    else { setVoiceStatus("Listening… just talk"); setVoiceOrb("listening"); }
  }
}

function applyGateToggleUI() {
  const t = $("voice-gate-toggle");
  if (!t) return;
  t.setAttribute("aria-checked", String(!!voice.gated));
  t.classList.toggle("on", !!voice.gated);
}
function setVoiceGated(on) {
  voice.gated = !!on;
  if (!voice.gated) {
    voice.gateOpen = true;            // continuous: always listening for a request
  } else if (!voice.speaking && !voice.thinking && !voice.recording && !voice.perm) {
    voice.gateOpen = false;           // soft-mute now; wake word needed for the next one
  }                                    // (mid-turn: it re-gates after this request)
  applyGateToggleUI();
  if (voice.active) idleOrListen();
}

function pttPress() {
  if (!voice.active || !voice.ptt || voice.recording || voice.transcribing) return;
  if (voice.speaking || voice.thinking) interruptReply();
  // In PTT we drive the recorder directly, bypassing the energy gate -- keep
  // whatever was captured (confirmed) regardless of measured energy.
  startUtterance();
  voice.confirmed = true;
  $("voice-ptt-btn").classList.add("held");
}
function pttRelease() {
  if (!voice.ptt || !voice.recording) return;
  $("voice-ptt-btn").classList.remove("held");
  endUtterance();
}

// -- earcons: tiny synthesized cues so turn-taking feels responsive -------- //
function earcon(kind) {
  if (!settings || !settings.voice_earcons || !voice.ctx) return;
  try {
    const ctx = voice.ctx;
    const now = ctx.currentTime;
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    const map = { endpoint: [660, 0.07], done: [784, 0.11], stop: [360, 0.13], wake: [740, 0.1] };
    const [freq, dur] = map[kind] || [600, 0.08];
    o.type = "sine";
    o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, now);
    g.gain.exponentialRampToValueAtTime(0.09, now + 0.012);
    g.gain.exponentialRampToValueAtTime(0.0001, now + dur);
    o.start(now); o.stop(now + dur + 0.02);
  } catch (e) { /* ignore */ }
}

// -- thinking cue: soft periodic ticks while it works, so the gap between you
// finishing and it speaking doesn't feel like dead air. Stops the instant the
// reply's first audio plays. --------------------------------------------------
function startThinkCue() {
  if (!settings || !settings.voice_earcons || !voice.ctx) return;
  stopThinkCue();
  voice.thinkTimer = setInterval(() => {
    if (!voice.active || voice.speaking) { stopThinkCue(); return; }
    try {
      const ctx = voice.ctx, now = ctx.currentTime;
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = "sine"; o.frequency.value = 340;
      g.gain.setValueAtTime(0.0001, now);
      g.gain.exponentialRampToValueAtTime(0.035, now + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, now + 0.16);
      o.start(now); o.stop(now + 0.18);
    } catch (e) { /* ignore */ }
  }, 620);
}
function stopThinkCue() { if (voice.thinkTimer) { clearInterval(voice.thinkTimer); voice.thinkTimer = 0; } }

// -- live waveform ---------------------------------------------------------- //
function startWaveform() {
  const canvas = $("voice-wave");
  if (!canvas || !voice.analyser) return;
  const ctx2d = canvas.getContext("2d");
  const buf = new Uint8Array(voice.analyser.fftSize);
  const draw = () => {
    if (!voice.active) return;
    voice.waveRaf = requestAnimationFrame(draw);
    const w = canvas.width, h = canvas.height;
    ctx2d.clearRect(0, 0, w, h);
    voice.analyser.getByteTimeDomainData(buf);
    const accent = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent").trim() || "#7aa0ff";
    ctx2d.lineWidth = 2;
    ctx2d.strokeStyle = voice.muted ? "rgba(140,140,150,0.5)"
      : (voice.speaking ? "#34c759" : accent);
    ctx2d.beginPath();
    const step = w / buf.length;
    for (let i = 0; i < buf.length; i++) {
      const v = voice.muted ? 0 : (buf[i] - 128) / 128;
      const y = h / 2 + v * (h / 2) * 0.9;
      const x = i * step;
      i ? ctx2d.lineTo(x, y) : ctx2d.moveTo(x, y);
    }
    ctx2d.stroke();
  };
  draw();
}

// -- mute: pause listening without ending the session ---------------------- //
function toggleMute() {
  voice.muted = !voice.muted;
  if (voice.stream) voice.stream.getAudioTracks().forEach((t) => { t.enabled = !voice.muted; });
  if (voice.muted && voice.recording) endUtterance();
  $("voice-mute").setAttribute("aria-pressed", String(voice.muted));
  $("voice-mute").classList.toggle("active", voice.muted);
  if (voice.muted) { setVoiceOrb("idle"); setVoiceStatus("Muted — tap the mic to resume"); }
  else idleOrListen();
}

// -- "say that again": replay the last spoken reply ------------------------ //
function replayLastReply() {
  if (!voice.lastReply.length) return;
  if (voice.recording) endUtterance();
  interruptReply();          // stop anything currently playing first
  voice.speaking = true;
  voice.turnComplete = true;  // a replay, not a new generation
  setVoiceOrb("speaking");
  setVoiceStatus("Replaying…");
  for (const src of voice.lastReply) enqueueTtsPlayback(src);
}

// -- wake word: start a hands-free session by saying a phrase --------------- //
// When armed (and voice mode is off) this keeps a light energy-VAD listener
// going, transcribing only the short bursts where you actually spoke, and opens
// voice mode when it hears the configured phrase. All local -- nothing leaves
// the machine. Opt-in; off by default.
const wake = {
  armed: false, stream: null, ctx: null, analyser: null, data: null, timer: 0,
  rec: null, chunks: [], recording: false, busy: false,
  voicedMs: 0, silenceMs: 0, recMs: 0, noiseFloor: 0.012,
};
const WK_FRAME_MS = 60, WK_SILENCE_MS = 550, WK_MIN_MS = 250, WK_MAX_MS = 5000;

async function armWake(retry = true) {
  if (wake.armed || voice.active) return;
  if (!settings || !settings.voice_wake_enabled) return;
  try {
    wake.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    // The mic can be briefly busy right after a voice session releases it. Try
    // once more before giving up, so the wake word doesn't silently stay off.
    if (retry && settings.voice_wake_enabled && !voice.active) setTimeout(() => armWake(false), 700);
    return;
  }
  if (voice.active) {  // a session opened while we were awaiting the mic
    wake.stream.getTracks().forEach((t) => t.stop());
    wake.stream = null;
    return;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  wake.ctx = new Ctx();
  const src = wake.ctx.createMediaStreamSource(wake.stream);
  wake.analyser = wake.ctx.createAnalyser();
  wake.analyser.fftSize = 1024;
  wake.data = new Uint8Array(wake.analyser.fftSize);
  src.connect(wake.analyser);
  wake.armed = true;
  $("voice-chip").classList.add("armed");
  $("voice-chip").title = `Listening for “${(settings.voice_wake_word || "hey assistant")}” — click to open voice`;
  wake.timer = setInterval(wakeTick, WK_FRAME_MS);
}

function disarmWake() {
  if (wake.timer) { clearInterval(wake.timer); wake.timer = 0; }
  try { if (wake.rec && wake.recording) wake.rec.stop(); } catch (e) { /* ignore */ }
  wake.recording = false;
  if (wake.stream) { wake.stream.getTracks().forEach((t) => t.stop()); wake.stream = null; }
  if (wake.ctx) { try { wake.ctx.close(); } catch (e) { /* ignore */ } wake.ctx = null; }
  wake.armed = false;
  $("voice-chip").classList.remove("armed");
  $("voice-chip").title = "Talk to it — hands-free voice conversation";
}

function wakeEnergy() {
  wake.analyser.getByteTimeDomainData(wake.data);
  let sum = 0;
  for (let i = 0; i < wake.data.length; i++) { const v = (wake.data[i] - 128) / 128; sum += v * v; }
  return Math.sqrt(sum / wake.data.length);
}

function wakeTick() {
  if (!wake.armed || wake.busy) return;
  const e = wakeEnergy();
  const startT = Math.max(0.012, wake.noiseFloor * 3);
  if (wake.recording) {
    wake.recMs += WK_FRAME_MS;
    if (e > wake.noiseFloor * 1.8) { wake.voicedMs += WK_FRAME_MS; wake.silenceMs = 0; }
    else wake.silenceMs += WK_FRAME_MS;
    if (wake.silenceMs >= WK_SILENCE_MS || wake.recMs >= WK_MAX_MS) wakeEndUtterance();
    return;
  }
  if (e > startT) { wakeStartUtterance(); }
  else {
    wake.noiseFloor = 0.98 * wake.noiseFloor + 0.02 * e;
    wake.noiseFloor = Math.min(0.05, Math.max(0.004, wake.noiseFloor));
  }
}

function wakeStartUtterance() {
  let mime = "";
  if (window.MediaRecorder) {
    if (MediaRecorder.isTypeSupported("audio/webm")) mime = "audio/webm";
    else if (MediaRecorder.isTypeSupported("audio/ogg")) mime = "audio/ogg";
  }
  try {
    wake.rec = mime ? new MediaRecorder(wake.stream, { mimeType: mime }) : new MediaRecorder(wake.stream);
  } catch (e) { return; }
  wake.chunks = []; wake.recording = true; wake.voicedMs = 0; wake.silenceMs = 0; wake.recMs = 0;
  wake.rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) wake.chunks.push(ev.data); };
  wake.rec.onstop = wakeFinish;
  wake.rec.start(200);
}

function wakeEndUtterance() {
  if (!wake.recording) return;
  wake.recording = false;
  wake.discard = wake.voicedMs < WK_MIN_MS;
  try { wake.rec.stop(); } catch (e) { /* onstop fires */ }
}

async function wakeFinish() {
  if (wake.discard || !wake.chunks.length || !wake.armed) return;
  wake.busy = true;
  const blob = new Blob(wake.chunks, { type: wake.chunks[0].type || "audio/webm" });
  wake.chunks = [];
  const dataUrl = await new Promise((resolve) => {
    const r = new FileReader();
    r.onloadend = () => resolve(r.result);
    r.onerror = () => resolve("");
    r.readAsDataURL(blob);
  });
  let text = "";
  try {
    const res = await api().transcribe_audio(dataUrl);
    if (res && res.text) text = res.text.trim();
  } catch (e) { /* ignore */ }
  wake.busy = false;
  if (!wake.armed) return;
  const m = text && wakeMatches(text);
  if (m) {
    disarmWake();
    await startVoice(true);  // opened via wake word -> gating ON by default
    // "hey assistant, add dark mode" -> open the session AND run that request
    // (in gated mode submitVoiceRequest re-mutes afterward).
    if (m.command && voice.active) submitVoiceRequest(m.command);
    else if (voice.active) acknowledgeWake();  // just the wake word -> say we're listening
  }
}

// Confirm the wake word was heard: an instant tone plus a short spoken "Yes?"
// so you know it's actually listening for your request.
function acknowledgeWake() {
  earcon("wake");
  try { api().voice_ack(); } catch (e) { /* ignore */ }
}
function stopAck() {
  if (voice.ackAudio) { try { voice.ackAudio.pause(); } catch (e) { /* ignore */ } }
  voice.ackAudio = null;
  voice.acking = false;
}

// True (with any trailing command) if the transcript contains the wake phrase.
function wakeMatches(text) {
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  const phrase = norm((settings && settings.voice_wake_word) || "hey assistant");
  if (!phrase) return null;
  const t = norm(text);
  const idx = t.indexOf(phrase);
  if (idx === -1) return null;
  return { command: t.slice(idx + phrase.length).trim() };
}

// Keep the wake listener in sync with the setting and voice-mode state.
function refreshWake() {
  if (settings && settings.voice_wake_enabled && !voice.active) armWake();
  else disarmWake();
}

$("voice-chip").addEventListener("click", () => { if (voice.active) stopVoice(); else startVoice(false); });
$("voice-close").addEventListener("click", stopVoice);
$("voice-mute").addEventListener("click", toggleMute);
$("voice-replay").addEventListener("click", replayLastReply);
$("voice-settings").addEventListener("click", openSettings);
$("voice-gate-toggle").addEventListener("click", () => setVoiceGated(!voice.gated));
$("voice-perm-yes").addEventListener("click", () => resolvePerm("y"));
$("voice-perm-always").addEventListener("click", () => resolvePerm("a"));
$("voice-perm-no").addEventListener("click", () => resolvePerm("n"));
$("voice-ptt-toggle").addEventListener("click", () => setVoicePtt(!voice.ptt));
$("voice-ptt-btn").addEventListener("mousedown", pttPress);
$("voice-ptt-btn").addEventListener("mouseup", pttRelease);
$("voice-ptt-btn").addEventListener("mouseleave", pttRelease);
window.addEventListener("keydown", (e) => {
  if (e.code === voice.pttKey && voice.active && voice.ptt && !e.repeat &&
      document.activeElement?.tagName !== "INPUT" &&
      document.activeElement?.tagName !== "TEXTAREA") {
    e.preventDefault(); pttPress();
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === voice.pttKey && voice.active && voice.ptt) { e.preventDefault(); pttRelease(); }
});

function bootSafely() {
  boot().catch((e) => {
    try { api().log && api().log("boot:error " + e); } catch (_) { /* ignore */ }
    console.error("boot failed", e);
  });
}

if (window.pywebview && window.pywebview.api) bootSafely();
else window.addEventListener("pywebviewready", bootSafely);

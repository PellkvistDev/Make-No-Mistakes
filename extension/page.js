// Injected once per frame; everything that touches the page happens here.
//
// It defines ONE entry point on the isolated world's global, because
// chrome.scripting.executeScript({func}) serialises the function it is given
// and injects it standalone -- a func referencing module scope would arrive
// with nothing to reference. The isolated world persists for the life of the
// frame, so background.js injects this file once and then just calls in.
//
// Extensions cannot eval strings (MV3 forbids unsafe-eval), which is why the
// snapshot cannot simply be sent over from Python the way it is handed to
// Playwright. It is GENERATED into the block below instead, from the same
// SNAPSHOT_JS the desktop uses, so the two cannot drift.

(() => {
  if (globalThis.__mnmAct) return;

  /* ==== GENERATED — do not edit by hand ==== */
  // From glmcode/browser_session.py — run scripts/gen_extension_page.py
  const MNM_SELECTOR = "a, button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=textbox], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=switch], [onclick]";
  const MNM_SNAPSHOT = (sel) => {
    const regionOf = (e) => {
      if (e.closest('[role=dialog],[aria-modal="true"],dialog')) return 'dialog';
      if (e.closest('nav,[role=navigation]')) return 'nav';
      if (e.closest('header,[role=banner]')) return 'header';
      if (e.closest('footer,[role=contentinfo]')) return 'footer';
      return 'main';
    };
    const labelOf = (e) => {
      const cand = e.getAttribute('aria-label') || e.getAttribute('placeholder')
        || e.getAttribute('name') || e.getAttribute('alt') || e.getAttribute('title');
      if (cand && cand.trim()) return cand.trim();
      const t = (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ');
      if (t) return t;
      if (typeof e.value === 'string' && e.value.trim()) return e.value.trim();
      return '';
    };
    let next = window.__mnmNextRef || 1;
    const seen = new Set();
    const out = [];
    for (const e of document.querySelectorAll(sel)) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = e.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      let ref = parseInt(e.dataset.mnmRef || '', 10);
      if (!ref || seen.has(ref)) { ref = next++; e.dataset.mnmRef = String(ref); }
      seen.add(ref);
      const tag = e.tagName.toLowerCase();
      const item = {
        ref, tag,
        label: labelOf(e).slice(0, 80),
        region: regionOf(e),
        disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
        type: (e.getAttribute('type') || '').toLowerCase(),
      };
      if ((tag === 'input' || tag === 'textarea') && typeof e.value === 'string'
          && e.value && item.type !== 'submit' && item.type !== 'button')
        item.value = e.value.slice(0, 40);
      if (tag === 'select') {
        item.options = [...e.options].slice(0, 12).map(o => (o.label || o.value || '').slice(0, 40));
        const so = e.selectedOptions && e.selectedOptions[0];
        if (so) item.value = (so.label || so.value || '').slice(0, 40);
      }
      if (e.checked === true) item.checked = true;
      out.push(item);
    }
    window.__mnmNextRef = next;
    const outline = [...document.querySelectorAll('h1,h2')].slice(0, 6)
      .map(h => (h.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60))
      .filter(Boolean);
    return { items: out, outline };
  };
  /* ==== END GENERATED ==== */

  const byRef = (ref) => document.querySelector(`[data-mnm-ref="${String(ref).replace(/"/g, "")}"]`);

  function need(ref) {
    const el = byRef(ref);
    if (!el) throw new Error(`Element [${ref}] is no longer on the page — it changed since your snapshot. Snapshot again and use the fresh refs.`);
    return el;
  }

  // A real event sequence, not el.click(). Frameworks listen on pointer and
  // mouse events at least as often as on click, and a bare click() skips
  // every one of them -- which shows up as "the button did nothing".
  function press(el, x, y) {
    const opts = { bubbles: true, cancelable: true, composed: true, view: window,
                   clientX: x, clientY: y, button: 0, buttons: 1 };
    el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    try { el.focus({ preventScroll: true }); } catch {}
    el.dispatchEvent(new PointerEvent("pointerup", { ...opts, buttons: 0, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...opts, buttons: 0 }));
    el.dispatchEvent(new MouseEvent("click", { ...opts, buttons: 0, detail: 1 }));
  }

  function centre(el) {
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  }

  // React (and anything else that owns the value property) ignores a plain
  // el.value = x: it tracks its own last-rendered value and concludes nothing
  // changed. Going through the prototype's native setter is what makes the
  // framework see a real edit.
  function setValue(el, text) {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, text);
    else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  }

  const KEYS = {
    Enter: { key: "Enter", code: "Enter", keyCode: 13 },
    Tab: { key: "Tab", code: "Tab", keyCode: 9 },
    Escape: { key: "Escape", code: "Escape", keyCode: 27 },
    Backspace: { key: "Backspace", code: "Backspace", keyCode: 8 },
    ArrowDown: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
    ArrowUp: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
    PageDown: { key: "PageDown", code: "PageDown", keyCode: 34 },
    PageUp: { key: "PageUp", code: "PageUp", keyCode: 33 },
    Home: { key: "Home", code: "Home", keyCode: 36 },
    End: { key: "End", code: "End", keyCode: 35 },
  };

  function sendKey(el, name) {
    const k = KEYS[name] || { key: name, code: name, keyCode: 0 };
    const init = { ...k, bubbles: true, cancelable: true, composed: true, view: window };
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keypress", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
    // PageDown/PageUp/Home/End are handled by the viewport, not a listener, so
    // a synthetic key event alone scrolls nothing.
    const scrolls = { PageDown: innerHeight * 0.9, PageUp: -innerHeight * 0.9 };
    if (name in scrolls) scrollBy({ top: scrolls[name], behavior: "instant" });
    if (name === "Home") scrollTo({ top: 0, behavior: "instant" });
    if (name === "End") scrollTo({ top: document.body.scrollHeight, behavior: "instant" });
  }

  globalThis.__mnmAct = function (a) {
    try {
      switch (a.kind) {
        case "size":
          return { value: { width: innerWidth, height: innerHeight } };

        case "snapshot":
          return { value: MNM_SNAPSHOT(MNM_SELECTOR) };

        case "text": {
          // The TRUE length travels with the slice. Python pages through this
          // text and tells the model how much of the page is left, so a cap
          // applied silently here would have it report a truncated document's
          // length as the whole page -- the agent would page cleanly to "the
          // end" of something that was already cut, and be told it had read
          // all of it.
          const t = (document.body && document.body.innerText) || "";
          const max = a.max || 6000;
          return { value: { text: t.slice(0, max), total: t.length } };
        }

        case "exists":
          return { value: !!byRef(a.ref) };

        case "enabled": {
          const el = need(a.ref);
          return { value: !(el.disabled || el.getAttribute("aria-disabled") === "true") };
        }

        case "click": {
          const el = need(a.ref);
          el.scrollIntoView({ block: "center", behavior: "instant" });
          press(el, ...centre(el));
          return { value: true };
        }

        case "click_at": {
          const el = document.elementFromPoint(a.x, a.y);
          if (!el) return { error: `Nothing is at (${a.x}, ${a.y}) — the page may have scrolled since your screenshot.` };
          press(el, a.x, a.y);
          return { value: true };
        }

        case "fill": {
          const el = need(a.ref);
          el.scrollIntoView({ block: "center", behavior: "instant" });
          try { el.focus({ preventScroll: true }); } catch {}
          if (el.isContentEditable) {
            el.textContent = a.text;
            el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
          } else {
            setValue(el, a.text);
          }
          if (a.submit) {
            sendKey(el, "Enter");
            const form = el.form || el.closest("form");
            // A form whose submit button is the only handler ignores a
            // synthetic Enter, and "I pressed Enter and nothing happened" is
            // the single most common way this looked broken.
            if (form && !form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))) {
              /* a listener called preventDefault -- it handled it */
            } else if (form && typeof form.requestSubmit === "function") {
              try { form.requestSubmit(); } catch {}
            }
          }
          return { value: true };
        }

        case "select": {
          const el = need(a.ref);
          const want = String(a.text);
          const opts = [...(el.options || [])];
          const hit = opts.find((o) => (o.label || "").trim() === want)
                   || opts.find((o) => (o.value || "") === want)
                   || opts.find((o) => (o.textContent || "").trim() === want);
          if (!hit) {
            return { error: `[${a.ref}] is a dropdown with no option '${want}'. Its options are: ` +
                            (opts.map((o) => (o.label || o.value || "").trim()).join(", ") || "(none)") };
          }
          el.value = hit.value;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { value: true };
        }

        case "press": {
          sendKey(document.activeElement || document.body, a.key);
          return { value: true };
        }
      }
      return { error: `Unknown action '${a.kind}'` };
    } catch (e) {
      return { error: String((e && e.message) || e) };
    }
  };
})();

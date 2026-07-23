# Make No Mistakes — phone app (Path A)

A coding agent that runs **entirely on your phone**. Your phone is the compute:
the model API is the brain, and your **GitHub repo is the filesystem**. The agent
reads, searches, and edits files over the GitHub API and commits each change — so
you can start a feature from a new empty repo on the bus, and your desktop picks
it up (auto-pull) the next time it's on.

There is **no backend**. Nothing sits between your phone and the two services it
talks to. That's the whole security idea.

## What it can and can't do

- ✅ Read / list / glob / grep / semantic `search_code` across the repo
- ✅ Write and edit files, each committed straight to the branch
- ✅ Create a fresh repo and build in it from scratch
- ❌ Run code, tests, or servers — a phone has no OS to run them on. The agent
  makes correct, minimal edits and tells you what still needs verifying. That
  verification happens on your **desktop** (which auto-pulls) or in **CI**
  (GitHub Actions — see `docs/agent-workflow.yml` and `glmcode/ci.py`).

## Security model

This was built security-first ("MAKE IT SECURE!"). The guarantees:

1. **Keys encrypted at rest, on-device only.** Your model key and GitHub token
   are encrypted with **AES-GCM**, using a key derived from your **PIN** via
   **PBKDF2 (210,000 iterations, SHA-256)**. Only the ciphertext is stored
   (`localStorage`). The PIN is never stored. A stolen phone is useless without
   the PIN — tested: a wrong PIN fails the AES-GCM auth tag, it never returns
   garbage, and the stored blob contains no plaintext.
2. **Secrets live in memory only while unlocked**, and the app **auto-locks**
   (drops the decrypted keys) the moment it's backgrounded, and after 5 min idle.
3. **The network is pinned.** A strict `Content-Security-Policy` in `index.html`
   allows connections to **only** the model API host and `api.github.com`, and
   loads **only our own bundled scripts** — no CDNs, no inline scripts, no
   `eval`. There is nowhere for an injected key-stealer to phone home to.
4. **Least-privilege GitHub token.** Use a **fine-grained** token scoped to just
   the repos you use, **Contents: Read and write** only.
5. **Writes are gated.** Every commit goes through a confirmation dialog by
   default — the agent can't silently push.
6. **The service worker never caches API traffic** — only the static app shell.

## Files

| File | Role |
|---|---|
| `agent-core.js` | Pure, env-agnostic core: vault, GitHub/model clients, tools, agent loop. Unit-tested in Node. |
| `agent-core.test.js` | Node tests (`node --test mobile/agent-core.test.js`). |
| `index.html` | App shell + the strict CSP. |
| `app.js` | The only DOM layer: screens, vault storage, auto-lock, confirm dialog. |
| `style.css` | Phone-first dark styling, no external assets. |
| `manifest.webmanifest`, `sw.js` | Installable, offline-capable PWA. |

## Hosting it — free

It's all static files, so hosting is **free**. This repo ships a GitHub Pages
workflow (`.github/workflows/pages.yml`) that publishes the `mobile/` folder on
every push to `main`. To turn it on once:

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. Push to `main` (or run the *Deploy phone app to GitHub Pages* workflow by
   hand). The job prints the live URL — something like
   `https://<you>.github.io/<repo>/`.
3. Open that URL on your phone → **Add to Home Screen**.

Only the `mobile/` folder is published, so nothing else in the repo is exposed,
and because everything sensitive is encrypted client-side with your PIN, the
host (GitHub) never sees your keys or your code. Any other static host works too
(Cloudflare Pages, Netlify) — just serve `mobile/` over HTTPS.

> **Note on CORS:** `api.github.com` sends permissive CORS headers, so browser
> calls work directly. Some model endpoints may not allow browser origins; if
> your model host blocks CORS, put a thin relay in front of it (e.g. a Cloudflare
> Worker that only adds CORS headers and forwards to the model host) and point the
> base URL at it. Be aware of the tradeoff: such a relay does see the bearer token
> on each request it forwards, so run **your own** relay — never a shared one — and
> keep it stateless (no logging, no storage). GitHub needs no relay.

# Make No Mistakes

A Claude Code-style coding agent that is **100% free** to run — as a polished
**desktop app** (Apple-style liquid glass over a cinematic background) or in
the terminal. It uses z.ai's free GLM models:

| Role | Model | Cost |
|---|---|---|
| Coding / agent brain | `glm-4.7-flash` (30B MoE, ~200K context, 59.2 SWE-bench Verified) | free |
| Vision (screenshots, mockups, diagrams) | `glm-4.6v-flash` (9B VLM) | free |

It works like Claude Code: an agentic loop with real tools — it reads and edits
files, searches your codebase, runs PowerShell commands, keeps a todo list,
searches the web and fetches docs, and asks for your permission before doing
anything destructive (unless you put it in auto mode).

The agent gets quality-multiplying scaffolding a small free model needs:
every file edit is **syntax-checked immediately** (Python/JSON/TOML/JS) with
errors surfaced in the same tool result; each chat starts with a compact
**project layout map** in the system prompt so it navigates without burning
tool calls on exploration; a **`review_changes` tool** shows it a git diff of
everything changed since the turn started (against the automatic pre-turn
backup snapshot) for self-review; and if a turn edits files without running
anything, a one-time **verify nudge** pushes it to test its changes before
finishing. In the chat, file paths in `inline code` are **clickable** — they
open in whatever your OS associates with them.

## Setup (once)

> **Run every command below in PowerShell, not the old Command Prompt (cmd).**
> The installer is a PowerShell script and won't run under cmd. Open
> **Windows PowerShell** from the Start menu (or Windows Terminal → PowerShell
> tab). `$HOME` below expands to your user folder, e.g. `C:\Users\<you>`.

1. **Get a free API key** — sign up at [z.ai](https://z.ai) (free, no credit
   card), open your profile menu → **API Keys** → create a key.
2. **Get the code** — clone it into a `Make No Mistakes` folder:
   ```powershell
   cd $HOME
   git clone https://github.com/PellkvistDev/Make-No-Mistakes.git "Make No Mistakes"
   ```
3. **Install**:
   ```powershell
   cd "$HOME\Make No Mistakes"
   .\install.ps1
   ```
4. Set your API key as an environment variable (this is where Make No Mistakes reads
   it from):
   ```powershell
   setx ZAI_API_KEY your-key-here
   ```
   (Or just run `glm` — on first run it asks for the key and saves it as the
   `ZAI_API_KEY` user environment variable for you.)
5. Launch the **desktop app** with the "Make No Mistakes" desktop shortcut, or from a
   new terminal:
   ```powershell
   glmapp
   ```
   (Terminal version: `glm`. Without installing: `.\glmapp.cmd` / `.\glm.cmd`
   from this directory, or `python -m glmcode.gui` / `python -m glmcode`.)

## Updating

Pull the latest and you're done — the `glm`/`glmapp` launchers run the code
straight from this folder, so code changes take effect immediately:

```powershell
cd "$HOME\Make No Mistakes"
git pull
```

You only need to **re-run `.\install.ps1`** if you **move or rename this
folder** (the launchers and desktop shortcut have its path baked in, so they
break until you regenerate them) — or if `requirements.txt` changed.

## The desktop app

- **Liquid glass UI** — frameless window, translucent blurred panels, macOS-style
  traffic lights, smooth streaming markdown, dark cinematic backdrop. On older or
  low-powered machines, **Settings → Appearance → Reduce visual effects** turns off
  the blur and animations for a flat but fully responsive UI.
- **Parallel chats** — chats keep working when you switch away. Kick off a
  long task, jump to another project (or start a new chat) and keep talking;
  the sidebar shows a pulsing dot on chats that are working and a green dot
  on ones that finished while you were away (with a toast when they do).
  Each chat's tools are pinned to its own project folder, so simultaneous
  chats in different projects can't touch each other's files, and each keeps
  its own task checklist. If a background chat needs a permission answer, it
  waits patiently and asks the moment you switch back to it.
- **Slash commands** — type `/` in the composer for built-in actions (`/plan`,
  `/compact`, `/new`) and your own **saved prompts**. Define reusable prompts in
  **Settings → APIs → Slash commands** (use `$INPUT` for text you type after the
  command) and fire them with `/name` — e.g. `/review the auth module`.
- **Command palette (Ctrl/⌘+K)** — fuzzy-jump to any action (new chat, plan mode,
  compact, export, settings, stop), open any past chat, or switch the model, all
  from the keyboard.
- **Export chat to Markdown** — save any conversation to a `.md` file from the
  command palette.
- **MCP servers** — connect any [Model Context Protocol](https://modelcontextprotocol.io)
  server (stdio: `npx`, `uvx`, a script) in **Settings → APIs → MCP servers**, and
  its tools appear to the agent alongside the built-in ones, permission-gated like
  any other tool. Servers start in the background; a dead one just drops its tools
  instead of breaking anything.
- **Drag & drop attachments** — drop files or images anywhere on the window to
  attach them to your next message; same pipeline as the paperclip.
- **Prompt history** — press ↑ in the (empty) composer to recall your previous
  messages, terminal-style, per chat. ↓ walks back toward what you were typing.
- **@-mention files** — type `@` in the composer to fuzzy-search your project's
  files; pick one and its current contents are attached to your message, so the
  agent works from the exact code instead of hunting for it (or burning a turn
  reading it). Attach as many as you like; the file dumps stay out of your
  on-screen message.
- **Edit & resend any past message** — hover a message you sent and click the
  pencil. The chat rewinds to just before it — **and the project files are
  reverted to how they were at that point** (via the auto-backup snapshots) —
  then your edited message is resent as a fresh turn. Fix a typo or rethink an
  instruction three messages back without hand-undoing everything the agent
  did after it.
- **Copy code in one click** — every fenced code block in a reply has a
  hover-reveal **Copy** button that copies the raw (un-highlighted) code.
- **Desktop notifications** — when the app isn't the focused window, a native
  OS notification (Windows toast / macOS / Linux) fires the moment a chat
  needs a permission answer or finishes and is waiting for you, titled with
  the chat's name. No setup, no extra dependencies; on by default, with a
  toggle in **Settings → Notifications**. All app data — chats,
  backups, transcripts, memory, config — lives in `~/.makenomistakes/`
  (an existing `~/.glmcode/` folder from older versions is migrated
  automatically on first launch).
- **Chat history, one per project** — like Claude Code / Codex, every chat is
  tied to a project folder and saved to disk (`~/.makenomistakes/sessions/`). The
  sidebar (toggle button next to the traffic lights) lists all your chats with
  a title, project folder and last-active time, so you can jump between
  projects without losing context:
  - **New Chat** offers a choice: pick a project folder yourself, or open the
    **whiteboard** — an always-available scratch folder for quick, throwaway
    work, created next to this app's own install folder the first time you
    use it (e.g. `Theo\Make No Mistakes` → `Theo\whiteboard`). Settings has a
    **"Clear whiteboard"** button that empties it out (your chat history isn't
    affected).
  - Clicking a past chat reopens it exactly where you left off: same folder,
    same conversation, same task list.
  - **"New chat here"** (in Settings) starts fresh in the *same* folder — the
    old conversation stays in the sidebar, nothing is deleted.
  - Hover a chat to reveal a delete button. The app resumes your last chat
    automatically on launch.
- **Changeable background** — Settings → Appearance → *Change…* picks any image
  on your PC (Reset restores the bundled aurora). It's remembered across runs.
- **Permission sheets** — file edits show a color-coded diff before you Allow /
  Always allow / Deny (optionally telling the model why). The mode pill in the
  titlebar cycles ask → auto-edit → full auto.
- **Attachments** — attach any file with the paperclip (not just images).
  Nothing is read or sent anywhere automatically: the file is copied into an
  `uploads/` folder in the project (like `generated/` for the agent's own
  output) and the model gets a path reference, then decides for itself
  whether to `read_file` or `view_image` it.
- **Everything else** — live "thinking" stream (collapsible), expandable tool
  cards, task checklist, token counter (always $0.00), compact from Settings.
  First launch asks for your free z.ai key and stores it as the `ZAI_API_KEY`
  user environment variable.

## Using it

Just talk to it:

```
> find where user sessions expire and extend the timeout to 24h
> why does npm test fail?
> refactor utils.py: split the date helpers into dates.py
```

### Images (multimodal, terminal)

This section is about the terminal (`glm`). The desktop app's paperclip
button works differently (see Attachments above) — this is for mentioning an
image inline or with `/image`:

```
> here's the bug: C:\Users\me\Desktop\error.png what's wrong?
> /image mockup.png build this page in React
```

By default the free vision model produces an exhaustive analysis (exact text,
layout, colors) which is handed to the stronger coding model (`/vision
describe`). Use `/vision direct` to run image turns entirely on the vision
model instead.

The agent can also look at images on its own mid-task with `view_image` (e.g.
a screenshot it found in the repo), without you attaching anything.

### Local image generation

The agent can generate images itself — icons, placeholder art, banners, mockup
imagery — using [stabilityai/sd-turbo](https://huggingface.co/stabilityai/sd-turbo),
a small, fast Stable Diffusion model that runs **on your machine**, so there's
no API key or per-image cost.

The **first** time it's used, it installs some Python packages (`torch`,
`diffusers`, `transformers`, `accelerate`) and downloads the model weights —
a few GB total, one-time, needs network access. Every call after that runs
fully offline. In `ask` mode you'll get a permission prompt before this
happens, which tells you about the first-run download; it also runs faster
with an NVIDIA GPU, but works on CPU too (just slower).

Generated images are saved as PNGs (default: `generated/` in the project
folder) and shown inline in the chat automatically. The agent can also show
you any existing image file with `show_image`, without analyzing it.

### Browser preview

The agent can start a dev server itself with `run_background` (unlike
`run_powershell`, which blocks until a command exits, this keeps running so
it can be checked on later) and then actually look at the result with
`preview_page`: it loads a URL in headless Chromium and takes a screenshot,
shown inline in the chat, instead of just assuming a web UI change looks
right because the code compiled. The **first** use installs Playwright and
downloads Chromium (~150-300MB, one-time); every call after that runs fully
offline aside from loading the page itself.

### Plan mode

The checklist icon in the composer toggles **plan first**: your next message
becomes a read-only planning turn — the agent may explore the project all it
wants, but editing, writing and command tools are **hard-disabled by the
permission engine** (not just asked nicely), so nothing can change while you
haven't agreed to anything. It replies with a numbered plan; a bar appears
offering **Execute plan** (which seeds the task checklist and works through
the steps) or keep refining it by just replying. Plan messages carry a PLAN
badge in the chat, including after reload.

### Sub-agents

For work that splits into independent parts (research across several areas,
unrelated bugs, separate files), the agent can spawn up to 6 sub-agents that
run in parallel, each with its own mission, then reports back a summary from
each. Click any sub-agent's row in the chat to open its own live thread in a
slide-out panel on the right — the same reasoning/tool-call view you get for
the main agent, so you can see exactly what a specific sub-agent is doing.
Click another sub-agent's row (or its tab in the panel) to switch between
them, or the **✕** to close the panel.

**Steering** — while the main agent (or a sub-agent, from its own inspector
panel) is mid-turn, you can still send a message: the composer's send button
turns orange instead of disappearing. It doesn't interrupt whatever's running
— your message is delivered the next time a tool call finishes, right before
the model's next step, and a small note appears in the thread marking where
it was injected.

Each sub-agent's mini-composer also has a red **wrap-up** button — unlike a
hard stop, it doesn't cut the sub-agent off instantly. It finishes whatever
tool call is already in flight, then is forced to stop researching and write
its final report immediately instead of continuing, so you still get a
summary of what it found rather than nothing at all.

### Voice

Two independent features, both powered by [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)
running **locally** — no API key, no per-use cost:

- **Read replies aloud** — toggle the speaker icon in the title bar (or
  Settings). While on, every reply is spoken as it streams in, sentence by
  sentence; code blocks are never read aloud. The toggle is captured per
  message: turning it on or off mid-reply never changes what's already
  in flight, and TTS is never touched at all for a message sent while it's off.
- **`speak` tool** — the agent can generate and play a specific piece of
  speech on request (not for regular replies — that's the toggle above).
  Saved as a WAV (default: `generated/`) and played automatically.

Pick a voice and preview it in **Settings → Voice** (also lets you adjust
speed). The **first** use of either feature installs a small package
(`kokoro-onnx`) and downloads the Kokoro model (~300MB total, one-time,
needs network access); everything after that runs fully offline.

### Backups

Optional, per-chat, on by default: before each message runs, your project's
files are snapshotted to a hidden git repo (`~/.makenomistakes/backups/`) — separate
from any git repo the project already has, so it never touches your real
history, branches, or commits. Since it's just diffing the directory's
current state, it works no matter *how* something changed — a bad edit, a
destructive `run_powershell` command, whatever — as long as the change
stayed inside the project folder (installs/effects outside it, or already-
running processes, aren't something a file revert can undo).

Toggle it when starting a new chat, or later in **Settings → Backups**,
which also lists snapshots (by the message that triggered them) with a
**Revert** button, plus a one-click **Revert last turn**. Reverting only
changes files on disk — the chat conversation itself isn't affected.

### Change review

After every turn that touched files, a **Changes** card appears in the chat:
each file with an added/modified/deleted badge and an expandable colored
diff (against the automatic pre-turn snapshot), plus a per-file **Revert**
button — review the agent's work like a local pull request and undo just
the parts you don't want. When a newer turn runs, older cards retire their
buttons (Settings → Backups still reverts to any earlier point).

### Transcripts

Every chat's full conversation is also appended to a plain markdown
transcript (`~/.makenomistakes/transcripts/`, one file per chat) — including
everything that compaction later summarizes away. The agent is told where
these live, so it can grep them itself: ask about something from earlier
that's no longer in its context — or from a *previous chat entirely* — and
it looks it up instead of guessing. When a conversation is compacted, the
summary explicitly names the transcript file the details went to. Deleting
a chat deletes its transcript.

The sidebar's **search box** uses the same transcripts: it searches full
conversation text across every chat (not just titles), showing the matching
line under each hit — so "which chat was the ingress fix in?" is one search
away, even if that conversation was compacted long ago.

### Permission modes

| Mode | File edits | Shell commands |
|---|---|---|
| `ask` (default) | ask, with diff preview | ask |
| `autoedit` | auto | read-only auto, rest ask |
| `yolo` | auto | auto |

In every mode except `ask`, commands that only read state (`git status`,
`ls`, `cat`, `grep`, `Get-Content`, …) run without a prompt — the classifier
is strict, so anything that redirects to a file, substitutes a subcommand,
chains into an unrecognized stage, or passes a mutating subcommand/argument
still asks.

Switch with `/mode yolo` etc. When asked, answer `y` (once), `a` (always for
this session — for commands this allowlists the command prefix, e.g. `git`),
or `n` (deny, optionally telling the model why).

### Commands

`/help` `/mode` `/model` `/vision` `/image` `/think` `/reasoning` `/clear`
`/compact` `/cost` `/config` `/init` `/exit`

- `/init` explores your project and writes a `GLM.md` memory file that is
  auto-loaded into the system prompt in future sessions (it also honors an
  existing `AGENTS.md` or `CLAUDE.md`). That's per-project; for things about
  **you** that should apply everywhere, just tell it to remember them ("remember
  that I prefer 2-space indentation") — it's saved to `~/.makenomistakes/memory.md`
  and loaded into every chat, in every project, from then on. Say "forget
  that" / correct it and it'll edit the file directly.
- `/compact` summarizes long conversations. The agent sees a live "context
  usage" figure every turn and can proactively compact itself at a natural
  stopping point; there's also a hard automatic fallback if context grows
  past ~155K tokens regardless.
- `/cost` shows token usage — the price is always $0.00.
- Non-interactive mode: `glm -p "one-shot prompt"`.
- `Esc+Enter` inserts a newline; `Ctrl+C` interrupts the agent mid-turn.
- `Ctrl/Cmd+F` finds text in the open conversation — highlights every match
  and cycles through them with `Enter` / `Shift+Enter`.

### Web search

The agent has a `web_search` tool it uses to look up documentation, error
messages, and APIs it isn't sure about (then reads pages with `fetch_url`).
The default provider is **DuckDuckGo's HTML endpoint — no API key, no signup,
completely free**. Optionally you can upgrade to [Tavily](https://tavily.com)
(free tier: 1,000 searches/month, no credit card):

```powershell
setx TAVILY_API_KEY tvly-xxxxxxxx    # or: /config tavily_api_key tvly-xxx
```

With a Tavily key set it is used automatically (`search_provider` can force
`ddg` or `tavily`). Search results are always framed as untrusted data so the
model doesn't follow instructions embedded in web content.

## Bring your own model

The free z.ai models are the default, but any **OpenAI-compatible endpoint**
can power a chat. **Settings → APIs** shows every configured API as a
selectable list — click one to use it for the current chat (with an inline
model picker on the selected row), edit or delete it, or press **Add API…**
to configure a new one (OpenRouter, a paid API, anything with
`/chat/completions`). The very first time — before anything is configured —
the form comes pre-filled with the z.ai defaults so all you paste is the key,
which is saved to the `ZAI_API_KEY` environment variable. **Detect local**
auto-adds a running **Ollama** or **LM Studio** with its installed models.
There's also a **model selector in the title bar** — a slick dropdown that
lists every configured model as its own equal entry (the free z.ai model, an
OpenRouter model, each local Ollama/LM Studio model — no grouping), so
switching the current chat's model is one click. The choice is **per chat** —
pick a local Llama for one project while another stays on the free default —
and it's remembered with the chat.
Vision keeps routing through the built-in provider, so screenshots keep
working even when the chat's model can't see images.

## Notes on the free tier

- z.ai's free Flash tier is rate-limited (~1 request/second). Make No Mistakes
  automatically retries with backoff on rate-limit errors, so heavy agentic
  bursts just slow down rather than fail.
- Both models are also open-weight (MIT) on Hugging Face, so if the free API
  ever changes you can point `base_url` at any OpenAI-compatible local server
  (LM Studio, llama.cpp, vLLM) via `/config base_url http://localhost:1234/v1`.

## Layout

```
glmcode/
  api.py          z.ai client: SSE streaming, tool-call merging, retries, vision
  agent.py        the agentic loop (model <-> tools), compaction, image routing
  tools.py        read_file, write_file, edit_file, list_dir, glob, grep,
                  run_powershell, todo_write, web_search, fetch_url
  permissions.py  ask/autoedit/yolo modes, session allowlists, diff previews
  prompts.py      the system prompt, vision-analysis prompt, compaction prompt
  sessions.py     ~/.makenomistakes/sessions/*.json — chat history per project folder
  backup.py       ~/.makenomistakes/backups/ — per-chat shadow git repo, snapshot + revert
  events.py       frontend-agnostic event sink the agent reports through
  ui.py           rich terminal UI (streaming markdown, diffs, todo panel)
  cli.py          REPL, slash commands, image detection, first-run setup
  config.py       ~/.makenomistakes/config.json
  gui/            desktop app: pywebview shell (app.py) + HTML/CSS/JS (web/)
tests/            pytest suite (no network, no GUI deps): agent loop, retry/
                  backoff, steering, sub-agents, backups, sessions, memory
```

Run the tests with `pip install pytest` then `python -m pytest tests/ -q` —
they also run automatically on every push via GitHub Actions.

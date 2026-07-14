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
  traffic lights, smooth streaming markdown, dark cinematic backdrop.
- **Chat history, one per project** — like Claude Code / Codex, every chat is
  tied to a project folder and saved to disk (`~/.glmcode/sessions/`). The
  sidebar (toggle button next to the traffic lights) lists all your chats with
  a title, project folder and last-active time, so you can jump between
  projects without losing context:
  - **New Chat** always asks *you* to pick the project folder — nothing is
    auto-created or defaulted for you.
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
- **Images** — attach screenshots/mockups with the paperclip; thumbnails show in
  your message and the free vision model analyzes them for the coding model.
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

### Images (multimodal)

Attach any image by mentioning its path, or with `/image`:

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

### Permission modes

| Mode | File edits | Shell commands |
|---|---|---|
| `ask` (default) | ask, with diff preview | ask |
| `autoedit` | auto | ask |
| `yolo` | auto | auto |

Switch with `/mode yolo` etc. When asked, answer `y` (once), `a` (always for
this session — for commands this allowlists the command prefix, e.g. `git`),
or `n` (deny, optionally telling the model why).

### Commands

`/help` `/mode` `/model` `/vision` `/image` `/think` `/reasoning` `/clear`
`/compact` `/cost` `/config` `/init` `/exit`

- `/init` explores your project and writes a `GLM.md` memory file that is
  auto-loaded into the system prompt in future sessions (it also honors an
  existing `AGENTS.md` or `CLAUDE.md`).
- `/compact` summarizes long conversations; this also happens automatically
  when the context grows past ~110K tokens.
- `/cost` shows token usage — the price is always $0.00.
- Non-interactive mode: `glm -p "one-shot prompt"`.
- `Esc+Enter` inserts a newline; `Ctrl+C` interrupts the agent mid-turn.

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
  sessions.py     ~/.glmcode/sessions/*.json — chat history per project folder
  events.py       frontend-agnostic event sink the agent reports through
  ui.py           rich terminal UI (streaming markdown, diffs, todo panel)
  cli.py          REPL, slash commands, image detection, first-run setup
  config.py       ~/.glmcode/config.json
  gui/            desktop app: pywebview shell (app.py) + HTML/CSS/JS (web/)
```

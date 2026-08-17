# Working in this repo

## Merging

**Always merge your PR to `main` yourself unless explicitly told not to.** Do not
leave it open waiting for review. This has had to be said repeatedly; treat it as
standing instruction, not a per-task question.

## The phone app only ever sees `main`

`mobile/` is a PWA served from GitHub Pages, and `.github/workflows/pages.yml`
triggers on `push` to **`main`** only:

```yaml
on:
  push:
    branches: [main]
    paths: ["mobile/**", ".github/workflows/pages.yml"]
```

A push to a feature branch changes nothing on the device. If someone reports the
phone still showing an old build, check what `main` points at before suspecting
the service worker or iOS's cache — the usual answer is that the fix is sitting
on a branch that never deployed.

The publish job stamps the short SHA into `mobile/index.html` as `mnm-build`, and
the app shows it in Settings. That stamp is the source of truth for "which build
is this phone running", and it names a commit on `main`.

`gh-pages` is a build output: a fresh single-commit orphan branch, force-pushed
by that workflow. Never commit to it by hand. Publishing also depends on a repo
*setting* — Settings → Pages → Source must stay "Deploy from a branch" →
`gh-pages` / (root). Switching it to "GitHub Actions" silently ignores the
workflow.

## PRs are squash-merged, which matters when reverting

Each PR lands on `main` as one squashed commit, so the local commit and the one
on `main` are different objects with the same content.

To revert something already merged, **rebase onto `main` and revert the squashed
commit there** — do not stack a revert on top of the original local commit. A
branch that both applies a change and reverts it has a net-zero diff against its
merge base, and a squash merge of that branch computes exactly that diff: it
merges cleanly and deploys nothing.

```
git checkout -B <branch> origin/main
git revert --no-commit <squashed-sha-on-main>
```

Confirm with `git log --oneline origin/main..HEAD` that the branch carries only
the intended commit before pushing.

## The phone cannot work in the background, and never will

`runAgent` runs in the page and calls the model with `fetch` from the tab, so
the phone is doing the work. When iOS suspends the app the request in flight is
killed under it, `model.chat` throws, and the turn ends on its error path.

Nothing inside a PWA changes that. WebKit has never shipped Background Sync or
Background Fetch, and Web Push (iOS 16.4+, installed PWAs) delivers a
notification but cannot run anything. Do not go looking for a web API for this;
there isn't one. The only real answer for work that must continue while the app
is closed is to run it somewhere else — the desktop, via the handoff in
`session.pending`.

**This is a fact about the platform, not a rule about what to offer.** The
phone's voice mode used to be restricted to reading plus `needs_desktop`
*because* of the above, and that was the wrong inference: it meant asking for
work got a note left for the computer instead of the work. It now has the same
tools the desktop's voice mode has — the writing tools and the full
`dispatch_worker` / `check_workers` / `steer_worker` / `stop_worker` /
`worker_changes` / `revert_worker` set, mirrored from `CONVERSATIONAL_SCHEMAS`
and pinned by `tests/test_phone_workers.py`.

What the constraint actually requires is **honesty, not refusal**. A worker is a
floating promise in the page, so it progresses only while the app is open and
foregrounded, and a suspended tab kills the request in flight. So a worker
killed that way is reported as `interrupted`, with the reason said out loud —
never as `done`. Claiming work finished when it did not is the one outcome worse
than not offering to do it.

Two things about `dispatch_worker` on the phone are load-bearing:

- **It must not be awaited.** Live function calling is synchronous: the model is
  stopped until the tool returns, so a dispatch that waited for the work would
  hold the spoken conversation silent for the whole of it.
  `tests/mobile_ui/test_voice_workers.py` proves the return happens with the
  worker's first model call still open, and races every tool call against a
  timer so this regression fails instead of hanging the suite.
- **Its writes snapshot the previous content first** (`beforeWrite` in
  `makeTools`), because that is the only moment it is still knowable, and
  `revert_worker` needs it. Reverting re-reads the *current* sha rather than
  reusing a remembered one — GitHub rejects a write to an existing path without
  it, and re-reading also means a file changed since fails loudly instead of
  being silently clobbered.

Speech-to-speech does not change this and must not be described as if it
does. A Live session is a WebSocket opened by the page, so it is subject to
exactly the same suspension: iOS freezes the tab, the socket dies, and the
session is over. The phone ends it deliberately on `visibilitychange` rather
than letting it die silently — that is the whole of the difference. Work that
must continue while the app is closed still goes to the desktop, through
`session.pending`.

What *is* done, in `withRun`:

- A screen wake lock is held for the duration of a turn, so the phone does not
  lock itself while you wait. Covers the common case; does nothing for
  app-switching. Absent below iOS 16.4 and refused in Low Power Mode, so both
  must stay non-fatal.
- A turn cut off while hidden is resumed on the way back. `hiddenDuringRun` must
  be watched for the whole turn, not sampled at the start — the real shape is
  starting a turn and *then* leaving. Only a turn that was hidden and did not
  reach a terminal event counts: resuming a genuine foreground error would call
  the same endpoint and fail identically, forever.
- `session.interrupted` is set *before* the turn, not after. If the OS kills the
  app outright the `finally` never runs, and a flag written at the end would say
  the turn had never started.
- Resuming runs the saved history through `healInterruptedTurn`. A turn killed
  between a tool running and its result being recorded leaves `tool_calls` with
  no matching reply, which OpenAI-compatible APIs reject outright — so without
  it the resume fails on its first call and the chat is stuck for good.

## The desktop finishes turns the phone couldn't

Since the phone genuinely cannot work in the background, the machine that can
picks up after it. The phone marks the chat `interrupted` and publishes that in
the synced chat; the desktop scans for it on its 30-second timer
(`sync_finish_interrupted`) and finishes the turn with `pickup_note()`.

The whole design is about one failure: **two devices running the same turn.**
Both would call the model, both would commit, and each would end up holding a
history the other has never seen. Everything below exists to make that
impossible, so do not remove any of it as redundant.

- **A grace period** (`PICKUP_GRACE_MS`, 2 min). The phone resumes its own turn
  the instant it returns, so the desktop must not race it. Measured from the
  chat's `updated` stamp — the phone refreshes that when it saves on being
  backgrounded, so the clock starts when the phone went away.
- **The index is a cache, never the decision.** `pickup_candidates` shortlists
  from index rows; the chat body is re-read and re-checked before acting,
  because the phone may have come back in between.
- **The device lock is taken without `force`.** A phone that is awake is
  finishing its own turn; a stolen lock is exactly the bad case.
- **Chats already running here are skipped before the pull**, not after.
  `sync_pull_chat` re-activates the session from the store, so pulling over a
  live chat overwrites the messages the running agent is holding. The
  `turn_lock` check after the pull is too late to prevent that.
- **`interrupted` is always sent explicitly.** `SyncStore.save` merges, and an
  absent field means "nothing to say" — so omitting it would leave the last
  `True` standing and the chat would be picked up forever.
- **The phone awaits `refreshOpenChatFromSync` before resuming.** Otherwise it
  redoes a turn the desktop already finished.

The same rule now covers the Live session builder: `glmcode/live.py` and the
`live*` functions in `mobile/agent-core.js` must produce byte-identical setup
messages for the same inputs. Both devices open their own socket to the same
model with the same tools, so a difference is not cosmetic — it is one device
offering the model a tool the other cannot run. `tests/test_live.py` pins them
against each other by driving the phone's copy under Node.

`heal_interrupted_turn` exists in both `glmcode/syncstore.py` and
`mobile/agent-core.js` and the two must stay in step: each end adopts the
other's histories, and a turn killed between a tool running and its result
being recorded is unsendable until repaired. On the desktop it runs inside
`chat_to_session`, so every route in is covered — including the manual pull
button, which could always land such a history and fail on its first request.

## The pairing QR is read by a camera, and that constrains what goes in it

`glmcode/pairing.py` seals and `mobile/agent-core.js` opens, and the layout is a
version byte, two fixed-length fields and a compression scheme. Nothing in
either file can tell you whether the other agrees; a mismatch surfaces as "that
pairing link is damaged" on a phone with the working copy on the other machine.
`tests/test_pairing.py` drives the phone's copy under Node against a real
desktop seal, both directions.

Two things about that payload are not free to change:

- **It is deflated before encryption** (wire version 2). The payload is a
  photograph's worth of detail: a realistic one — two APIs with their model
  lists, a GitHub PAT, a sync passphrase — was a 113-module QR, and a camera has
  to resolve every module of it off a laptop screen. Compressed it is 73. Base
  URLs and model names repeat heavily; the keys are random and do not compress
  at all. Version 1 is still *read*, so a phone that updated ahead of its
  desktop can still pair. Adding a field to the payload spends this margin —
  `tests/test_pairing.py` and `tests/test_pair_qr.py` assert the module count
  for that reason.
- **The QR holds the bare token, with no URL around it.** It used to hold a
  link, and that was the bug behind "the code won't scan": the phone's *Camera*
  app read it perfectly, opened Safari, and paired Safari's storage — which for
  an app installed to the home screen is not the app's. The keys arrived
  somewhere that was not the app, and the app still had none. A bare token
  cannot be opened by anything, so scanning it with the wrong app does nothing,
  which is the better failure. The URL belongs in the install code, one step
  earlier in the same sheet.

The other half was the scanner. `getUserMedia` asked for no resolution at all
(browsers hand back 640×480) and the decode frame was capped at 720px, which is
about 1.7 pixels per module — unreadable by anything. It now asks for 1920×1080
and caps at 1440. **Keep a cap**: jsQR is plain JavaScript costing per pixel, so
a full 1920×1080 frame drops the scan rate to a crawl on the device this is for.

`tests/mobile_ui/test_scan_pairing.py` plays a real sealed QR through a fake
camera as an actual video track. What it cannot test is whether a real phone
reads it: canvas frames are perfectly sharp, square-on and noiseless, and jsQR
reads about two pixels per module from those while a lens does not. So the
margin is asserted where it is a fact — the pixels the decoder is handed —
rather than by trusting a synthetic decode.

## The sync passphrase is generated, never invented

It reads like a password and is not one. It is the key the chats are encrypted
under, and its only job is to be **identical on both devices** — nobody logs in
with it, and there is nothing to "remember". Asking a person to make one up
bought exactly two things: a chance to pick something weak, and a chance to
mistype it on the second device and fork the history into two halves that can
never read each other. On a phone keyboard, for a string that has to match
another machine exactly, that was the worst possible place to ask.

So `syncstore.make_passphrase()` and `makeSyncPassphrase()` in
`mobile/agent-core.js` generate one: four groups of five from the pairing code's
no-lookalike alphabet, 100 bits. Same shape on both, so a code made on either
can be copied to the other.

Three cases, and only one involves typing:

- **paired** — pairing already carries the key, so sync just works. This is the
  common path and it has no dialog in it at all.
- **nothing exists yet** — the device makes one. `open_sync` / `openSync`
  already report `created`, and `central_has_store` / `syncStoreExists` are what
  decide which case this is *before* generating.
- **a store already exists** — the key is decided and cannot be guessed, so the
  recovery code gets copied across. One field, no confirmation: it is a code
  being copied off another screen, not a secret being invented.

Never generate a key without checking for an existing store first. A fresh key
against one that exists cannot read a single chat in it, and surfaces as `Wrong
sync passphrase` — true, and useless, for a passphrase the user never chose.

The trade this makes: nobody types a passphrase, and nobody has one memorised
either. The only copies are the machine's credential store and any paired
phone. So **the code is shown, not hidden** — desktop Settings → Your phone →
"Show recovery code", and the same on the phone. Lose both devices without it
and the chats stay ciphertext forever. "Change passphrase" is gone; it was never
a useful thing to do, since a new key cannot read anything already uploaded.

## Everything the agent reads is data; only the conversation gives orders

`fetch_url` and `web_search` already said so. Nothing else did — which left the
two channels that matter most completely unmarked:

- **MCP tool output.** Third-party code, started from a command line the user
  pasted, whose text goes straight into context with no line numbers and no
  structure around it. A sentence in it reads exactly like a message. The note
  names the server, because "which server said this" is the first question when
  a result looks wrong.
- **The `@`-mention block**, which is appended to the *user's own message*.
  Anything arriving there sits in the most trusted position in the
  conversation, and the user pointed at the file without writing what is in it.
  The label goes **before** the content: a warning after ten thousand
  characters of attacker-controlled text has already been read in the wrong
  frame.

`read_file` is deliberately left alone. Its output is line-numbered
(`  12 | text`), which already frames it as file content rather than speech,
and it is the hottest tool in the app — the rule is stated once in the system
prompt, where it is always in context and costs nothing per call.

Two things worth keeping:

- **The note counts inside `MAX_TOOL_OUTPUT`, not on top of it.** Appending
  after the truncate put MCP results back over the cap — the one thing that cap
  exists to prevent, after an uncapped result once ballooned a chat to ~1.5M
  tokens. Room is reserved before truncating, so the total is what it was
  before the note existed. (`_truncate` also lands ~43 characters past the
  limit it is given; that is pre-existing and why the cap test carries slack.)
- **`UNTRUSTED_INPUT_RULE` is a named constant on both devices**, and
  `tests/test_untrusted_input.py` pins them word for word. This is how the gap
  was found: the desktop prompt was fixed and nothing failed, because the phone
  had no such rule at all. Both devices work on the same repository — the phone
  reads it over the GitHub API and every write it makes is a commit — so a rule
  that holds on only one of them is worth much less than it looks. Paraphrasing
  it on one side is a different instruction with nothing to say so.

## The shell tool is named after the platform, not after PowerShell

`run_command` execs PowerShell on Windows and bash (falling back to `/bin/sh`)
everywhere else. It was hard-coded to `powershell`, which meant it could not
**start** on macOS or Linux — every command raised "Failed to start
PowerShell" — while `prompts.py` was already telling the model it had a POSIX
shell there, and `run_check_command` eighty lines below had the platform branch
all along.

Two things about that are worth keeping in mind:

- **The name is part of the interface.** The tool description and the
  `Shell:` line in the system prompt are both generated from
  `tools.shell_name()`, so they cannot drift from what actually gets exec'd.
  Telling the model "PowerShell" on a Mac is how you get `Select-String` and
  `$env:` in a command that then fails for reasons the model cannot see. The
  same goes for bash-vs-`sh`: a model told "a POSIX shell" writes `[[ ]]` and
  arrays at some rate, and on Debian `/bin/sh` is dash, which rejects them with
  syntax errors that read as "the command was wrong".
- **`run_powershell` still resolves**, in `TOOL_FUNCTIONS` and in the
  permission engine's `SHELL_TOOL_NAMES`, because saved sessions replay tool
  calls by name and session allowlists are keyed on it. It is accepted from
  history, never offered to the model.

Why this survived so long is the more useful lesson. CI runs `ubuntu-latest`
and was green, because `tests/test_stop_command.py` substitutes a `FakePopen`
for the PowerShell that is "absent on Linux CI" — so the one platform CI
actually exercises was the one where the real tool could not start.
`tests/test_shell_platform.py` now runs a real command through the real tool,
mocking nothing, on whichever platform it finds itself on. **Do not let that
file grow mocks**; every other test of these tools already fakes the process,
and this one exists to be the one that doesn't.

The POSIX shell is started with `start_new_session=True` so its pid is also
its process-group id, and a stop signals the group. `proc.terminate()` reaches
only the shell, so a stopped `npm run dev` would leave the actual server
holding the port — invisibly, since the app believes it stopped it. That is
the same reach `taskkill /T` gives on Windows.

## Tests

The mobile keyboard/composer geometry is covered by
`tests/mobile_ui/test_composer_keyboard.py` (Playwright). Run it for any change
to `mobile/app.js` keyboard handling or the `--kb` rules in `mobile/style.css`:

```
python -m pytest tests/mobile_ui/test_composer_keyboard.py -q
```

It runs a desktop Chromium, so it verifies the arithmetic, not iOS. It cannot
see anything specific to an installed iOS PWA — the system form accessory bar,
or the web view being scrolled by the OS. Those need the device, and the
diagnostics panel in Settings is there to report them.

## iOS keyboard: what has already been settled on-device

Do not re-litigate these. Each cost a round trip to a real phone.

- `--kb` has two parts and they are not the same kind of thing. What
  `visualViewport` reports as hidden is measurable: on an iPhone 15 Pro, an
  852px screen, a 59px top inset and a 449px visible viewport give 344px hidden.
  The accessory bar is **not** in that number. It is a native view painted over
  the web view and does not shrink `visualViewport` at all, so room for it has to
  be added on top. Reading 344 as "the keys and the bar together" is what left
  the composer sitting behind the bar.
- Do not check that with `dock bottom == visual h`. That was used to confirm the
  reading above and it confirms the broken state: flush with the bottom of the
  visible area *is* underneath a bar painted across it. The only check that means
  anything is looking at the phone with the keyboard up.
- The bar's height is unmeasurable from the page, so it lives in `localStorage`
  as `mnm.kb.bar` and Settings → "Adjust composer height" puts − and + on the
  composer while the keyboard is up. Change the number there, on the device,
  rather than shipping a fourth guess.
- `window.innerHeight` tracks the *visual* viewport in an installed iOS PWA, so
  it shrinks with the keyboard. Measure against
  `document.documentElement.clientHeight`, or the lift comes out ~0.
- The focus flash — the whole UI translating and returning when a field is
  focused — is iOS scrolling the web view's own enclosing scroll view. It reports
  nothing to any web API (document scroll, visual viewport offset, and `<html>`
  height were all measured at zero while it was plainly visible), and nothing in
  the page can observe or undo it. See
  [ionic-team/capacitor#1366](https://github.com/ionic-team/capacitor/issues/1366),
  closed as not planned. The only fix is native — Capacitor's
  `Keyboard.setScroll({ isDisabled: true })` — and a native build has been ruled
  out for this project.
- Predicting the lift at `focusin` from a remembered keyboard height does not
  work and was reverted. Two reasons, both worth knowing before trying it again:
  a height remembered from every viewport report ends up holding a frame of the
  keyboard *sliding away*, not its settled size; and an already-open keyboard
  never resizes, so `visualViewport` never fires to correct the prediction and
  the composer stays under the accessory bar until blur.

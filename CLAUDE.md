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

## The runner is the only machine that is never off

`glmcode/ci.py` has been able to run the agent headless on a GitHub runner for
a long time — a `/agent` comment, work on a branch, a draft pull request, a
comment back with the link. All of it finished, and none of it reachable: the
workflow was a file in `docs/` you were told to copy into another repository by
hand, so the honest description of the feature was "you can do this yourself".

That gap is worth more than convenience. The phone hands work to the desktop,
and the desktop has to be awake; a runner does not.

- **The workflow is read from `docs/agent-workflow.yml`, never duplicated as a
  string.** The copy people install by hand and the copy the app installs would
  otherwise drift into two different workflows.
- **`workflow_dispatch` is gated by GitHub already** (it needs write access),
  which is why the job's `if:` lets it through unconditionally. The
  `issue_comment` path is NOT gated by GitHub and must keep checking
  `author_association` for itself — a stranger opening an issue must not be
  able to start a runner.
- **`MNM_TASK` reads `inputs.task || github.event.comment.body`.** Two trigger
  paths, one variable; a dispatch that left it empty would start a runner with
  nothing to do and say so ten minutes later.
- **The acknowledgement step is skipped on a dispatch run,** because reacting
  to `context.payload.comment.id` with no comment fails the step.
- **`workflow_status` reports "installed but out of date".** A workflow
  installed before `workflow_dispatch` existed cannot be started from the app,
  and saying nothing would make the button look broken.
- **The app cannot set the Actions secret** and says so rather than appearing
  to have finished — a feature that silently needs one more step is one that
  looks broken the first time it is used.

## Web Push is the one thing a suspended phone can still do

The desktop finishes turns the phone was suspended through, and for a long time
never told it — you found out by opening the app and looking. Web Push closes
exactly that: it cannot **run** anything on iOS (WebKit still has no Background
Sync or Background Fetch, and that section above stands), but it can deliver a
notification, and a notification was the whole missing piece.

**No server is introduced.** The desktop is the sender: `glmcode/webpush.py`
implements RFC 8291 (message encryption) and RFC 8292 (VAPID) against
`cryptography`, which is already a dependency. The subscription and the
desktop's VAPID public key travel in the encrypted sync store the two devices
already share (`devices/push.json`, `devices/vapid.json`).

Things that are load-bearing:

- **The RFC's own worked example is the test.** Hand-rolling push encryption is
  only defensible if it is checked against the specification rather than
  against one's reading of it, so `tests/test_webpush.py` runs RFC 8291 §5 —
  their keys, their salt, their plaintext, their exact output bytes. If that
  test ever fails, do not adjust it to match the code.
- **Salt and ephemeral key are random per message.** Reusing the pair against
  one subscription reuses the AES-GCM nonce, which leaks the plaintext. That is
  the worst mistake available here and there is a test for it.
- **The VAPID `aud` is the endpoint's ORIGIN,** not the endpoint. Sending the
  full URL earns a 401 that names no field.
- **The key goes through the sync store, not the pairing QR.** That payload is
  read by a camera and its module count decides whether scanning works at all.
- **`userVisibleOnly` is a promise, not a hint.** A worker that receives a push
  and shows nothing gets its subscription revoked, so `sw.js` raises a
  notification even for an empty or unreadable payload.
- **A 404/410 means forget the device; a 500 does not.** Dropping a
  subscription over a transient server error silences that phone for good.
- **The service worker's `CACHE` name must be bumped** whenever its handlers
  change (`tests/test_phone_icons.py` pins it, so the bump is deliberate). A
  phone on the old worker receives the push and does nothing with it.

## Values both devices need are generated, not restated

`scripts/gen_mobile_core.py` writes a marked block inside
`mobile/agent-core.js` from the Python that defines those values —
wire-format version bytes, PBKDF2 iterations, the sync repo and branch names,
device-lock timings, the Live sample rates, `CONVERSATIONAL_SCHEMAS`, and
`UNTRUSTED_INPUT_RULE`. Change the **Python** and run the script; editing the
block by hand only makes the phone disagree with the desktop.
`python scripts/gen_mobile_core.py --check` runs in CI and
`tests/test_generated_core.py` fails if the committed block is stale.

**Only data goes in the block.** The crypto, the agent loop and the sync store
stay hand-written on both sides, pinned by the node tests. Generating a
*function* across two languages is a different and much worse idea than
generating the numbers it operates on — a test asserts everything collected is
plain data, so this line does not quietly move.

**It is a block, not a separate file, on purpose.** The phone has no build step
and that is deliberate: a folder of static files with no toolchain to rot. A
generated `.js` of its own would need a script tag in `index.html`, an entry in
the service worker's precache list, and a module lookup that works under both a
plain `<script>` and `node --test` — three new ways to half-deploy a phone, to
save one file. The block needs none of them.

This does not retire the node parity tests and must not be read as doing so.
They cover the half that is still written twice.

## The Api class comes apart along subjects, not at a line count

`gui/app.py` held one `Api` class of 190 methods across ~3,600 lines, and every
feature landed in it. Two seams are taken so far, each a subject that shares
its whole vocabulary:

- `glmcode/gui/devices_api.py` — sync, pairing, Web Push, the CI runner:
  reaching a machine that is not this one.
- `glmcode/gui/voice_api.py` — speech-to-text, text-to-speech, and the spoken
  conversation: the delegator agent, its `<sid>::voice` event sink, the convo
  lock, and the two queues that carry work done by voice back to the coding
  agent.
- `glmcode/gui/github_api.py` — cloning and connecting a project, the token,
  the clone root, push and pull, and reviewing a pull request: everything that
  speaks `githubsync` about the chat's own repository.

All three follow the same rules.

- **A mixin, not a collaborator object.** These methods reach all over the
  instance (`self._cfg`, `self._chats`, `self._active`, `self._store`,
  `self._save_chat`). More importantly, **pywebview exposes the Api instance's
  public methods by inspection**: an inherited method is found exactly like a
  defined one, and a method moved onto a *collaborator* would not be — failing
  only at runtime, in the app, on the one path nobody re-tests.
- **The proof is the untouched tests.** The sync, push, CI and GitHub suites
  were not modified and still pass. A refactor that needed its tests rewritten
  would not have been behaviour-preserving.
- **Nothing is left behind.** A copy still defined on `Api` would shadow the
  mixin's, and which one wins depends on the MRO — `tests/test_api_split.py`
  fails if a name exists in both.

**The voice seam is one subject and must not be halved.** The app has two
speech engines and the choice is per-session: Gemini Live hears and speaks for
itself, while the local engine is Whisper plus Kokoro/Piper with this app in
between. `voice_mode` readies one, `live_voice_config` readies the other, and
`_persist_voice_turn` records the result identically whichever ran. Splitting
"the spoken conversation" from "the speech engines" would cut through the
middle of that rather than along a seam.

Cutting the voice seam is also what moved `WebEvents` out of `app.py`, into
`glmcode/gui/events.py`. It was never part of the `Api` class — it is the other
side of the bridge — and `_ensure_convo` builds a second one for the spoken
conversation, which a mixin in its own module cannot do without an import
cycle. Two things follow:

- **A lazy `from .app import X` inside a function is not the fix.** It works,
  and it is the seam leaking back: the cycle is still there, hidden until the
  next split. `tests/test_api_split.py` fails on that string.
- **`app.py` re-exports what it used to define** — `WebEvents`, `_TtsFeeder`,
  `_tts_engine_voice`, `_data_uri`, `_thumb_uri`. That is why
  `tests/test_webevents.py`, `tests/test_read_aloud.py`, `tests/test_background.py`
  and `tests/test_tts_engine.py` still import them from `glmcode.gui.app`, were
  not touched, and still pass.

`speech.py`, `media.py` and `paths.py` are leaves and must stay leaves: they
exist so that `events.py`, `voice_api.py` and `github_api.py` can each have
what they need without importing the other. An import back up the stack
recreates exactly the cycle they were carved out to break.

**Not every GitHub call belongs to the GitHub seam.** Chat sync, pairing and
the CI runner also talk to GitHub, and they stay in `devices_api.py`, because
their subject is reaching a machine that is not this one — GitHub is the
transport they happen to use, not what they are about. `sync_env` calling
`self._gh_token()` across that line is two mixins sharing one instance, which
is what a mixin is for, and `tests/test_api_split.py` pins it so nobody
"fixes" it later by duplicating the helper.

The same question moved `get_phone_app` and `get_pair_phone` in the other
direction: setting the phone up *is* reaching the other machine, and they had
been left in `app.py` by the first split. Expect a seam to correct the one
before it — that is the subject boundary being found, not churn.

**A dead-looking import may be a lookup path.** `pyflakes` called `pairing`
and `qrcode_util` unused in `app.py` once their methods moved, and removing
them broke four pairing tests: they monkeypatch `gui_app.qrcode_util.qr_svg`,
which needs the name to still resolve there. Re-exported with `# noqa: F401`,
same as `WebEvents` and the rest. The test failing is the good outcome —
check what reaches a name through `gui.app` before deleting it.

The next seams, when it is worth it, are the same shape: a subject with its own
vocabulary. Do not split by size.

## The history is a tree; rewinding is only one way down it

`backup.py` commits a snapshot of the work tree before **every** user turn, and
edit-and-resend already reverts both the conversation and the files to one. So
the structure underneath has always been a tree. It was offered as a line:
`rewind_to` truncates in place and the branch you were on is gone.

`fork_at` is the other move — the original chat is untouched, and a new one
opens holding the conversation up to that message with the files reverted to
the same snapshot. For an agent that is wrong a fair share of the time, "try it
both ways and compare" is a better primitive than undo, and it costs no extra
storage because those commits are already being written.

- **Up to, not including.** The fork starts where that turn was about to be
  sent, so its text is still yours to retype or change.
- **The files are reverted BEFORE the new chat is built,** so its own
  `BackupRepo` records the state the fork actually starts from rather than the
  parent's latest. A failed revert returns an error and creates nothing: half a
  fork — a new chat whose files belong to the other branch — is worse than none.
- **The inherited snapshots come with it,** so the new chat can be rewound or
  forked again. The tree keeps branching instead of flattening at the first
  fork.
- **Same turn ordinal as the edit path** (position among user bubbles), so the
  two cannot disagree about which message was meant.

## Setting a system prompt means `set_system_prompt`, not the attribute

`Agent.__init__` calls `rebuild_system_prompt()`, so `messages[0]` is written
before any caller can touch `_base_system_prompt` — and `messages[0]` is what
`_messages_for_call` sends. Assigning the attribute afterwards updates a cache
nothing re-reads: the change looks applied and is not.

That is how the Browser Agent ran with the **coding** agent's system prompt.
And since its goal lived only in that specialised prompt, the goal reached the
model nowhere at all — the user turn said "Begin. Work toward the goal", so the
conversation contained no goal to work toward. It answered by asking what the
task was, on every provider, which is why it looked like the Gemini sub-agent
bug and was not.

Two rules came out of it:

- **`set_system_prompt(text)` installs it**, into the cache *and* into
  `messages[0]`. Nothing should assign `_base_system_prompt` from outside.
- **A task goes in the TURN, not only in the system prompt.**
  `SUBAGENT_PREAMBLE` always did this; `BROWSER_AGENT_TASK` now does too. A
  system prompt is what the model *is*; the turn is what it was *asked*. Some
  providers treat the first as background, and a request whose conversation
  contains no request is answered with a question.

`tests/test_browser_agent_task.py` checks what reaches the wire rather than
what the code appears to set — the whole bug lived in that gap.

## A request ends on the turn, not on a note about it

The context-usage figure is sent at the END of the message list, not inside the
system prompt. That much is right and must stay: the figure changes every turn,
so putting it first gave every request a different prefix from the last, and a
prefix cache only matches an identical run of *leading* tokens. This app
re-sends ~12,400 tokens of system prompt and tool schemas on every request.

Dead last was wrong, though. A request must end on the user's turn or a tool
result. Google's OpenAI-compatibility layer does not treat a trailing `system`
message the way z.ai does, and sub-agents showed it at its worst: their first
request is `[system prompt, user(the mission), system(context usage)]`, so the
last thing the model read was a sentence about token budgets — and it answered
*that*, with the mission sitting directly above it. A coordinator handing out
three missions got back three sub-agents asking what the task was, on Gemini
only, which is exactly what makes it look like a model problem rather than a
message-ordering one.

The note goes **next-to-last** now. In any conversation long enough for the
cache to matter this leaves the same stable prefix, so nothing was given up.
`tests/test_request_ends_on_the_turn.py` pins both halves — that the request
ends on the turn, and that the prefix before the newest turn is unchanged
between requests.

## The live voice engine has to be wired IN, not alongside

Four separate desktop bugs turned out to be one mistake: `startLiveVoice` was
added next to the local engine rather than into it, so everything the local
engine owns was simply absent when the toggle said "Gemini Live".

- **Push-to-talk is enforced in `onaudioprocess`,** because that is the only
  path audio takes to the model. It was enforced nowhere: the mic streamed
  continuously whatever the toggle said, and the *button* ran the local
  recorder — so live + push-to-talk gave you a hands-free live session with a
  local transcribe-and-send bolted on top. `pttPress`/`pttRelease` branch on
  the engine now, and release sends `audioStreamEnd` to flush the turn (that
  pauses the input stream; sending audio again resumes it — it does not close
  the session).
- **`voice.speaking` is a LOCAL-engine flag.** The live engine schedules
  playback straight onto an AudioContext and sets nothing. Anything that must
  not talk over the assistant has to ask `liveSpeaking()` too, or it is right
  in one mode and wrong in the other — which is what let finished workers talk
  over the model.
- **Worker announcements go through the live session,** as a text turn into
  its own socket. Routing them to the local convo agent is what put two voices
  in the room at once, one of them reading from a conversation the live model
  had never seen.
- **`liveSpeaking()` alone is not enough to serialise announcements.** Audio
  takes a moment to come back, and it is false for that whole gap, so two
  workers finishing together both got through. `liveVoice.pendingTurn` covers
  the gap and is **time-boxed** (`LIVE_TURN_WAIT_MS`): `turnComplete` is the
  only thing that clears it, and a plain boolean would silence every later
  announcement for the rest of the session if that frame never arrived.
- **The transcript is the local engine's `.voice-you`/`.voice-it` blocks.** The
  live path assigned `textContent` on the whole caption element, which wiped
  every earlier turn — including the local engine's, so switching engines
  mid-session erased the conversation. It appends through the same helpers now.

## Talking and typing are one conversation

A spoken exchange used to reach the append-only transcript file and nothing
else. You could talk for ten minutes, close the overlay, and the chat window
still showed whatever had been typed before — and the coding agent, asked about
it by typing, had never heard any of it.

The phone did the right thing from the start: `voiceRecordTurn` pushes both
halves into `session.messages` and renders them as bubbles.
`_persist_voice_turn` now does the same on the desktop — transcript *and*
chat — and emits `voice_chat_turn` on the **coding** chat's sink, since the
voice sid drives the overlay, which is not where a chat message belongs.

- **The turn lock is tried, not taken.** Appending to `agent.messages`
  underneath a running turn is how you get a `tool_call` with no matching
  reply. An idle chat is written to and saved immediately, so it survives a
  reload; a busy one queues, and `_drain_voice_turns` picks it up at the top of
  the next turn — the same shape as the worker reports beside it.
- **The live engine passes its transcript in.** Gemini transcribes both
  directions and hands them over, so `reply=` is explicit rather than dug back
  out of the delegator's history — which is a copy at best, and absent
  entirely once voice mode closes and `convo_agent` is dropped.

Worth knowing about **whose transcription this is**, because it is asked: with
the live engine both devices already use Gemini's own `inputTranscription` /
`outputTranscription`. There is nothing to switch. With the local engine there
is no Gemini in the loop at all — Whisper hears, the delegator answers — so the
question does not arise there either.

## A worker's report outlives the voice session

Work dispatched by voice used to report back only to the delegator, which is a
separate voice-only agent. Close the overlay, type "what did that change?", and
the agent you are typing to had never been told anything happened.

`worker_report_note()` is filed in two places: the delegator's history, so a
spoken follow-up can be answered, and `ChatState.worker_reports`, drained into
the coding agent at the top of its next turn.

**Queued, not appended.** A worker finishes on its own daemon thread at a
moment nothing controls, and the coding agent may be mid-turn — writing into
`agent.messages` underneath it produces a `tool_call` with no matching reply,
which OpenAI-compatible APIs reject outright. `_drain_worker_reports` runs
inside `_run_send_turn`, under the turn lock, which is the one moment that
history is not being written by anyone else. The queue is bounded, because
every entry is spent from the coding agent's context the moment it next runs.

The note carries `WORKER_REPORT_PREFIX` so `sessions.to_display` keeps it out
of the rendered chat: it arrives in the `user` role (the role this app injects
plumbing under) and nothing else would distinguish it from something typed.

## The reasons are in git, and the agent could not reach them

This repository writes down *why*. Nearly every non-obvious decision carries the
failure that produced it — in the comment above the code, and in the commit
body, which for a squash-merged PR is the whole PR description. `CLAUDE.md`
opens a section with "Do not re-litigate these. Each cost a round trip to a real
phone."

The agent read none of it. It reads **code**, which is the one artefact that
cannot say what was tried and reverted: a line that came back looks identical to
a line never touched. So it proposes the thing that was measured on a device and
undone, and the only defence is that a human remembers.

`why(path, line)` is the fix, and it is mostly a reading of `git log -L`.

- **It answers even when git cannot.** The comment block is computed and
  returned first; a file outside a repository, or one never committed, still
  gets what the code says about itself, with a sentence explaining the absence.
  A tool that returns nothing for an uncommitted file is useless exactly when
  the agent has just written it.
- **A blank line ends a comment run, and indentation gates the enclosing
  block.** A comment separated from its code is usually about something else,
  and the nearest `def` *above* a line is frequently the one that already ended
  — a comment between two functions belongs to the one below. Handing back a
  neighbour's docstring reads exactly like an answer, which is worse than
  returning nothing, so both heuristics fail closed.
- **The docstring is not at `def + 1`.** Signatures here wrap routinely, so the
  scan walks to the end of the header (parens balanced, line ending in a colon)
  before looking. In this codebase the reason sits in the docstring at least as
  often as in a comment above it; looking only upwards finds half of it.
- **`_git()` takes an argv list, not a shell string.** The other git helpers
  interpolate into a shell command, which is fine for the paths they take.
  `git log -L` takes a `start,end:path` argument with a colon in it and a path
  that may hold spaces — quoting that for two different shells is a worse
  problem than using neither.
- **Trailers are stripped.** `Co-Authored-By` is plumbing, not a reason, and it
  costs context on every call.
- **The output ends by saying what it is.** Without the closing line it reads as
  a changelog, and a changelog is something you skim rather than act on. A
  commit that says an approach was tried and reverted is an instruction.

The system prompt names the trigger, because a tool the model never thinks to
call is not a feature: reach for it when a line looks *unnecessary* — a magic
constant, a workaround, a retry, an ordering that looks arbitrary, a guard that
looks redundant.

## A rate limit should slow you down, not stop you

The free tiers this app is built around are metered in requests per day and per
minute. Hitting one does not make you a worse programmer; it just stops you.
`model_fallbacks` is an ordered chain — "3.6 flash, then 3.5 flash, then 3.5
flash lite" — walked when the preferred model is refusing for quota.

Two things it costs, and both are worth paying:

- **The prompt cache.** This app re-sends ~12,400 tokens of system prompt and
  tool schemas on every request, and a different model has no cache for that
  prefix. But the alternative is not "keep the cache" — it is "get nothing",
  because the preferred model is refusing.
- **Quality.** A weaker model is worse at tool calling, and switching mid-task
  is where that shows.

The conversation itself is unaffected: history is plain OpenAI-format messages
and this app already switches models per chat.

What keeps the cost bounded:

- **Only a 429 walks the chain.** A weaker model is not the answer to a server
  fault or a bad request, and falling back on any error would quietly answer
  every hiccup with a worse model.
- **Switching does not sit out the backoff.** The wait is the thing the chain
  exists to avoid; sleeping the full `retry-after` and then switching gives up
  the entire benefit.
- **A rate-limited model is skipped for `MODEL_COOLDOWN`, then tried again.**
  Asking one that just refused for quota spends a request to be told the same
  thing — but a per-minute limit recovers in a minute, and being exiled to the
  weakest model for the rest of the session is the failure this must not have.
- **If the whole chain is cooling down, it asks the preferred model anyway.**
  Waiting beats refusing outright.
- **Cooldowns are per endpoint.** The same model name on another provider is a
  different quota.
- **Only the main turn gets the chain.** Sub-agents and compaction keep the
  plain path: the main turn is the long, tool-calling one a rate limit actually
  kills, and it is the one whose model the user chose.
- **The switch is said out loud, once.** A silent downgrade is the worst
  version of this — the chat quietly gets worse at tool calling and nothing
  anywhere explains why. Once per switch, not per request: news the first time,
  noise after that.

Not done, and deliberately: no per-model context-limit check. Model context
windows differ, and a fallback with a smaller one can fail on a long chat — but
inventing a table of limits would be a guess that goes stale, and the failure
is loud rather than silent.

## Updating is a pull and a restart, and both halves fail quietly

The app is installed by cloning it, so the Update button in Settings → General
runs `git pull` and starts a fresh copy. Every part of that has a failure mode
invisible from a button, and a button that half-works leaves someone's app
directory in a state they did not ask for and cannot see.

- **Two steps, never one.** `check()` looks and changes nothing; `pull()` only
  runs after a clean check. A single-click "update" would do all of it to
  someone who wanted to know whether there *was* one.
- **Every refusal names the actual state** — local edits, detached HEAD, no
  upstream, not a git checkout at all. "Couldn't update" leaves a button that
  does not work and no way to find out why.
- **`--ff-only`.** A merge commit created by a button in someone's app
  directory is not something they asked for, and a *conflicted* merge leaves
  the app in a state it cannot run from. Refusing is recoverable; half-merging
  is not.
- **Local changes stop it.** The person editing their own copy is exactly the
  person most likely to press this.
- **The restart is spawned BEFORE the window closes,** and detached. Close-then-
  start leaves a gap where the app is simply gone, and a failure in that gap is
  indistinguishable from the update having quit the app for good. On Windows a
  child in the same console group dies with its parent, so `DETACHED_PROCESS`
  is not optional.
- **`sys.executable`, not `"python"`.** On Windows that may be a different
  install, or absent from PATH.
- **A running turn refuses the update.** Restarting mid-turn loses whatever the
  agent was part-way through, and an update is never that urgent.

## Reaching the user's own browser means going in through the side

`control_chrome` launches its own Chromium by default and that is unchanged: a
throwaway profile, or the dedicated agent profile behind "Remember browser
logins", and nothing it does can touch the browser the user is signed into.

For driving the browser they *already have open*, there are two routes and only
one of them is usable.

**`browser_connect_url` (the DevTools port) cannot reach a running browser.**
The port only opens at launch — there is no way to switch it on afterwards — so
the honest description of that feature was always "quit your browser and reopen
it with these flags", and current Chrome additionally refuses the port on the
default profile directory. It is kept, behind an Advanced disclosure, because
it gives real browser-level input events. It is not the way in.

**The extension is.** It is already inside the browser: same profile, same
logins, same tabs, no flags and no relaunch. So the direction of the connection
flips — the app stops trying to reach into the browser, and the browser dials
the app.

- **WebSocket, not a fetch loop.** An MV3 service worker is killed after 30
  seconds idle, and WebSocket activity is the documented thing that resets that
  timer. A poll loop would have the worker dying underneath it every half
  minute.
- **The server is hand-rolled, and `Origin` is the entire security boundary.** A
  WebSocket is not subject to CORS, so any page in any tab can open a socket to
  `127.0.0.1` and start issuing commands. The browser sets `Origin` and page
  JavaScript cannot forge it, so only `chrome-extension://` is accepted; the
  socket is bound to loopback so nothing off the machine reaches it at all.
  `tests/test_extension_bridge.py` drives a real client over a real socket,
  including the RFC 6455 §1.3 worked example for the handshake.
- **`stop()` joins the accept thread before closing.** Closing a listening
  socket while another thread sits in `accept()` does *not* release the port —
  the close waits on the blocked call, which never returns. Without the join a
  stopped bridge holds its port for the life of the process, and the next one
  silently walks down to the next port in the list. This was found by a test
  that started and stopped eight bridges.
- **The extension gets a Playwright-Page-shaped object, not a second set of
  ops.** `ExtensionPage` implements the dozen calls `_op_*` actually makes, so
  the snapshot, the stable refs, the region grouping, the error messages and the
  Browser Agent's whole prompt are untouched. Teaching every op a second way to
  work would have been two subtly different pages described to one model.
- **The snapshot is GENERATED into the extension** (`scripts/gen_extension_page.py`).
  MV3 forbids `unsafe-eval`, so an extension cannot run a JavaScript string it
  was handed — the snapshot has to *be* code inside it, and a second hand-written
  copy would drift from the one Playwright gets.
- **Clicks are a full event sequence, and fills go through the native value
  setter.** `el.click()` skips the pointer and mouse events frameworks actually
  listen on, and `el.value = x` is ignored by React, which tracks its own
  last-rendered value and concludes nothing changed. Both show up as "the page
  did nothing".
- **`is_attached` is true for both routes**, because everything keying off it
  cares only whose browser this is. The Browser Agent gets
  `BROWSER_ATTACHED_NOTE` either way.

**The install is the feature.** It cannot be automated away — Chrome has no API
for loading an unpacked extension and the Web Store is not a route this project
can take — so the only thing left is to remove every step around it:

- **Browsers are found and named** (`installed_browsers.py`), and each gets a
  button that opens its own extensions page. "Open chrome://extensions" assumes
  one browser and assumes it is Chrome; someone who lives in Edge reads that,
  installs it in the wrong browser, and wonders why nothing connects. Passing
  the `chrome://` URL on the command line is the only way to reach that page
  from outside the browser, and a running browser answers it with a new tab.
- **The port opens while the panel is on screen, before the switch is on**
  (`status(cfg, listen=True)`). It used to open only once the feature was
  enabled, so the sheet said "Waiting for the extension…" forever to anyone who
  had not flipped the switch first — there was nothing to wait on. Verifying
  the install *before* handing over a logged-in browser is also the right order.
- **The sheet carries the switch.** Finishing the instructions and then having
  to find the control you were sent away from is where people stop.
- **Connected-but-off is called out as nearly done**, not shown as an error.
  It is the state everyone lands in after installing.
- **The panel names the tab it would act on.** "My own browser" is otherwise a
  leap of faith taken at the moment the agent starts clicking things.

**An open socket is not evidence of a live browser.** `connected` used to mean
"a file descriptor exists", which produced the worst version of this feature:
Settings said *Connected* while every browser action sat for the full timeout
and then failed. A laptop that slept, a browser that was killed, a FIN that
never arrived — all leave a socket that reads as fine and answers nothing. The
bridge pings on a heartbeat and `connected` asks when the extension was last
heard from; a stale client is dropped and anything waiting on it fails at once,
naming the reason. Browsers answer a WebSocket ping at the protocol level,
without the service worker being woken, so this stays true exactly when it must
— while Chrome has the worker asleep.

The heartbeat **ticks finely and decides from elapsed time** rather than
sleeping for the whole interval: a loop parked in an eight-second wait cannot
notice a shutdown, and a test that shortens the timings finds a loop that never
re-reads them.

**`my_tabs` is on the CODING agent, not just inside the Browser Agent.** Asked
"what tabs do I have open?", the model said it had no access — true, since
`control_chrome` was its only browser tool and nothing said the user's own tabs
were reachable. A question does not deserve a sub-agent either. It is offered
unconditionally rather than only when connected, so the answer is "your browser
isn't connected, here is why" instead of "I can't": a tool the model does not
know exists cannot tell it anything.

**Three ways this fails in silence, all of them found by one bug report.** The
symptom was a second Chrome window opening with a blank tab, which says nothing
about any of the causes:

- **An MV3 service worker is killed after 30 seconds idle, and a pending
  `setTimeout` does not survive it.** The reconnect loop was a `setTimeout`, so
  the ordinary install order — load the extension while the app is not yet
  listening — burned a few retries, got terminated, and never tried again.
  `chrome.alarms` is the one timer that outlives the worker; `tabs.onActivated`
  and `windows.onFocusChanged` cover the gap below the alarm's 30-second floor,
  since browsing wakes the worker anyway. `connect()` must therefore stay a
  no-op when a socket already exists, or browsing sprays connections.
- **The port was opened lazily, by the Settings panel's status call.** So a
  normal launch — app starts, setting already on, nobody opens Settings — left
  nothing to connect to. `boot()` opens it now when the setting is on, and only
  then.
- **The fallback was silent.** With the setting on and nothing connected,
  `control_chrome` quietly launched a separate browser. It warns now
  (`not_connected_hint()`), naming the port and what to check, because the
  feature looked broken rather than off.

**An open socket is not evidence of a live browser.** `connected` used to mean
"a file descriptor exists", which produced the worst version of this feature:
Settings said *Connected* while every browser action sat for the full timeout
and then failed. A laptop that slept, a browser that was killed, a FIN that
never arrived — all leave a socket that reads as fine and answers nothing. The
bridge pings on a heartbeat and `connected` asks when the extension was last
heard from; a stale client is dropped and anything waiting on it fails at once,
naming the reason. Browsers answer a WebSocket ping at the protocol level,
without the service worker being woken, so this stays true exactly when it must
— while Chrome has the worker asleep.

The heartbeat **ticks finely and decides from elapsed time** rather than
sleeping for the whole interval: a loop parked in an eight-second wait cannot
notice a shutdown, and a test that shortens the timings finds a loop that never
re-reads them.

**A blink is not a failure.** The socket goes away for ordinary reasons — Chrome
recycling the service worker, an extension reload, a browser restart — each a
gap well under a second. A call arriving inside one waits for the reconnect and
retries on the new socket, because a task dying because the transport blinked is
not something the model or the user can act on. Only READS are repeated
(`SAFE_TO_REPEAT`): a click whose connection dropped might or might not have
happened, and a form submitted twice is not something a later snapshot undoes.

**`SO_REUSEADDR` means two different things.** On Unix it only permits rebinding
a port left in `TIME_WAIT`. On **Windows** it permits binding a port that is
already in ACTIVE USE, and which socket then receives connections is undefined —
so a second copy of the app can take the port from under a running bridge, and
the extension starts getting `ERR_CONNECTION_REFUSED` while the first app still
believes it owns the socket. `SO_EXCLUSIVEADDRUSE` is the Windows option that
means what `SO_REUSEADDR` means everywhere else.

**The connection log is in Settings because nothing else could settle it.**
Every report of this feature came down to "it said connected but wasn't", and
neither end could say when it went or why: Chrome's extensions page timestamps
its errors too vaguely, and the app was throwing the information away. The
bridge keeps the last dozen connect/drop events with reasons and real
timestamps, and `boot()` no longer swallows the one failure the user cannot see
from outside — a port that never opened.

**The whole feature is: open some tabs, open the app, ask for something, and it
works.** Anything the user has to do beyond installing the extension once is a
bug in this feature, and it has had two.

**There is no second opt-in.** `browser_own` defaults to `"auto"`, meaning the
extension is used whenever it is connected. Loading an unpacked extension into
your own browser is already a deliberate, several-step act — requiring a switch
afterwards meant people finished the hard part and found nothing worked. It is
a NEW config field rather than a flipped default on the old one, because the
old one defaulted to `False` and a persisted `False` cannot be told apart from
a choice. `"off"` is the way out, and it closes the port.

The cost is that the port is open by default. That is the price of the
extension ever being able to reach anything on first install, and it is
loopback-bound and `Origin`-gated (above).

**The agent chooses a tab; it does not inherit one.** Driving "the active tab"
silently meant whatever happened to be in front, and it followed the user
around as they switched tabs. `browser_tabs` / `browser_switch_tab` /
`browser_new_tab` are added to the Browser Agent's schemas **only when
`supports_tabs`**, since a launched browser holds the single page this app made
it and three tools that always answer with the current page are noise in the
longest prompt in the app.

- **Their open tabs are the workspace, and the prompt says so.** A first
  version told the model to *prefer a new tab* — reasoning about not hijacking
  the page someone is reading — and that fights the actual use case head on:
  "do something in those tabs" is the normal request. The rule is now: list the
  tabs, work in the one the goal is about, and open a new tab only when the
  goal needs a page that is not open yet.
- **A selected tab is pinned in the extension** (`pinnedTabId`), so a long task
  stays where it was put. Dropped when that tab closes.
- **Switching brings the tab to the front.** The user should be able to see
  where the agent is working, and a background tab throttles timers and
  rendering in ways that make a page behave differently.
- **`supports_tabs` is answered from the backend, not the page.** The page only
  exists after `start()`, and a property that said False until then would drop
  the tab tools for anyone who asked in the wrong order.
- **The fallback warning is skipped when `browser_connect_url` is set.**
  Someone who deliberately configured the DevTools route has not asked about
  the extension.

**Nothing can open another program's `chrome://` page.** Chrome refuses those
URLs on the command line (they were a malware vector), a web page cannot link
to them, and there is no API. Passing one anyway is *worse than doing nothing*:
the browser drops the URL and opens an empty window — which is exactly what a
button labelled "Open in Chrome" appeared to do, and it read as the whole
feature misfiring. The address is copied for pasting instead, the panel says
why, and the per-browser button only brings that browser to the front (with no
argument, a running browser is focused rather than handed a blank window).

What the extension route gives up: input is dispatched as page events, so a site
that checks `event.isTrusted` (bot detection on sign-in pages, mostly) will
refuse it. The launched browser uses real browser-level input and remains the
better tool there. Say so rather than letting it look broken.

## The browser agent is a worker, not a blocking call

`control_chrome` ran the Browser Agent inline and the whole conversation waited
on it. That is the wrong shape for the one thing the user is most likely to be
*watching*: they could not ask about it, could not redirect it, and could not
get on with anything else. Steering existed, but only through the sub-agent
panel — the agent they were talking to was frozen.

It is a background worker now (`background=true`), reporting through the same
registry as every other one, so `check_workers` / `steer_worker` / `stop_worker`
apply to it unchanged. Voice reaches the same thing with
`dispatch_worker(kind="browser")`, which is the shape that was asked for: watch
the browser, talk to the assistant, have it steered mid-flight.

- **The coding agent had NO worker tools.** Not an oversight to fix later —
  it is *why* a background browser was impossible. Everything it could delegate
  blocked until it finished, so there was never anything running to ask about.
  `WORKER_SCHEMAS` gives it the same three the spoken side has.
- **Blocking stays the default.** A quick look whose answer is needed to
  continue is a worse conversation as a worker, not a better one.
- **The browser is opened before the thread starts.** "The browser would not
  open" is an answer the model can act on; raised on the worker thread it would
  land in a report nobody is waiting for.
- **Workers are named from their goal** (`open-dashboard-check-error`).
  `steer_worker` takes a name as well as an id, and "wk3" is not something
  anyone says out loud — least of all in voice mode, where this matters most.
- **The phone shares the schema and has no browser.** `kind="browser"` there
  returns a refusal naming `needs_desktop`. A shared schema that silently did
  something else would have the model report browsing it never did — which is
  exactly the failure the parity tests exist to prevent.

## The agent's browser and the user's browser are two different things

`control_chrome` launches its own Chromium — a throwaway profile, or the
dedicated agent profile behind "Remember browser logins". Nothing it does can
touch the browser the user is signed into, and **that stays the default**.

`browser_connect_url` is the opt-in opposite: attach over CDP to a browser the
user is already running and drive the window in front of them, as them. It is
strictly more dangerous and exists because the user asked for the choice — so
the job is to make it honest, not to make it safe.

- **The port cannot be opened on a running browser.** There is no way to switch
  DevTools on after the fact, so attaching always means quitting the browser and
  reopening it with `--remote-debugging-port`. Current Chrome also refuses the
  port when it would use the default profile directory, so `--user-data-dir` is
  not optional — pointed at a copy of the profile is how logins come along.
  `DEBUG_PORT_HINT` carries the per-platform command and is returned from the
  failure path *and* the check, because "nothing is listening" and "here is what
  makes something listen" are one conversation.
- **Teardown disconnects and nothing else.** `context.close()` or
  `browser.close()` reaches across the connection and shuts the user's own
  windows. `_real_attach`'s teardown is `pw.stop()` alone, and
  `tests/test_browser_attach.py` asserts it against the real function rather
  than a fake of it — that is the test that catches someone tidying it up later.
  A tab opened because the browser had none is left behind for the same reason.
- **The tab is chosen by `document.visibilityState`, not by index.** Playwright
  has no active-tab concept and `context.pages` is creation order, so `pages[0]`
  is whatever they opened first. A foreground tab reports `visible` and every
  background tab reports `hidden` — the page telling us itself. The search runs
  across every context, since a second (or incognito) window is a separate one.
- **The viewport is re-read from the real window.** Every snapshot header quotes
  `self.viewport` and `browser_click_at` *refuses* coordinates outside it, so a
  stale 1280×800 would both describe the page wrongly and then reject the right
  click for being out of bounds. Launched sessions ask for their size and are
  unaffected; only the attach path syncs.
- **The endpoint must be localhost.** Not a security boundary — the debugging
  port is unauthenticated, so anything that can reach it has already won — but a
  typo that aimed this at another machine would point the agent at someone
  else's signed-in browser. `_normalize_connect_url` refuses out loud and stores
  nothing; a setting that silently ignored a bad value would look broken.
- **`BROWSER_ATTACHED_NOTE` goes in the SYSTEM prompt.** Not the turn: it is
  what the model *is* this time round and has to hold over every improvised
  action, not just the first. A model that believes it is in a scratch profile
  will click "Sign out" to get a clean login form, or empty a cart to start
  over — reasonable in a sandbox, destructive in the window someone lives in.
  It is appended *after* `.format()`, so it is free to contain braces.

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

## The phone commits as it edits, so the branch is decided first

There is no filesystem on the phone. `write_file` and `edit_file` are commits,
made the moment the model calls them, to whichever branch the chat is bound to
— and that was always the repository's **default** branch, because opening a
repo is the only way the phone gets one and it opens the default. So work done
from a phone went straight to `main`, unreviewed, and on this repo it deployed
on the way past.

`new_branch` and `open_pull_request` close that. Four things about them are
load-bearing:

- **Switching branches rebuilds the tools, it does not re-point them.**
  `makeTools` keeps a `path -> {text, sha}` cache, and a cache carried across a
  branch switch is the worst available kind of wrong: reads *succeed* and hand
  back the other branch's text. `bindBranch` builds a new `makeGitHub` client
  and a new tool set from it, which is what drops the cache.
- **It is not `connectRepo`.** Connecting a repo resets the chat — messages,
  transcript, the queue of work parked for the desktop. None of that belongs to
  the branch, so the two were split rather than one calling the other with a
  flag.
- **The switch is persisted immediately**, not at the end of the turn. The
  branch is where every subsequent commit goes; a relaunch that came back on
  the old one would carry on writing to it.
- **Sub-agents get neither tool.** One of them moving the chat onto another
  branch underneath the agent that dispatched it is not something that
  conversation could recover from.

The system prompt states the rule the tools exist for — *every edit is
committed immediately, so branch first* — because a tool the model never
thinks to call is not a feature.

## "Did CI pass" is a different question from "do the tests pass"

Both devices can now ask it, for different reasons and by separate routes.

On the phone it closes the loop `new_branch` and `open_pull_request` opened:
nothing runs there, so *did the tests pass* was the one step in **branch → edit
→ commit → pull request** that still had to be answered somewhere else. It is
also the most phone-shaped question in the app — you push, you walk away, and
you want to know ten minutes later without going back to a desk.

On the desktop the shell is not the answer either. It can run the tests
*here*; it cannot run the ones on a **runner**, and those are what gate a
merge. Asked "did CI pass" the agent could only say to go and look — the same
answer it gave before it could push at all.

`glmcode/tools.py:check_ci` and the `check_ci` in `mobile/agent-core.js` are
two implementations of one shape. The rules are the part that must not drift,
because a model that learns them on one device carries them to the other:

- **`check-runs`, not the older `statuses` endpoint.** Actions reports through
  checks, and a repository using both would otherwise show half its answer.
- **`skipped` and `neutral` are passes.** A conditional job that did not need
  to run is not a broken build, and calling it one makes this cry wolf on every
  repository that has one. `tests/test_check_ci.py` pins the two lists against
  each other.
- **A run still going is never reported as a pass.** "1 passed, 0 failed, 1
  still running" is the honest shape of a half-finished answer; collapsing it
  into green is how someone merges on the strength of a job that had not
  started.
- **"Nothing reported" covers both reasons.** "Not started yet" and "this repo
  has no CI configured" look identical from here, and saying only the first
  sends someone off to wait for a run that is never coming.
- **A check's own `output.summary`, and never a log.** A job log is served as a
  redirect to blob storage — tens of megabytes, and unreadable cross-origin
  from a page at all — so promising one would be a tool that fails on exactly
  the case it exists for. The summary is capped: one is written for a web page
  and can run to thousands of lines.

The desktop adds the one thing the phone cannot know: **whether what is on the
runner is what is on this disk.** It counts unpushed commits and says so, since
a green answer about a commit two pushes ago is the most confidently wrong
output this tool could produce.

## What the phone cannot borrow from the desktop, and why

Three tools stayed behind, and the reasons are different in each case. They are
written down so the question is not reopened by inspection of the tool lists.

- **`web_search` and `fetch_url` are blocked by CORS, not by the CSP.** The
  desktop scrapes DuckDuckGo HTML; a page cannot read a response from a host
  that sends no `Access-Control-Allow-Origin`, and no `connect-src` entry
  changes that. Reaching them needs a proxy, which is a server, which this
  project does not have.
- **`why` reads the FILE's history, not the line's.** `git log -L a,b:file` has
  no API equivalent — GitHub filters commits by path and no finer. The phone's
  `why` says so in its own output, naming the line it could not narrow to,
  because implying a precision it does not have would hand back a confident
  wrong reason.
- **`remember` writes somewhere else.** The desktop's memory lives outside the
  repo (`~/.makenomistakes/memory.md`); everything the phone can write is a
  commit. So its notes go into the project's own agent file, which the desktop
  already reads into its system prompt. **Which** file matters:
  `prompts._project_memory` returns the FIRST of `GLM.md` / `AGENTS.md` /
  `CLAUDE.md` that exists, so creating `GLM.md` in a repo that has a `CLAUDE.md`
  would silently shadow it and the project's real instructions would stop being
  read. It appends to whichever is already there, and only invents a name when
  there is none.

Two things that ARE shared and must stay shared: argument names, and the
trigger sentences in tool descriptions. A chat syncs between the devices and
carries the other one's calls in its own history — so a model reading its past
`todo_write(content=...)` against a schema saying `task` will use whichever it
read last, and an argument the phone silently ignores is a read it believes it
did and did not.

## Reading the repo from the phone is a network round trip per file

`grep` and `search_code` scan every file, and each one is its own HTTPS
request. In series that is the difference between a search and a stall: this
repository is 225 scannable files, and at ~150ms each that is over half a
minute before the model sees anything.

They are read in ordered **chunks** now (`scanFiles`, eight at a time). The
same *number* of requests either way — nothing is spent faster against the
GitHub rate limit — but a chunk is not a race, and the difference is two things
that are easy not to notice:

- **Results stay in tree order.** Output that reorders itself between two runs
  of the same search is output nobody can compare.
- **The early stop stops.** `grep` quits at 100 hits; with everything in flight
  it would quit after whatever had already been scheduled, which on a large
  repo is all of it.

`read_file` had the matching problem in the other direction: it took the first
12,000 characters of the joined file and said nothing. The model concluded the
symbol was absent — a wrong answer rather than a missing one — and the cut
landed mid-line, so `edit_file` was handed `old_string` values that never
existed. It cuts on a line boundary now, says how many lines are left, and
names the offset to continue from.

## A write to an existing path needs the sha, and the sha comes back from the write

`makeTools` cached a file's text after a write with `sha: undefined`, and
GitHub refuses to overwrite a path it is not told which blob is being replaced.
So the FIRST edit of a file worked and every one after it went out with no sha
and came back 422 — on the device where every write is a commit and editing the
same file twice is the normal shape of a task.

The new sha is in the response to the previous write. A cache entry that
somehow has none re-reads the file rather than sending nothing.

The fake GitHub in `tests/test_phone_tools.py` enforces that rule. That is the
only reason a test can see this at all — a fake that accepted any PUT would
have passed throughout.

## Reverting a worker has to mean that worker

`revert_worker` called `BackupRepo.revert_to(baseline)`, which is
`reset --hard` plus `clean -fd`. That is the whole work tree, so undoing one
worker also undid **everything done since it started** — another worker's work,
and the user's own edits — and the `clean` additionally deleted untracked files
that were never part of any diff: build output, scratch notes, a half-written
file. The old return string admitted the first half of this, *after* it had
already happened.

The fix needed something the app did not have: knowing which files a
*particular* worker wrote.

- **`_emit_subagent_stream` is where that is knowable.** Every sub-agent event
  funnels through it tagged with the sub-agent's id, which for a background
  worker IS the worker id. A `tool_call` for `write_file`/`edit_file` names a
  path, so the worker's own writes can be recorded as they are announced.
  `replace_in_files` is deliberately not in that map: it takes a pattern, and
  guessing a path would be worse than knowing the tool is not covered.
- **The revert set is the INTERSECTION of two imperfect sets.** `wrote` is
  exact about whose change it was but records *intent* — the event fires before
  the permission engine has had its say, so a denied write is in there.
  `changes` (the baseline diff) is exact about what really changed but contains
  every edit anyone else made meanwhile. A file in both is one this worker asked
  to write and that is genuinely different now. Nothing else is safe to touch on
  its behalf.
- **Nothing attributable means refusing.** Files changed while it ran but none
  are ones it wrote — most likely it edited them by running a command. Reverting
  the tree and explaining afterwards is the behaviour this replaced.
- **A running worker is refused.** The phone's `revert_worker` always did this;
  the desktop did not. Reverting under a worker that is still writing leaves the
  files half one thing and half the other.

`BackupRepo.revert_paths_to` is the per-path instrument. Two things in it are
load-bearing, and the second was found by its own test:

- **`add -A -N` first**, exactly as `changed_files_since` does. Without it a
  file *created* since the baseline is not in the index, and cannot be told
  apart from one the shadow repo excludes entirely.
- **A path the shadow repo does not track is never deleted.** "Not in the
  baseline" has two causes and only one of them means "created since". The other
  is `DEFAULT_EXCLUDES` — `.git/`, `node_modules/`, build output — and there is
  no snapshot of those to restore from. The first version of this deleted the
  project's real `.git/config`, which is why the check lives in `BackupRepo` and
  is not left to the caller.

## A worker that did not finish is not a worker that failed

Four places described any non-`done` worker as a failure, and told the user and
the model so.

- `worker_report_note` and the spoken `announce_worker` both said "failed" for a
  worker the user had **stopped on purpose**. Telling the coding agent a
  cancelled job crashed invites it to diagnose and re-run exactly what was just
  cancelled. `_WORKER_OUTCOMES` maps each status to what it actually means, and
  an unrecognised status claims neither success nor failure.
- `check_workers` counted only running/done/error, so a chat whose one worker
  had been stopped opened with "0 running, 0 done, 0 failed" and then listed it.
  The header is now built from the same statuses the rows print.
- A `reverted` worker fell into that summary's `else` branch, whose `error` is
  `None` — so undoing a worker printed **"FAILED — None"**.
- **A crashed worker recorded no changes at all.** `w["changes"]` was set on the
  success path only, so `worker_changes` and `revert_worker` both believed a
  worker that died half way through had touched nothing — and that is the one
  you most want to be able to undo.

`worker_changes` on a *running* worker had the same shape of bug: `changes` is
filled in at the end, so an empty list mid-flight means "not known yet", and
reporting it as "changed nothing" is a wrong answer about work in progress. It
reads the live diff instead and says the worker is still going.

## A spoken worker name is a fragment, not a sentence

`_resolve_worker` matched both directions — `ident in nm` **and `nm in ident`**.
The second is the wrong way round: a worker named `a` or `fix` is contained in
almost anything anyone would say, so "please stop what you are doing" resolved
to whichever worker happened to be first, and stopped it. Only the fragment
direction survives, and ties now go to the **most recent** worker, because "stop
the browser one" means the one just started rather than its namesake from ten
minutes ago.

## The agent must not reshape the user's browser window

`capture()` called `chrome.windows.update(id, {state: "normal"})` before every
screenshot. `state: "normal"` is not "make this visible" — it is Chrome's
**restore**: it un-maximizes a maximized window and drops a fullscreen one back
to a floating rectangle. So the agent resized the browser the user lives in, on
the way past, with no undo — once the state has been changed the previous one is
not readable from anywhere.

`focused: true` alone is what was wanted. It raises a covered window, and for a
minimized one Chrome restores whatever it was before — precisely the value that
would otherwise have to be guessed at. `tests/test_extension_window.py` asserts
that **no** `chrome.windows.update` call in the extension carries a `state`,
not just the one in `capture()`: every one of them is reachable from an ordinary
agent action.

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

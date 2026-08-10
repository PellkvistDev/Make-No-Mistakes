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

`heal_interrupted_turn` exists in both `glmcode/syncstore.py` and
`mobile/agent-core.js` and the two must stay in step: each end adopts the
other's histories, and a turn killed between a tool running and its result
being recorded is unsendable until repaired. On the desktop it runs inside
`chat_to_session`, so every route in is covered — including the manual pull
button, which could always land such a history and fail on its first request.

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

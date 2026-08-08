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

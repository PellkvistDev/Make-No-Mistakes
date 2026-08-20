# Make No Mistakes — browser control

Lets the desktop app drive the tab you are looking at, in your own browser,
with you already signed in.

## Install (about thirty seconds, once)

1. Open `chrome://extensions` in the browser you want the agent to drive.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → choose this folder.

Then turn on Settings → Browser → **Use my own browser** in the app. The panel
says *Connected* as soon as both ends are up.

Chrome, Edge, Brave, Arc and anything else Chromium-based all work; the app's
Settings panel has a button that opens this folder for you.

## If it says it isn't connected

The app has to be running for the extension to have anything to connect to, and
the extension reconnects on its own — within a few seconds if you click around
in the browser, within thirty otherwise. If it stays disconnected:

- Is the toolbar button showing `||`? That is paused. Click it.
- Is **Use my own browser** actually on in Settings → Browser?
- Reload the extension on `chrome://extensions` (the circular arrow).

When it is off, the agent falls back to launching a separate browser of its own
and **says so in the chat** rather than leaving you watching the wrong window.

## What it does, and what it cannot

It talks **only** to the app, on `127.0.0.1`. Nothing is uploaded, there is no
account, and no server is involved. The toolbar button pauses it at any time —
while paused it disconnects and refuses every command.

It drives the **active tab in the frontmost window**. Browser pages
(`chrome://`, the Web Store, other extensions) are off limits — Chrome forbids
extensions from touching those, and it says so rather than failing quietly.

Clicks and typing are dispatched as page events, which is right for essentially
everything you would ask an agent to do. A site that specifically checks
`event.isTrusted` — mostly bot-detection on sign-in pages — will not accept
them. For those, the app's own launched browser (the default) uses real
browser-level input and is the better tool.

## Why an extension at all

Chrome's DevTools port can only be opened when the browser **starts**. There is
no way to switch it on afterwards, so "attach to the window I already have
open" is impossible from outside the browser — the honest version was always
"quit your browser and reopen it with these flags", which is not something
anyone wants to do twice.

An extension is already inside the browser. Same profile, same logins, same
tabs, no flags and no relaunch. So the connection runs the other way: the
browser dials the app.

`page.js` is partly generated — run `python scripts/gen_extension_page.py`
after changing `SNAPSHOT_JS` in `glmcode/browser_session.py`. CI fails on a
stale copy.

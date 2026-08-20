# Make No Mistakes — browser control

Lets the desktop app drive the tab you are looking at, in your own browser,
with you already signed in.

## Install (about thirty seconds, once)

Easiest: in the app, **Settings → Browser → Set it up**. That sheet opens the
extensions page in whichever browser you pick, hands you the folder to paste,
shows a green light the moment it connects, and has the switch on it so you
never have to go looking for it afterwards.

By hand, if you prefer:

1. Open `chrome://extensions` in the browser you want the agent to drive.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → choose this folder.
That is all. There is no switch to find afterwards — the app uses your browser
whenever the extension is connected.

Chrome, Edge, Brave, Vivaldi, Arc and anything else Chromium-based all work.

## If a screenshot fails

`captureVisibleTab` photographs what is **on screen**. If the browser window is
minimised or fully covered, Chrome has nothing rendered to read back and says
`image readback failed`. The extension raises the window and retries, but a
minimised window it cannot restore will still fail — the page text and the
clickable-element list both still work in that case.

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

The agent asks which tabs are open and picks one — by default it opens a
**new** tab for its own work rather than taking over one you are reading.
When it switches to an existing tab it brings that tab to the front, so you
can see where it is working. Browser pages
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

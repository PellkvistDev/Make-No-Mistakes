"""Generate the snapshot block inside extension/page.js.

The extension drives the page with the SAME accessibility snapshot the desktop
hands Playwright -- same stamped refs, same regions, same caps -- because the
Browser Agent's whole prompt is written against that exact format. Two copies
of it would be two subtly different pages described to the same model.

It cannot simply be sent over the wire the way it is handed to Playwright:
MV3 forbids unsafe-eval, so an extension cannot run a JavaScript string it was
given. The snapshot has to BE code in the extension. So it is generated, from
the one definition in glmcode/browser_session.py.

Same rule as scripts/gen_mobile_core.py: change the PYTHON and run this. The
block is checked in so the extension stays a folder of static files that Chrome
can load with no build step, and `--check` runs in CI so a stale block fails
rather than shipping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "extension" / "page.js"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from glmcode.browser_session import INTERACTIVE_SELECTOR, SNAPSHOT_JS  # noqa: E402

BEGIN = "  /* ==== GENERATED — do not edit by hand ==== */"
END = "  /* ==== END GENERATED ==== */"


def block() -> str:
    body = "\n".join("  " + ln if ln.strip() else ln
                     for ln in SNAPSHOT_JS.splitlines())
    return "\n".join([
        BEGIN,
        "  // From glmcode/browser_session.py — run scripts/gen_extension_page.py",
        f"  const MNM_SELECTOR = {json.dumps(INTERACTIVE_SELECTOR)};",
        "  const MNM_SNAPSHOT = " + body.lstrip() + ";",
        END,
    ])


def render(source: str) -> str:
    start = source.find(BEGIN)
    end = source.find(END, start)
    if start < 0 or end < 0:
        raise SystemExit(f"{TARGET} has no generated block; expected\n{BEGIN}\n{END}")
    return source[:start] + block() + source[end + len(END):]


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")
    fresh = render(source)
    if "--check" in sys.argv:
        if fresh != source:
            print(f"{TARGET.relative_to(ROOT)} is STALE — run "
                  "python scripts/gen_extension_page.py", file=sys.stderr)
            return 1
        print(f"{TARGET.relative_to(ROOT)} is up to date.")
        return 0
    TARGET.write_text(fresh, encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

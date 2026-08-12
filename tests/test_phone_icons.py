"""The home-screen icon.

The phone shipped an inline SVG for both `rel=icon` and `apple-touch-icon`.
iOS ignores an SVG apple-touch-icon entirely -- it falls back to a screenshot
of the page -- so the installed app never wore the icon the desktop app does,
and the two looked like different programs.

None of this is visible in the markup: the failure modes live in the image
files (wrong format, transparent corners, wrong size), so the files themselves
are opened here.

Deliberately NOT under tests/mobile_ui/: that package's conftest skips the
whole directory when playwright is missing, and nothing here needs a browser --
so filing it there would have meant these checks quietly not running in any
environment without one.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOBILE = ROOT / "mobile"
DESKTOP_ICON = ROOT / "glmcode" / "gui" / "web" / "app_icon.png"

# Pillow is in requirements.txt, so this runs rather than skips in CI -- worth
# checking, because a skipped test and a passing one look the same in a summary.
Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")

ICONS = {"icon-180.png": 180, "icon-192.png": 192, "icon-512.png": 512,
         "icon-maskable-512.png": 512}


@pytest.mark.parametrize("name, size", sorted(ICONS.items()))
def test_each_icon_is_a_png_of_the_size_it_claims(name, size):
    im = Image.open(MOBILE / name)
    assert im.format == "PNG", "iOS ignores an SVG apple-touch-icon outright"
    assert im.size == (size, size)


@pytest.mark.parametrize("name", sorted(ICONS))
def test_no_transparent_corners(name):
    """Both platforms mask the icon into their own shape. A pre-rounded one
    gets rounded twice, with the app's dark corners showing through the gap --
    and on iOS transparency composites onto black, so the baked rounding reads
    as four black notches."""
    im = Image.open(MOBILE / name).convert("RGBA")
    w, h = im.size
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        assert im.getpixel(xy)[3] == 255, f"{name} is transparent at {xy}"


def test_the_maskable_one_keeps_the_logo_out_of_the_crop():
    """Android crops a maskable icon to a circle, taking roughly the outer 10%.
    Full-bleed artwork loses the outer strokes of the logo, which is why this
    cannot be the same file as the "any" icon."""
    plain = Image.open(MOBILE / "icon-512.png").convert("RGB")
    mask = Image.open(MOBILE / "icon-maskable-512.png").convert("RGB")
    assert list(plain.getdata()) != list(mask.getdata())
    # The outer band is flat backdrop, so the circle cannot clip the logo.
    edge = [mask.getpixel((x, 8)) for x in range(0, 512, 32)]
    assert len(set(edge)) == 1, "the outer band should be plain background"


def test_it_is_the_desktop_icon_and_not_some_other_art():
    """The whole point: one app, one icon. Compared as images rather than by
    filename, since a stale or unrelated copy would pass any name check."""
    want = Image.open(DESKTOP_ICON).convert("RGB").resize((32, 32), Image.LANCZOS)
    got = Image.open(MOBILE / "icon-192.png").convert("RGB").resize((32, 32), Image.LANCZOS)
    diff = sum(abs(a - b)
               for pa, pb in zip(want.getdata(), got.getdata())
               for a, b in zip(pa, pb)) / (32 * 32 * 3)
    # Not identical -- the corners were filled in and it was rescaled -- but
    # recognisably the same picture.
    assert diff < 12, f"the phone icon is not the desktop icon (mean diff {diff:.1f})"


def test_the_page_points_at_the_png_and_not_at_an_svg():
    html = (MOBILE / "index.html").read_text(encoding="utf-8")
    assert 'rel="apple-touch-icon" sizes="180x180" href="./icon-180.png"' in html
    # The data-URI SVG is what iOS was silently discarding.
    assert 'rel="apple-touch-icon" href="data:image/svg' not in html


def test_the_manifest_offers_both_purposes_from_different_files():
    m = json.loads((MOBILE / "manifest.webmanifest").read_text(encoding="utf-8"))
    by_purpose = {i["purpose"]: i["src"] for i in m["icons"]}
    assert by_purpose["any"] != by_purpose["maskable"]
    assert all(i["type"] == "image/png" for i in m["icons"])


def test_the_icons_are_precached_so_they_survive_an_offline_install():
    sw = (MOBILE / "sw.js").read_text(encoding="utf-8")
    for name in ICONS:
        assert f'"./{name}"' in sw, f"{name} missing from the precached shell"
    # A changed shell needs a changed cache name, or a phone sitting on the old
    # one never fetches the new files.
    assert 'const CACHE = "mnm-shell-v6"' in sw

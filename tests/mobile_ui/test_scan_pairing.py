"""Scanning the desktop's pairing code, through the real camera pipeline.

The code would not scan, and nothing in either codebase could tell you why: the
QR was built correctly, the decoder worked, and the two ends agreed on the
wire. What was wrong was in between -- the frames the decoder was given.

`getUserMedia` was called with no resolution at all, which browsers commonly
answer with 640x480, and the decode canvas then capped the frame at 720px wide.
A pairing QR is many times denser than the install QR next to it, so at that
size the modules landed under two pixels each and no decoder on earth reads
that. The install code, being coarse, scanned every time -- which is exactly
why this looked like a problem with the pairing code specifically.

So the test drives the whole path: a real sealed token from glmcode.pairing, a
real QR image, played through a fake camera as an actual video track, decoded
by the app's own scanner. Nothing here asserts a constant; it asserts that the
app reads the code, which is the only claim that matters and the one no unit
test can make.
"""

import base64
import io

import pytest

from glmcode import pairing, syncstore

segno = pytest.importorskip("segno")
needs_crypto = pytest.mark.skipif(
    not syncstore.crypto_available(), reason="cryptography AES-GCM unavailable")

CODE = "NW89GR"


def _pair_png() -> str:
    """The pairing QR as the desktop draws it, as a data URL."""
    payload = pairing.build_payload(
        model_key="sk-from-the-desktop",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.6-flash",
        github_token="github_pat_" + "c" * 70,
        sync_passphrase="a shared passphrase",
        providers=[
            {"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4",
             "key": "sk-zai", "models": ["glm-4.7-flash", "glm-4.6v-flash"]},
            {"name": "Google AI Studio", "key": "sk-google",
             "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
             "models": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash"]},
        ])
    buf = io.BytesIO()
    segno.make(pairing.seal(payload, CODE), error="l").save(
        buf, kind="png", scale=10, border=2)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# A camera pointed at a laptop screen from a comfortable distance. The QR is a
# fraction of the frame with the rest of the desk around it -- if the scanner
# only worked on a QR filling the viewport it would not work in the room.
FAKE_CAMERA = r"""([png, w, h, fill]) => {
  window.__constraints = [];
  const img = new Image();
  const ready = new Promise((res) => { img.onload = res; img.src = png; });
  navigator.mediaDevices.getUserMedia = async (c) => {
    window.__constraints.push(JSON.parse(JSON.stringify(c)));
    await ready;
    // Honour the requested resolution the way a camera does: this is the whole
    // variable under test, so it must not be quietly ignored here.
    const want = (c.video && c.video.width && c.video.width.ideal) || 640;
    const wantH = (c.video && c.video.height && c.video.height.ideal) || 480;
    const cv = document.createElement("canvas");
    cv.width = Math.min(want, w); cv.height = Math.min(wantH, h);
    const ctx = cv.getContext("2d");
    const side = Math.round(cv.height * fill);
    const draw = () => {
      ctx.fillStyle = "#6a6a72";
      ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, (cv.width - side) / 2, (cv.height - side) / 2, side, side);
      requestAnimationFrame(draw);
    };
    draw();
    return cv.captureStream(15);
  };
}"""


def _scan(phone, png, *, width=1920, height=1080, fill=0.55):
    phone.page.evaluate(FAKE_CAMERA, [png, width, height, fill])
    phone.page.evaluate(
        "(c) => { window.prompt = () => c; }", CODE)
    phone.page.click("#btn-scan-setup")


@needs_crypto
def test_the_pairing_code_is_read_off_a_screen(phone):
    """The bug, end to end. Everything under the camera is the real app."""
    _scan(phone, _pair_png())
    p = phone.page
    p.wait_for_function(
        "() => document.getElementById('in-model-key').value === 'sk-from-the-desktop'",
        timeout=30000)
    assert p.input_value("#in-gh-token").startswith("github_pat_")
    assert p.eval_on_selector("#scan-backdrop", "e => e.hidden") is True
    assert phone.errors == []


@needs_crypto
def test_the_apis_come_across_too(phone):
    _scan(phone, _pair_png())
    p = phone.page
    p.wait_for_function(
        "() => JSON.parse(localStorage.getItem('mnm.providers') || '[]').length === 2",
        timeout=30000)
    names = p.evaluate(
        "() => JSON.parse(localStorage.getItem('mnm.providers')).map(x => x.name)")
    assert names == ["Z.AI", "Google AI Studio"]


@needs_crypto
def test_a_camera_is_asked_for_enough_pixels_to_resolve_the_modules(phone):
    """Not a style point. Left unasked, browsers hand back 640x480, and at that
    size the pairing QR's modules are under two pixels wide."""
    _scan(phone, _pair_png())
    p = phone.page
    p.wait_for_function("() => (window.__constraints || []).length", timeout=15000)
    c = p.evaluate("() => window.__constraints[0]")["video"]
    assert c["width"]["ideal"] >= 1280, c
    assert c["height"]["ideal"] >= 720, c
    # And still the back camera: the front one points at your face.
    assert c["facingMode"]["ideal"] == "environment"


@needs_crypto
def test_a_qr_that_is_not_ours_does_not_open_the_code_prompt(phone):
    """The scanner keeps looking and says so. Opening a code prompt for the
    sticker on a router would be its own kind of broken."""
    buf = io.BytesIO()
    segno.make("WIFI:S:cafe;T:WPA;P:hunter2;;", error="l").save(
        buf, kind="png", scale=10, border=2)
    png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    phone.page.evaluate(FAKE_CAMERA, [png, 1920, 1080, 0.55])
    phone.page.evaluate(
        "() => { window.__prompted = 0; window.prompt = () => { window.__prompted++; return null; }; }")
    phone.page.click("#btn-scan-setup")
    p = phone.page
    p.wait_for_function(
        "() => /not a set-up code/.test(document.getElementById('scan-status').textContent)",
        timeout=30000)
    assert p.evaluate("() => window.__prompted") == 0
    assert p.eval_on_selector("#scan-backdrop", "e => e.hidden") is False


# ---- how many pixels the decoder actually gets -----------------------------
#
# What is NOT tested here, and cannot be: whether a real phone reads the code.
# The frames above are drawn into a canvas, so they are perfectly sharp,
# perfectly square-on, noiseless and at full contrast. jsQR reads a 113-module
# code from a 264-pixel synthetic square without complaint -- roughly two
# pixels per module -- while the same thing photographed through a lens does
# not, because a lens adds the blur this harness has no way to produce.
#
# So the margin is asserted where it is a fact rather than an optical
# judgement: the number of pixels the decoder is handed. That is the thing that
# was wrong (a 720-pixel cap on a 640-pixel camera frame) and the thing a later
# change could silently undo.

SPY_DECODER = r"""() => {
  // The vendor script assigns window.jsQR when it loads, so the wrapper is
  // installed as a setter rather than around an object that does not exist yet.
  let real = null;
  window.__frames = [];
  Object.defineProperty(window, "jsQR", {
    configurable: true,
    get() {
      if (!real) return undefined;
      return (data, w, h) => { window.__frames.push([w, h]); return real(data, w, h); };
    },
    set(fn) { real = fn; },
  });
}"""


@needs_crypto
def test_the_frame_is_not_shrunk_below_what_the_code_needs(phone):
    """It used to be shrunk to 720 pixels wide. That cap was set when the only
    code being scanned was the 37-module install one, where it cost nothing;
    against a pairing code it threw away exactly the detail being looked for.

    There is still a cap, and it should stay: jsQR is plain JavaScript and its
    cost is per pixel, so handing it a full 1920x1080 frame would drop the
    scan rate to a crawl on the device this exists for. The cap just has to sit
    above the code rather than through it."""
    phone.page.evaluate(SPY_DECODER)
    _scan(phone, _pair_png())
    p = phone.page
    p.wait_for_function("() => (window.__frames || []).length", timeout=30000)
    w, h = p.evaluate("() => window.__frames[0]")
    assert w >= 1440, f"decoded a {w}x{h} copy of a 1920x1080 frame"
    # Squashed rather than scaled would distort the modules into unreadability
    # while keeping the pixel count that this test is checking for.
    assert abs(w / h - 1920 / 1080) < 0.01


@needs_crypto
def test_the_pixels_per_module_clear_what_a_decoder_needs(phone):
    """The two changes multiply, and this is the product of them: frame size
    from the camera request, module count from the payload. Either one
    regressing shows up here as a number, rather than as a phone that will not
    pair with no way to tell which end is at fault."""
    modules = segno.make(
        pairing.seal(pairing.build_payload(
            model_key="k", github_token="t" * 80, sync_passphrase="p" * 24,
            providers=[{"name": "Google", "key": "g" * 39,
                        "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                        "models": ["gemini-3.6-flash", "gemini-3.5-flash"]}]),
            CODE), error="l").symbol_size(scale=1, border=0)[0]
    phone.page.evaluate(SPY_DECODER)
    _scan(phone, _pair_png())
    p = phone.page
    p.wait_for_function("() => (window.__frames || []).length", timeout=30000)
    _, h = p.evaluate("() => window.__frames[0]")
    # The QR occupies a bit over half the frame's height when the phone is held
    # at a comfortable distance -- the framing the fixture draws.
    per_module = (h * 0.55) / modules
    assert per_module >= 4, (
        f"{per_module:.1f} pixels per module across {modules} modules. Decoders "
        "want three or more before optics are taken into account, and this had "
        "fallen to about 1.7.")

"""What the pairing QR actually contains, from the desktop's side.

The code would not scan. Two symptoms, and the second one gave it away: the
in-app scanner never read it, but pointing the phone's *Camera* app at it
worked -- and opened the website with nothing filled in.

So the image decoded fine. It held a URL, the Camera app did what a Camera app
does with a URL, and Safari opened it. On iOS an app installed to the home
screen has its own storage, so the keys landed in Safari and the app still had
none. It looked exactly like a code that could not be read.

The QR now holds the bare token. Nothing can "open" that, so scanning it with
the wrong app does nothing at all -- which is a far better failure. And the URL
was never needed here: it is on screen a step earlier, in the install code.
"""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode import config as config_mod  # noqa: E402
from glmcode import pairing, syncstore  # noqa: E402
from glmcode.gui import app as gui_app  # noqa: E402

needs_crypto = pytest.mark.skipif(
    not syncstore.crypto_available(), reason="cryptography AES-GCM unavailable")
segno = pytest.importorskip("segno")

APP_URL = "https://example.github.io/app/"


@pytest.fixture
def api(monkeypatch):
    # A provider's key lives in an environment variable now, and the stored
    # field is only the fallback -- so a variable set on the machine running
    # the tests is the key that gets paired, not the one written here.
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    cfg = config_mod.Config()
    cfg.phone_app_url = APP_URL
    cfg.providers = [{"name": "Z.AI", "base_url": "https://api.z.ai/api/paas/v4",
                      "api_key": "sk-zai", "models": ["glm-4.7-flash"]}]
    a = gui_app.Api.__new__(gui_app.Api)
    a._cfg = cfg
    a._gh_token = lambda: "github_pat_" + "c" * 70
    monkeypatch.setattr(syncstore, "load_passphrase", lambda: "a sync passphrase")
    return a


@needs_crypto
def test_the_pairing_image_holds_no_url(api, monkeypatch):
    """The whole fix. A URL in this code is one the Camera app will open, into
    the wrong storage box."""
    encoded = []
    monkeypatch.setattr(gui_app.qrcode_util, "qr_svg",
                        lambda data, **kw: encoded.append(data) or "<svg/>")
    res = api.get_pair_phone()
    assert "error" not in res, res
    assert "http" not in encoded[0]
    assert APP_URL not in encoded[0]
    # And it is the token itself, not the token with something around it.
    assert set(encoded[0]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@needs_crypto
def test_the_code_is_still_not_in_the_image(api, monkeypatch):
    """The one property that makes a photographed QR useless on its own."""
    encoded = []
    monkeypatch.setattr(gui_app.qrcode_util, "qr_svg",
                        lambda data, **kw: encoded.append(data) or "<svg/>")
    res = api.get_pair_phone()
    assert res["code"] not in encoded[0]


@needs_crypto
def test_what_is_encoded_opens_back_into_the_keys(api, monkeypatch):
    """End to end through the real seal: whatever went into the image is what
    the phone will be handed."""
    encoded = []
    monkeypatch.setattr(gui_app.qrcode_util, "qr_svg",
                        lambda data, **kw: encoded.append(data) or "<svg/>")
    res = api.get_pair_phone()
    back = pairing.open_sealed(encoded[0], res["code"])
    assert back["modelKey"] == "sk-zai"
    assert back["githubToken"].startswith("github_pat_")
    assert back["syncPass"] == "a sync passphrase"


@needs_crypto
def test_the_image_is_coarse_enough_for_a_phone_camera(api, monkeypatch):
    """113 modules is what would not scan. This is the number that decides it,
    so it is the number asserted -- a field added to the payload later is how
    it creeps back, and nobody notices until a phone will not read it."""
    encoded = []
    monkeypatch.setattr(gui_app.qrcode_util, "qr_svg",
                        lambda data, **kw: encoded.append(data) or "<svg/>")
    api.get_pair_phone()
    n = segno.make(encoded[0], error="l").symbol_size(scale=1, border=0)[0]
    assert n <= 81, f"{n} modules is too dense to read off a screen"


@needs_crypto
def test_pairing_refuses_when_there_is_nothing_but_a_model_name_to_send(monkeypatch):
    """A default model name is always set, so "is the payload empty" was never
    the right question: a desktop with no keys at all minted a code whose whole
    contents was the string "glm-4.7-flash". The phone pairs, believes itself
    configured, and fails on the first message with an error about the key."""
    cfg = config_mod.Config()
    cfg.phone_app_url = APP_URL
    cfg.providers = []
    a = gui_app.Api.__new__(gui_app.Api)
    a._cfg = cfg
    a._gh_token = lambda: ""
    monkeypatch.setattr(syncstore, "load_passphrase", lambda: "")
    assert "error" in a.get_pair_phone()


@needs_crypto
def test_a_sync_passphrase_on_its_own_is_worth_pairing(monkeypatch):
    """Shared chats need no model key, so refusing here would block the one
    thing that machine could actually hand over."""
    cfg = config_mod.Config()
    cfg.phone_app_url = APP_URL
    cfg.providers = []
    a = gui_app.Api.__new__(gui_app.Api)
    a._cfg = cfg
    a._gh_token = lambda: ""
    monkeypatch.setattr(syncstore, "load_passphrase", lambda: "a sync passphrase")
    assert "error" not in a.get_pair_phone()

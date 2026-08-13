"""Turning shared chats on without inventing a passphrase.

The passphrase was never a password. It is the key the chats are encrypted
under, and its only job is to be identical on both devices -- pairing already
carries it here. Asking a person to make one up bought nothing except a chance
to pick something weak, or to mistype it on the second device and fork the
history into two halves that can never read each other. On a phone keyboard,
for a string that has to match another machine exactly, that is the worst
possible place to ask.

So there are three cases and only one of them involves typing:

  * this phone was paired -> it already has the key, and sync just works
  * nothing exists yet     -> it makes one, the way the computer would
  * a store already exists -> the key is decided and cannot be guessed, so the
                              recovery code is copied across

The last one is the only one with a text field, and what goes in it is a code
being copied, not a secret being invented -- one field, no confirmation.
"""

import json

import pytest

# Worded rather than shaped like a real code, on purpose. A passphrase keyword
# assigned a random-looking grouped string is what a secret scanner exists to
# catch, and it filed an incident against this repo for one -- reasonably, on
# the evidence available to it. A test fixture is not worth arguing with that
# over. Nothing here depends on the shape: the tests that care what a generated
# key looks like generate one and inspect that.
#
# Do not paste an example of the offending form into this comment. That was
# tried, and the scanner flagged the comment.
OTHER_DEVICE_KEY = "example-key-from-the-other-device"
WRONG_KEY = "example-key-that-does-not-match"


def _to_chat(phone):
    p = phone.page
    p.fill("#in-model-key", "modelkey")
    p.fill("#in-gh-token", "ghtoken")
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_selector("#screen-repo:not([hidden])", timeout=15000)
    p.wait_for_selector(".repo-list li", timeout=15000)
    p.click(".repo-list li")
    p.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    return phone


def _settings(phone):
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    return phone


def _seed_store(phone, passphrase):
    """A store another device already created."""
    phone.page.evaluate("""async (pass) => {
      const AC = window.AgentCore;
      const probe = AC.makeGitHub({ token: "ghtoken", owner: "", repo: "" });
      const { owner, repo } = await AC.ensureSyncRepo(probe);
      const gh = AC.makeGitHub({ token: "ghtoken", owner, repo, branch: AC.SYNC_REPO_BRANCH });
      await AC.openSync(gh, pass);
    }""", passphrase)
    return phone


def _stored_pass(phone):
    return phone.page.evaluate("() => window.__peekSyncPass ? window.__peekSyncPass() : null")


# ------------------------------------------------- first device: no typing

def test_turning_it_on_asks_for_nothing(phone):
    """The whole complaint. No dialog, no field, no passphrase to think up."""
    _to_chat(phone)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_function(
        "() => document.getElementById('set-sync').checked", timeout=20000)
    assert p.eval_on_selector("#syncpass-backdrop", "e => e.hidden") is True
    assert p.evaluate("() => localStorage.getItem('mnm.sync')") == "1"


def test_the_key_it_makes_actually_opens_the_store(phone):
    """Generating one is only useful if the store it bootstraps can be read
    back -- including by a second reader that has only the key."""
    _to_chat(phone)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_function("() => document.getElementById('set-sync').checked", timeout=20000)
    phone.close_settings()
    phone.reply({"role": "assistant", "content": "ok"})
    phone.send("hello").wait_idle()
    _settings(phone)
    p.click("#btn-change-syncpass")
    p.wait_for_selector("#set-syncpass-code:not([hidden])", timeout=15000)
    code = p.input_value("#set-syncpass-code")
    assert code, "the recovery code has to be readable, or it is unrecoverable"
    chats = phone.stored_chats(code)
    assert chats and chats[0]["transcript"], "the generated key must open its own store"


def test_the_generated_key_is_not_something_a_person_would_pick(phone):
    _to_chat(phone)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_function("() => document.getElementById('set-sync').checked", timeout=20000)
    p.click("#btn-change-syncpass")
    p.wait_for_selector("#set-syncpass-code:not([hidden])", timeout=15000)
    code = p.input_value("#set-syncpass-code")
    # 4 groups of 5, from an alphabet with no lookalikes -- 100 bits, and
    # readable off a screen, which is the one time a human touches it.
    assert len(code.split("-")) == 4
    assert all(len(g) == 5 for g in code.split("-"))
    assert not (set(code) & set("IO01")), "these are the ones people misread"


def test_two_phones_do_not_generate_the_same_key(phone):
    codes = phone.page.evaluate(
        "() => Array.from({length: 50}, () => AgentCore.makeSyncPassphrase())")
    assert len(set(codes)) == 50


# -------------------------------------------- second device: the code path

def test_a_store_that_already_exists_asks_for_the_code(phone):
    """And must NOT generate: a fresh key here would bootstrap nothing and
    leave two halves that can never read each other."""
    _to_chat(phone)
    _seed_store(phone, OTHER_DEVICE_KEY)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_selector("#syncpass-backdrop:not([hidden])", timeout=20000)
    assert p.eval_on_selector("#set-sync", "e => e.checked") is False


def test_the_code_dialog_asks_once_not_twice(phone):
    """It used to ask for a passphrase and then to repeat it. This is a code
    being copied off another screen; confirming a copy against itself catches
    nothing that verification does not."""
    _to_chat(phone)
    _seed_store(phone, OTHER_DEVICE_KEY)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_selector("#syncpass-backdrop:not([hidden])", timeout=20000)
    assert p.query_selector("#in-syncpass2") is None
    assert p.eval_on_selector("#in-syncpass", "e => e.type") != "password", \
        "a code being copied off a screen is not something to mask"


def test_the_right_code_joins_the_existing_store(phone):
    _to_chat(phone)
    _seed_store(phone, OTHER_DEVICE_KEY)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_selector("#syncpass-backdrop:not([hidden])", timeout=20000)
    p.fill("#in-syncpass", OTHER_DEVICE_KEY)
    p.click("#btn-syncpass-save")
    p.wait_for_selector("#syncpass-backdrop", state="hidden", timeout=20000)
    assert p.eval_on_selector("#set-sync", "e => e.checked") is True


def test_a_wrong_code_is_refused_rather_than_forking_the_history(phone):
    """The failure the verification exists for: a mismatched key that silently
    "worked" would leave this phone writing chats no other device can read."""
    _to_chat(phone)
    _seed_store(phone, OTHER_DEVICE_KEY)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_selector("#syncpass-backdrop:not([hidden])", timeout=20000)
    p.fill("#in-syncpass", WRONG_KEY)
    p.click("#btn-syncpass-save")
    p.wait_for_function(
        "() => document.getElementById('syncpass-error').textContent.length > 0",
        timeout=20000)
    assert p.eval_on_selector("#syncpass-backdrop", "e => e.hidden") is False
    assert p.evaluate("() => localStorage.getItem('mnm.sync')") != "1"


def test_a_code_too_short_to_be_one_is_caught_before_the_network(phone):
    _to_chat(phone)
    _seed_store(phone, OTHER_DEVICE_KEY)
    _settings(phone)
    p = phone.page
    p.click("#set-sync")
    p.wait_for_selector("#syncpass-backdrop:not([hidden])", timeout=20000)
    p.fill("#in-syncpass", "abc")
    p.click("#btn-syncpass-save")
    p.wait_for_timeout(300)
    assert "too short" in p.text_content("#syncpass-error")


# -------------------------------------------------------- paired: nothing

def test_a_paired_phone_never_sees_any_of_this(phone, app_url):
    """Pairing carries the key over, which is the whole point of pairing --
    so the common path has no dialog in it at all, generated or typed."""
    pairing = pytest.importorskip("glmcode.pairing")
    syncstore = pytest.importorskip("glmcode.syncstore")
    if not syncstore.crypto_available():
        pytest.skip("cryptography unavailable")
    code = pairing.make_code()
    token = pairing.seal(pairing.build_payload(
        model_key="sk", github_token="ghtoken", base_url="https://api.z.ai/api/paas/v4",
        model="glm-4.7-flash", sync_passphrase=OTHER_DEVICE_KEY), code)
    p = phone.page
    p.on("dialog", lambda d: d.accept(code))
    phone.open_at(pairing.pair_url(app_url, token))
    p.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    p.wait_for_function(
        "() => document.getElementById('in-gh-token').value === 'ghtoken'", timeout=15000)
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    # Sync came on by itself, with the key from the computer.
    p.wait_for_function("() => localStorage.getItem('mnm.sync') === '1'", timeout=20000)
    assert p.eval_on_selector("#syncpass-backdrop", "e => e.hidden") is True
    # And the key it holds is the computer's, read back through the app's own
    # control rather than by reaching into storage.
    # A phone that arrives with sync already on lands on the chat hub, not the
    # repo picker -- there are chats to show before there is a repo to choose.
    p.wait_for_selector("#screen-chats:not([hidden])", timeout=20000)
    p.click("#btn-chats-settings")
    p.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    p.click("#btn-change-syncpass")
    p.wait_for_selector("#set-syncpass-code:not([hidden])", timeout=15000)
    assert p.input_value("#set-syncpass-code") == OTHER_DEVICE_KEY

"""Pairing the phone from the desktop, end to end.

The token is sealed by glmcode.pairing -- the real desktop code -- and opened
by the real phone app, so this pins the two halves of the wire format together
the same way the syncstore tests pin the two codecs.

The question this answers concretely: does the phone get the sync passphrase
automatically, or does the user have to type it again? It has to survive an
awkward ordering to do so. The passphrase is stored encrypted under the vault
key, and the vault key does not exist until a PIN has been chosen -- so it is
held in memory across the setup screen and only written once the PIN is set.
That gap is exactly where it would be easy to drop it.
"""

import pytest

pairing = pytest.importorskip("glmcode.pairing")
syncstore = pytest.importorskip("glmcode.syncstore")

pytestmark = pytest.mark.skipif(
    not syncstore.crypto_available(), reason="cryptography not installed")

SYNC_PASS = "correct horse battery"


@pytest.fixture
def paired(phone, app_url):
    """The app opened with a real sealed pairing token in the URL fragment."""
    payload = pairing.build_payload(
        model_key="sk-from-desktop",
        base_url="https://api.z.ai/api/paas/v4",
        model="glm-4.7",
        github_token="github_pat_from_desktop",
        sync_passphrase=SYNC_PASS,
    )
    code = pairing.make_code()
    token = pairing.seal(payload, code)

    phone.page.on("dialog", lambda d: d.accept(code))
    phone.open_at(pairing.pair_url(app_url, token))
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    return phone


def test_scanning_fills_in_the_keys_from_the_desktop(paired):
    p = paired.page
    p.wait_for_function(
        "() => document.getElementById('in-model-key').value !== ''", timeout=15000)
    assert p.input_value("#in-model-key") == "sk-from-desktop"
    assert p.input_value("#in-gh-token") == "github_pat_from_desktop"
    # From the MENU, not the free-text box. A payload whose base URL matches a
    # known API selects that API, so the model belongs in its model menu -- and
    # one the menu does not list (like this) is added rather than dropped,
    # since it is what the desktop is actually using.
    assert p.input_value("#in-model-pick") == "glm-4.7"
    assert paired.errors == []


def test_the_sync_passphrase_comes_across_without_being_retyped(paired):
    """Installing the app and sharing your chats is meant to be one act."""
    p = paired.page
    p.wait_for_function(
        "() => document.getElementById('in-model-key').value !== ''", timeout=15000)

    # Only a PIN is chosen here -- the passphrase is never typed on the phone.
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_selector("#screen-setup", state="hidden", timeout=15000)

    assert p.evaluate("() => localStorage.getItem('mnm.sync')") == "1", \
        "pairing carried the passphrase but left sync switched off"

    # Sync being on sends the app straight to the chat hub, which is also where
    # it bootstraps the store. Wait for that to finish: probing underneath it
    # means two writers creating sync.json at once, and the fake honours the
    # sha precondition, so the loser gets a real 409.
    p.wait_for_selector("#screen-chats:not([hidden])", timeout=15000)
    # Not just "an <li> exists" -- the loading placeholder is an <li> too, and
    # it is on screen before the store has been bootstrapped.
    p.wait_for_function("""() => {
      const el = document.querySelector('#chats-list li');
      return el && !/Loading/.test(el.textContent);
    }""", timeout=15000)

    # And it is the desktop's passphrase, not merely *a* passphrase: prove it
    # by opening the store with what the desktop generated.
    ok = p.evaluate("""async (pass) => {
      const AC = window.AgentCore;
      const probe = AC.makeGitHub({ token: "github_pat_from_desktop", owner: "", repo: "" });
      const { owner, repo } = await AC.ensureSyncRepo(probe);
      const gh = AC.makeGitHub({ token: "github_pat_from_desktop", owner, repo,
                                 branch: AC.SYNC_REPO_BRANCH });
      try { await AC.openSync(gh, pass); return true; } catch (e) { return String(e); }
    }""", SYNC_PASS)
    assert ok is True, f"the phone's store does not open under the desktop's passphrase: {ok}"
    assert paired.errors == []


def test_a_wrong_code_does_not_hand_over_anything(phone, app_url):
    """The code is deliberately not in the image, so a photographed QR is
    useless on its own. That only holds if a bad code yields nothing."""
    payload = pairing.build_payload(model_key="sk-secret", sync_passphrase=SYNC_PASS)
    token = pairing.seal(payload, pairing.make_code())

    seen = []

    def wrong_then_give_up(d):
        seen.append(d.type)
        # First the code prompt, then the "try again?" confirm.
        d.accept("AAAAAA") if d.type == "prompt" else d.dismiss()

    phone.page.on("dialog", wrong_then_give_up)
    phone.open_at(pairing.pair_url(app_url, token))
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    phone.page.wait_for_timeout(700)

    assert phone.page.input_value("#in-model-key") == "", "a wrong code filled the keys in anyway"
    assert "confirm" in seen, "a wrong code should say so, not fail silently"

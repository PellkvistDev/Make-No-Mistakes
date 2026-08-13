"""Choosing which model answers, and having that choice stick.

Two faults, and the first one only bites on a phone that has ALSO been paired,
which is every phone that scanned a QR:

  * The setup screen's model menu was written into the vault and nowhere else,
    and the resolver consulted the vault LAST -- below the current provider's
    first model. So the model picked during setup was replaced, before the
    first message, by whatever happened to sort first in the list the desktop
    paired over. Pick gemini-3.5-flash-lite, get told gemini-2.5-flash-lite
    does not exist.

  * Settings offered the model as an <input list> against a datalist, which on
    iOS is a plain text box. Model names are the one thing here nobody can
    spell from memory, and a typo does not fail until the first message.

The two are the same screen disagreeing with itself: setup had a menu, settings
had a box, and neither told the other what had been chosen.
"""

import json

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"

# What a desktop actually pairs over: the full catalogue, in the order the
# provider's API returned it -- which is not the order the phone's own preset
# lists, and is where "2.5" came from.
PAIRED = [{
    "name": "Google AI Studio", "baseUrl": GOOGLE, "key": "sk-g",
    "models": ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3-flash",
               "gemini-3.5-flash", "gemini-3.5-flash-lite"],
}]


def _setup_on_google(phone, model="gemini-3.5-flash-lite", providers=PAIRED):
    """Through the real first-run screen, picking a model deliberately."""
    p = phone.page
    p.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    p.evaluate(
        """() => [...document.querySelectorAll('#setup-presets .api-preset')]
             .find(b => b.textContent.trim() === 'Google AI Studio').click()""")
    p.wait_for_timeout(200)
    p.select_option("#in-model-pick", model)
    if providers is not None:
        p.evaluate("(v) => localStorage.setItem('mnm.providers', v)",
                   json.dumps(providers))
    p.fill("#in-model-key", "sk-g")
    p.fill("#in-gh-token", "ghtoken")
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_selector("#screen-repo:not([hidden])", timeout=15000)
    return phone


def _into_chat(phone):
    p = phone.page
    p.wait_for_selector(".repo-list li", timeout=15000)
    p.click(".repo-list li")
    p.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    return phone


def _model_sent(phone):
    return phone.page.evaluate(
        "() => { const s = window.__sent; return s.length ? s[s.length-1].model : null; }")


def _open_settings(phone):
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    return phone


# ---------------------------------------------------- the reported failure

def test_the_model_chosen_at_setup_is_the_one_that_answers(phone):
    """The bug in one line. Everything else here is about not regressing the
    ways it could come back."""
    _setup_on_google(phone)
    _into_chat(phone)
    phone.reply({"role": "assistant", "content": "ok"})
    phone.send("hello").wait_idle()
    assert _model_sent(phone) == "gemini-3.5-flash-lite"


def test_settings_agrees_with_what_was_chosen(phone):
    """It showed the model the app was about to use, which was the wrong one --
    so the screen was honest and the resolver was not."""
    _setup_on_google(phone)
    _into_chat(phone)
    _open_settings(phone)
    assert phone.page.input_value("#set-model-pick") == "gemini-3.5-flash-lite"


def test_a_paired_provider_does_not_overrule_a_deliberate_choice(phone):
    """The specific collision: the desktop's list starts with a different
    model, and used to win."""
    _setup_on_google(phone)
    _into_chat(phone)
    first = phone.page.evaluate(
        "() => JSON.parse(localStorage.getItem('mnm.providers'))[0].models[0]")
    assert first == "gemini-2.5-flash-lite", "the fixture must actually collide"
    phone.reply({"role": "assistant", "content": "ok"})
    phone.send("hi").wait_idle()
    assert _model_sent(phone) != first


# ------------------------------------------------------------- the control

def test_the_model_is_a_menu_not_a_text_box(phone):
    """On iOS a datalist renders as a plain text field, so the list may as well
    not exist."""
    _setup_on_google(phone)
    _into_chat(phone)
    _open_settings(phone)
    p = phone.page
    assert p.eval_on_selector("#set-model-pick", "e => e.tagName") == "SELECT"
    assert p.eval_on_selector("#set-model-pick", "e => e.hidden") is False
    assert p.eval_on_selector("#set-model", "e => e.hidden") is True


def test_the_menu_offers_the_apis_own_models(phone):
    _setup_on_google(phone)
    _into_chat(phone)
    _open_settings(phone)
    opts = phone.page.evaluate(
        "() => [...document.getElementById('set-model-pick').options].map(o => o.value)")
    assert set(opts) == set(PAIRED[0]["models"])


def test_choosing_from_the_menu_changes_what_is_sent(phone):
    _setup_on_google(phone)
    _into_chat(phone)
    _open_settings(phone)
    p = phone.page
    p.select_option("#set-model-pick", "gemini-3-flash")
    phone.close_settings()
    phone.reply({"role": "assistant", "content": "ok"})
    phone.send("hi").wait_idle()
    assert _model_sent(phone) == "gemini-3-flash"


def test_a_model_the_list_does_not_carry_is_offered_rather_than_swapped(phone):
    """Opening settings must not silently move the chat onto a different model.
    A paired list can lag behind what the desktop is actually using."""
    _setup_on_google(phone, providers=[
        {"name": "Google AI Studio", "baseUrl": GOOGLE, "key": "sk-g",
         "models": ["gemini-3-flash", "gemini-3.5-flash"]}])
    _into_chat(phone)
    _open_settings(phone)
    p = phone.page
    assert p.input_value("#set-model-pick") == "gemini-3.5-flash-lite"
    opts = p.evaluate(
        "() => [...document.getElementById('set-model-pick').options].map(o => o.value)")
    assert "gemini-3.5-flash-lite" in opts


def test_an_api_with_no_model_list_still_gets_a_box_to_type_in(phone):
    """A hand-typed endpoint has no catalogue to offer, and a menu of nothing
    would be a dead end. Same split as the setup screen."""
    _setup_on_google(phone, providers=[
        {"name": "Something else", "baseUrl": "https://x.test/v1", "key": "k"}])
    _into_chat(phone)
    _open_settings(phone)
    p = phone.page
    assert p.eval_on_selector("#set-model", "e => e.hidden") is False
    assert p.eval_on_selector("#set-model-pick", "e => e.hidden") is True
    assert p.input_value("#set-model") == "gemini-3.5-flash-lite"


def test_typing_a_model_for_a_hand_typed_api_still_works(phone):
    _setup_on_google(phone, providers=[
        {"name": "Something else", "baseUrl": "https://x.test/v1", "key": "k"}])
    _into_chat(phone)
    _open_settings(phone)
    p = phone.page
    p.fill("#set-model", "my-own-model")
    p.dispatch_event("#set-model", "change")
    phone.close_settings()
    phone.reply({"role": "assistant", "content": "ok"})
    phone.send("hi").wait_idle()
    assert _model_sent(phone) == "my-own-model"


def test_switching_api_moves_off_a_model_that_one_does_not_have(phone):
    """The existing rule, kept: a glm name against a Gemini key was only ever
    going to 404."""
    _setup_on_google(phone, providers=PAIRED + [
        {"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4", "key": "z",
         "models": ["glm-4.7-flash"]}])
    _into_chat(phone)
    _open_settings(phone)
    p = phone.page
    p.select_option("#set-provider", "Z.AI")
    p.wait_for_timeout(200)
    assert p.input_value("#set-model-pick") == "glm-4.7-flash"

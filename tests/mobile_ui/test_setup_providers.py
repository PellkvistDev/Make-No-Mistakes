"""Setting a phone up by hand, on something other than z.ai.

The screen asked for a "z.ai / model API key", pre-filled glm-4.7-flash, and
offered a base-URL menu holding two z.ai entries. So a phone configured by hand
could not reach anything else -- including the only API that can do voice --
and the only route to a Google key was pairing from a computer that already had
one. The desktop grew a provider catalogue and the phone never got it.
"""

LIVE_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _setup_screen(phone):
    """The first-run screen, as a fresh install lands on it."""
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    return phone


def _chips(phone):
    return phone.page.evaluate(
        """() => [...document.querySelectorAll('#setup-presets .api-preset')]
             .map(b => ({ label: b.textContent.trim(),
                          on: b.classList.contains('on') }))""")


def _pick(phone, label):
    phone.page.evaluate(
        """(label) => [...document.querySelectorAll('#setup-presets .api-preset')]
             .find(b => b.textContent.trim() === label).click()""", label)
    phone.page.wait_for_timeout(200)


def test_the_apis_are_offered_as_a_visible_choice(phone):
    """A closed dropdown hides the fact that there IS a choice, which is how
    this screen spent so long looking like a z.ai-only app."""
    _setup_screen(phone)
    labels = [c["label"] for c in _chips(phone)]
    assert "Z.AI" in labels
    assert "Google AI Studio" in labels, "the API that voice needs"
    assert "Other" in labels
    assert sum(c["on"] for c in _chips(phone)) == 1, "one is always chosen"


def test_choosing_google_sets_its_endpoint_and_models(phone):
    """The bug in one line: this endpoint was not reachable from this screen
    at all."""
    _setup_screen(phone)
    _pick(phone, "Google AI Studio")
    assert phone.page.input_value("#in-base-url") == LIVE_BASE
    models = phone.page.evaluate(
        "() => [...document.getElementById('in-model-pick').options].map(o => o.value)")
    assert models and all(m.startswith("gemini") for m in models)


def test_the_key_field_says_whose_key_it_wants(phone):
    """It said "z.ai / model API key" whatever you were setting up."""
    _setup_screen(phone)
    _pick(phone, "Google AI Studio")
    assert "aistudio.google.com" in phone.page.get_attribute("#setup-key-link", "href")
    _pick(phone, "Z.AI")
    assert "z.ai" in phone.page.get_attribute("#setup-key-link", "href")


def test_other_asks_for_the_endpoint_and_the_model_by_hand(phone):
    """For a preset the URL is decided and the models are a menu; for a
    hand-typed endpoint both are the whole point."""
    _setup_screen(phone)
    _pick(phone, "Z.AI")
    assert phone.page.eval_on_selector("#setup-url-field", "e => e.hidden") is True
    _pick(phone, "Other")
    assert phone.page.eval_on_selector("#setup-url-field", "e => e.hidden") is False
    assert phone.page.eval_on_selector("#in-model", "e => e.hidden") is False
    assert phone.page.eval_on_selector("#in-model-pick", "e => e.hidden") is True


def test_nothing_local_is_offered(phone):
    """Ollama on a computer is not reachable from a phone: a menu entry that
    fails on selection with a connection error and nothing explaining why."""
    _setup_screen(phone)
    assert not any("Ollama" in c["label"] for c in _chips(phone))


def test_an_endpoint_left_empty_is_refused_at_setup(phone):
    """Rather than at the first message, with an error about the key."""
    _setup_screen(phone)
    _pick(phone, "Other")
    p = phone.page
    p.fill("#in-model-key", "k")
    p.fill("#in-gh-token", "t")
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_timeout(300)
    assert "base URL" in p.text_content("#setup-error")
    assert p.eval_on_selector("#screen-setup", "e => e.hidden") is False


def test_a_google_phone_can_be_set_up_end_to_end(phone):
    """The whole point: reach the chat screen on Google without ever touching
    a computer."""
    _setup_screen(phone)
    _pick(phone, "Google AI Studio")
    p = phone.page
    p.fill("#in-model-key", "sk-google")
    p.fill("#in-gh-token", "ghtoken")
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_selector("#screen-repo:not([hidden])", timeout=15000)
    stored = p.evaluate(
        """async () => AgentCore.decryptVault(
             JSON.parse(localStorage.getItem('mnm.vault.v1')), '1234')""")
    assert stored["baseUrl"] == LIVE_BASE
    assert stored["modelKey"] == "sk-google"
    assert stored["model"].startswith("gemini")

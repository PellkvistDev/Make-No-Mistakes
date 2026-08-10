"""The app must not invent a price for someone else's API.

"$0.00", "always free" and "via z.ai" were written into the markup when z.ai was
the only endpoint this app could talk to, so they were simply true. Making the
provider a choice turned every one of them into a claim about a stranger's
billing that nobody had checked -- point it at Google, or at a paid gateway, and
the footer still promised the model was free.

What can honestly be said comes from the catalogue, per model, and the honest
answer for a hand-typed endpoint is nothing at all.
"""

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _providers(name, model, tier, **extra):
    row = {"name": name, "base_url": extra.get("base_url", GOOGLE),
           "models": [model], "builtin": True,
           "local": extra.get("local", False), "tier": tier,
           "key_url": "", "has_key": True}
    return {"providers": [row], "chat_provider": name, "chat_model": model,
            "chat_tier": tier}


def _foot(desktop, providers):
    desktop.boot(providers=providers)
    # The footer is painted from whatever the provider call answered.
    desktop.page.evaluate("() => refreshModelFoot(window.__replies.providers)")
    desktop.page.wait_for_timeout(100)
    return desktop.page.eval_on_selector("#model-foot", "e => e.textContent")


def test_a_free_model_is_named_as_free(desktop):
    got = _foot(desktop, _providers("Google AI Studio", "gemini-2.5-flash", "free"))
    assert got == "gemini-2.5-flash via Google AI Studio — free"


def test_a_model_of_unknown_price_gets_no_claim_either_way(desktop):
    """The bug in one line: this used to end "— always $0.00"."""
    got = _foot(desktop, _providers(
        "OpenRouter", "anthropic/claude-opus-4", "",
        base_url="https://openrouter.ai/api/v1"))
    assert got == "anthropic/claude-opus-4 via OpenRouter"
    assert "$" not in got and "free" not in got.lower()


def test_a_model_whose_free_tier_is_uncertain_is_not_called_free(desktop):
    got = _foot(desktop, _providers("Google AI Studio", "gemini-2.5-pro", "unsure"))
    assert "free" not in got.lower(), got
    assert "check" in got.lower(), got


def test_a_local_model_is_described_as_local_rather_than_free(desktop):
    """Worth distinguishing: a free tier has a quota and a policy that can
    change; a model on your own machine has neither."""
    got = _foot(desktop, _providers(
        "Ollama", "qwen2.5-coder", "local",
        base_url="http://localhost:11434/v1", local=True))
    assert got == "qwen2.5-coder via Ollama — runs on this machine"


def test_no_screen_still_promises_a_price(desktop):
    """A sweep, because these were scattered: the composer footer, the About
    line, the token chip's tooltip and the usage line in Settings all carried
    their own hardcoded "$0.00"."""
    desktop.boot(providers=_providers("OpenRouter", "gpt-4o", "",
                                      base_url="https://openrouter.ai/api/v1"))
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(300)
    text = desktop.page.evaluate("() => document.body.innerText")
    assert "$0.00" not in text
    for el in ("#usage-chip", "#model-foot"):
        assert "$" not in (desktop.page.eval_on_selector(
            el, "e => (e.title || '') + (e.textContent || '')") or "")


def test_the_settings_toggles_agree_with_the_composer_about_what_is_on(desktop):
    """These options appear on two screens, and each screen used to decide for
    itself what an absent value meant -- `!== false` in Settings, `=== true` in
    the composer. A settings object missing the key showed the same switch ON in
    one place and OFF in the other, with Settings contradicting the backend."""
    desktop.boot(providers=_providers("Z.AI", "glm-4.7-flash", "free"))
    # Deliberately absent, the way a config written before the setting existed
    # comes back once someone forgets to extend the hand-written _settings().
    desktop.page.evaluate("""() => {
        delete settings.verify_edits; delete settings.auto_fix_tests;
        syncSettingsUI(); renderComposerOpts(); }""")
    for sheet, composer in (("#opt-verify", "#opt-verify2"),
                            ("#opt-green", "#opt-green2")):
        a = desktop.page.get_attribute(sheet, "aria-checked")
        b = desktop.page.get_attribute(composer, "aria-checked")
        assert a == b == "false", f"{sheet}={a} but {composer}={b}"

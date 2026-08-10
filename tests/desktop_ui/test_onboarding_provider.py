"""The first screen: choosing where the model runs.

It used to say "Connect your free z.ai account" and had one box. z.ai is now one
recommendation among others, so the screen is drawn from the provider catalogue
-- which means these tests are also what stops the desktop and the phone
drifting into offering different things.
"""

CHOICES = {
    "chosen": "",
    "choices": [
        {"key": "zai", "label": "Z.AI", "base_url": "https://api.z.ai/api/paas/v4",
         "model": "glm-4.7-flash", "models": ["glm-4.7-flash"],
         "key_url": "https://z.ai/manage-apikey/apikey-list",
         "blurb": "GLM coding models. A free tier with no card required.",
         "free": "glm-4.7-flash is free to use.", "caveat": "",
         "model_options": [{"name": "glm-4.7-flash", "tier": "free"}],
         "steps": ["Open z.ai and sign in.", "Create a key.", "Paste it below."]},
        {"key": "google", "label": "Google AI Studio",
         "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
         "model": "gemini-2.5-flash", "models": ["gemini-2.5-flash"],
         "key_url": "https://aistudio.google.com/apikey",
         "blurb": "Gemini models. A free tier with no card required.",
         "free": "Flash is free. Your exact quota is shown in AI Studio.",
         "model_options": [{"name": "gemini-2.5-flash", "tier": "free"},
                           {"name": "gemini-2.5-pro", "tier": "unsure"}],
         "caveat": "On the free tier Google may use your prompts to improve "
                   "their models.",
         "steps": ["Open Google AI Studio.", "Click Get API key.", "Paste it below."]},
        {"key": "custom", "label": "Other", "base_url": "", "model": "",
         "models": [], "key_url": "", "free": "", "caveat": "",
         "model_options": [],
         "blurb": "Any OpenAI-compatible API, or a model on this machine.",
         "steps": ["Paste the base URL.", "Paste a key if it needs one.",
                   "Type the model name."]},
    ],
}


def onboard(desktop, **extra):
    desktop.boot(boot={"needsKey": True}, provider_choices=CHOICES, **extra)
    desktop.page.wait_for_selector("#prov-choices .prov-card", timeout=8000)
    return desktop


def cards(desktop):
    return desktop.page.eval_on_selector_all(
        "#prov-choices .prov-card",
        "els => els.map(e => e.dataset.key)")


def test_the_first_screen_offers_every_provider(desktop):
    onboard(desktop)
    assert cards(desktop) == ["zai", "google", "custom"]
    assert desktop.errors == []


def test_no_provider_is_hardcoded_into_the_page(desktop):
    """The screen must come from the catalogue. If a provider's name is written
    into the HTML, the phone and the desktop drift apart the moment either is
    edited on its own."""
    desktop.boot(boot={"needsKey": True},
                 provider_choices={"chosen": "", "choices": [
                     {"key": "only", "label": "Only One", "base_url": "https://x/v1",
                      "model": "m", "models": ["m"], "key_url": "https://x",
                      "blurb": "b", "free": "", "caveat": "", "steps": ["s"]}]})
    desktop.page.wait_for_selector("#prov-choices .prov-card", timeout=8000)
    assert cards(desktop) == ["only"]
    sheet = desktop.page.inner_text("#key-backdrop")
    assert "z.ai" not in sheet.lower(), "a provider name is baked into the page"
    assert desktop.errors == []


def test_the_instructions_change_with_the_choice(desktop):
    onboard(desktop)
    desktop.page.click('.prov-card[data-key="google"]')
    detail = desktop.page.inner_text("#prov-detail")
    assert "Google AI Studio" in detail and "aistudio.google.com" in detail
    desktop.page.click('.prov-card[data-key="zai"]')
    detail = desktop.page.inner_text("#prov-detail")
    assert "z.ai" in detail.lower()
    assert desktop.errors == []


def test_the_free_tier_training_caveat_is_on_screen(desktop):
    """Not in a tooltip and not on a second page. It is the one thing about
    that option someone might mind, and this app sends source code."""
    onboard(desktop)
    desktop.page.click('.prov-card[data-key="google"]')
    assert desktop.page.is_visible(".prov-caveat")
    assert "prompts" in desktop.page.inner_text(".prov-caveat")
    # ...and it does not linger over a provider it does not apply to.
    desktop.page.click('.prov-card[data-key="zai"]')
    assert not desktop.page.is_visible(".prov-caveat")
    assert desktop.errors == []


def test_the_url_and_model_boxes_belong_to_other_alone(desktop):
    onboard(desktop)
    assert desktop.page.is_hidden("#prov-custom")
    desktop.page.click('.prov-card[data-key="custom"]')
    assert desktop.page.is_visible("#prov-custom")
    desktop.page.click('.prov-card[data-key="google"]')
    assert desktop.page.is_hidden("#prov-custom")
    assert desktop.errors == []


def test_the_choice_reaches_the_backend(desktop):
    onboard(desktop, save_setup={"ok": True, "persisted": True,
                                 "provider": "Google AI Studio", "sessions": []})
    desktop.page.click('.prov-card[data-key="google"]')
    desktop.page.fill("#key-input", "g-key")
    desktop.page.click("#key-save")
    desktop.page.wait_for_timeout(400)
    calls = desktop.calls("save_setup")
    assert calls, "the backend was never told which provider was chosen"
    assert calls[0]["args"][0] == "google"
    assert calls[0]["args"][1] == "g-key"
    assert desktop.page.is_hidden("#key-backdrop")
    assert desktop.errors == []


def test_other_sends_its_url_and_model(desktop):
    onboard(desktop, save_setup={"ok": True, "persisted": False,
                                 "provider": "localhost:11434", "sessions": []})
    desktop.page.click('.prov-card[data-key="custom"]')
    desktop.page.fill("#prov-base-url", "http://localhost:11434/v1")
    desktop.page.fill("#prov-model", "llama3")
    desktop.page.click("#key-save")
    desktop.page.wait_for_timeout(400)
    args = desktop.calls("save_setup")[0]["args"]
    assert args[0] == "custom"
    assert args[2] == "http://localhost:11434/v1"
    assert args[3] == "llama3"
    assert desktop.errors == []


def test_a_rejected_setup_says_why_and_stays_put(desktop):
    """A toast slides away while you are still reading the instructions, and
    closing the sheet on a failure would strand you with no way back."""
    onboard(desktop, save_setup={"error": "paste the API's base URL"})
    desktop.page.click('.prov-card[data-key="custom"]')
    desktop.page.click("#key-save")
    desktop.page.wait_for_timeout(400)
    assert desktop.page.is_visible("#key-error")
    assert "base URL" in desktop.page.inner_text("#key-error")
    assert desktop.page.is_visible("#key-backdrop"), "the sheet closed on an error"
    assert desktop.errors == []


def test_the_button_never_ends_up_stuck_when_the_bridge_dies(desktop):
    """The long-standing rule for this screen: it must never look dead."""
    onboard(desktop, save_setup={"__throw": "bridge gone"})
    desktop.page.fill("#key-input", "k")
    desktop.page.click("#key-save")
    desktop.page.wait_for_timeout(500)
    assert desktop.page.is_enabled("#key-save")
    assert desktop.page.inner_text("#key-save").strip() != "Starting…"


def test_the_chooser_is_reachable_from_the_keyboard(desktop):
    onboard(desktop)
    desktop.page.focus('.prov-card[data-key="zai"]')
    desktop.page.keyboard.press("ArrowRight")
    on = desktop.page.eval_on_selector(".prov-card.on", "e => e.dataset.key")
    assert on == "google"
    assert desktop.errors == []


def test_a_model_of_uncertain_price_is_not_labelled_free(desktop):
    """Whether the free tier covers Pro has changed more than once and this app
    cannot be right about it for long. Saying "check AI Studio" is the only
    claim that stays true -- and it must not quietly read as "free"."""
    onboard(desktop)
    desktop.page.click('.prov-card[data-key="google"]')
    tags = desktop.page.eval_on_selector_all(
        ".prov-model", "els => els.map(e => e.textContent)")
    pro = [t for t in tags if "gemini-2.5-pro" in t]
    assert pro, tags
    assert "check AI Studio" in pro[0], pro
    assert "free" not in pro[0].replace("check AI Studio", ""), pro
    assert any("gemini-2.5-flash" in t and "free" in t for t in tags), tags
    assert desktop.errors == []


def test_no_quota_numbers_are_promised_on_screen(desktop):
    """Google cut the free allowances sharply and no longer publishes one table
    that applies to everyone, so a figure baked in here becomes a lie quietly.
    The console is the only thing that knows the truth for a given key."""
    onboard(desktop)
    desktop.page.click('.prov-card[data-key="google"]')
    free_line = desktop.page.inner_text(".prov-free")
    assert "AI Studio" in free_line
    assert not any(ch.isdigit() for ch in free_line), free_line


def test_a_missing_catalogue_still_leaves_a_usable_first_screen(desktop):
    """The bridge failing must not strand the user on the very first screen.

    It used to `return` on an empty reply, which left the sheet showing its
    title, its blurb and a lone "Paste your API key" box -- no chooser, no
    instructions, nothing naming the service the key belonged to. Worse, Start
    then filed the key under "zai" regardless, so a pasted Google key became a
    z.ai key in the wrong environment variable and the first request failed
    with nothing on screen explaining why.
    """
    desktop.boot(boot={"needsKey": True}, provider_choices={})
    desktop.page.wait_for_selector("#prov-choices .prov-card", timeout=8000)

    # Still a real choice on screen, and the manual fields are open.
    assert cards(desktop) == ["custom"]
    assert desktop.page.is_visible("#prov-custom")
    assert desktop.page.is_visible("#prov-base-url")

    # And it says what went wrong rather than looking merely empty.
    assert desktop.page.is_visible("#key-error")
    assert "providers" in desktop.page.inner_text("#key-error").lower()
    assert desktop.errors == []


def test_a_key_saved_without_a_catalogue_is_not_filed_under_zai(desktop):
    """The specific harm above: the wrong provider recorded, silently."""
    desktop.boot(boot={"needsKey": True}, provider_choices={})
    desktop.page.wait_for_selector("#prov-choices .prov-card", timeout=8000)
    desktop.page.fill("#prov-base-url", "http://localhost:11434/v1")
    desktop.page.fill("#prov-model", "qwen2.5-coder")
    desktop.page.fill("#key-input", "sk-whatever")
    desktop.page.click("#key-save")
    desktop.page.wait_for_timeout(300)

    saved = desktop.calls("save_setup")
    assert saved, "Start did not reach the backend"
    assert saved[0]["args"][0] == "custom"
    assert saved[0]["args"][2] == "http://localhost:11434/v1"
    assert desktop.errors == []


def test_the_lede_stops_counting_options_it_is_no_longer_showing(desktop):
    """"Both of the first two have a free tier" over a single card."""
    desktop.boot(boot={"needsKey": True}, provider_choices={})
    desktop.page.wait_for_selector("#prov-choices .prov-card", timeout=8000)
    lede = desktop.page.inner_text("#key-lede").lower()
    assert "first two" not in lede
    assert "openai-compatible" in lede
    assert desktop.errors == []


def test_the_lede_is_left_alone_when_the_catalogue_loads(desktop):
    onboard(desktop)
    assert "first two" in desktop.page.inner_text("#key-lede").lower()
    assert desktop.errors == []

"""Settings lists your APIs; it does not choose between them.

Two things were wrong with the old list and they were the same thing twice.

Every row was a radio that set the current chat's provider -- a second place to
pick a model, next to the picker above the composer that already lists every
model from every API. And the row created at setup was not the same kind of
object as the others: its name, URL and model list were disabled in the form,
it had no delete button, and the save path behind it accepted nothing but a key.
So the one list in the app was both a chooser it did not need to be and an
editor it only half was.
"""

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"

PROVIDERS = {
    "providers": [
        {"name": "Google AI Studio", "base_url": GOOGLE,
         "models": ["gemini-3.6-flash"],
         "all_models": ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
         "quota": {}, "preset": "google", "env_var": "GOOGLE_API_KEY",
         "local": False, "multimodal": True, "tier": "", "key_url": "",
         "has_key": True},
        {"name": "Z.AI", "base_url": "https://api.z.ai/api/paas/v4",
         "models": ["glm-4.7-flash"], "all_models": ["glm-4.7-flash"],
         "quota": {}, "preset": "zai", "env_var": "ZAI_API_KEY",
         "local": False, "multimodal": False, "tier": "free", "key_url": "",
         "has_key": True},
    ],
    "chat_provider": "Z.AI", "chat_model": "glm-4.7-flash", "chat_tier": "free",
    "default_provider": "Google AI Studio", "default_model": "gemini-3.6-flash",
    "vision_provider": "Google AI Studio", "vision_model": "gemini-3.6-flash",
    "vision_pinned": False,
}


def _open(desktop, providers=None):
    desktop.boot(providers=providers or PROVIDERS)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    # Onto the tab these controls live on. Settings opens on General, and the
    # other tabs are hidden rather than unbuilt -- so reading them works from
    # script while clicking them does not, which is the difference between
    # testing the markup and testing what someone can actually do.
    desktop.page.evaluate(
        """() => document.querySelector(
             '.settings-tab-btn[data-tab="models"]').click()""")
    desktop.page.wait_for_timeout(250)
    return desktop


def _rows(desktop):
    return desktop.page.evaluate(
        """() => [...document.querySelectorAll('#api-list .api-row')].map(r => ({
             name: r.querySelector('.provider-name').textContent,
             edit: !!r.querySelector('.api-edit'),
             del: !!r.querySelector('.api-del'),
             radio: !!r.querySelector('.api-radio'),
             badges: [...r.querySelectorAll('.api-badge')].map(b => b.textContent),
           }))""")


def test_every_row_can_be_edited_and_deleted(desktop):
    _open(desktop)
    rows = _rows(desktop)
    assert [r["name"] for r in rows] == ["Google AI Studio", "Z.AI"]
    assert all(r["edit"] and r["del"] for r in rows), \
        "one row used to have no delete button, because it was stored where " \
        "delete_provider could not see it"
    assert desktop.errors == []


def test_the_rows_are_not_a_chooser(desktop):
    _open(desktop)
    assert not any(r["radio"] for r in _rows(desktop))
    # Clicking a row does nothing to the chat. It used to call set_chat_model,
    # which is the model picker's job.
    desktop.page.evaluate(
        "() => document.querySelector('#api-list .api-row').click()")
    desktop.page.wait_for_timeout(150)
    called = desktop.page.evaluate(
        "() => window.__calls.filter(c => c.name === 'set_chat_model').length")
    assert called == 0


def test_what_a_row_is_for_is_written_on_it(desktop):
    """Which API new chats use, and which one reads images. Both used to be
    positional -- "the first one" -- and neither was shown or changeable."""
    _open(desktop)
    by_name = {r["name"]: r["badges"] for r in _rows(desktop)}
    assert by_name["Google AI Studio"] == ["default", "images"]
    assert by_name["Z.AI"] == []


def test_the_setup_row_opens_a_form_with_nothing_greyed_out(desktop):
    _open(desktop)
    desktop.page.evaluate(
        "() => document.querySelector('#api-list .api-edit').click()")
    desktop.page.wait_for_timeout(150)
    state = desktop.page.evaluate(
        """() => ['prov-name', 'prov-url', 'prov-models'].map(id => ({
             id, disabled: document.getElementById(id).disabled,
             value: document.getElementById(id).value }))""")
    assert not any(f["disabled"] for f in state), \
        "the row created at setup had these three disabled -- the only API in " \
        "the app whose URL or name could not be corrected"
    assert dict((f["id"], f["value"]) for f in state)["prov-url"] == GOOGLE


def _labels(desktop, sel):
    return desktop.page.evaluate(
        f"() => [...document.getElementById('{sel}').options].map(o => o.textContent)")


def test_the_default_and_vision_pickers_span_every_api(desktop):
    _open(desktop)
    every = ["gemini-3.6-flash — Google AI Studio",
             "gemini-3.5-flash-lite — Google AI Studio",
             "glm-4.7-flash — Z.AI"]
    assert _labels(desktop, "default-model-select") == every
    # Vision leads with automatic, which is a real answer: each chat reads
    # images with its own API rather than with whichever one setup wrote down.
    vis = _labels(desktop, "vision-model-select")
    assert vis[0].startswith("Automatic")
    assert vis[1:] == every


def test_choosing_a_vision_model_reaches_the_backend(desktop):
    _open(desktop)
    desktop.page.select_option("#vision-model-select",
                               label="glm-4.7-flash — Z.AI")
    desktop.page.wait_for_timeout(200)
    sent = desktop.page.evaluate(
        "() => window.__calls.filter(c => c.name === 'set_vision_model').pop()")
    assert sent["args"] == ["Z.AI", "glm-4.7-flash"]


def test_automatic_is_sent_as_no_provider_at_all(desktop):
    _open(desktop)
    desktop.page.select_option("#vision-model-select", "auto")
    desktop.page.wait_for_timeout(200)
    sent = desktop.page.evaluate(
        "() => window.__calls.filter(c => c.name === 'set_vision_model').pop()")
    assert sent["args"] == ["", ""]


def test_a_provider_named_with_spaces_still_selects_correctly(desktop):
    """The option value cannot be "<provider> <model>": provider names are free
    text, so any separator is something a person can legitimately type. This
    one is chosen to break exactly that."""
    provs = {**PROVIDERS, "providers": [
        {**PROVIDERS["providers"][0], "name": "My Gateway v1 fast"},
        PROVIDERS["providers"][1],
    ], "default_provider": "My Gateway v1 fast"}
    _open(desktop, provs)
    desktop.page.select_option("#default-model-select",
                               label="gemini-3.5-flash-lite — My Gateway v1 fast")
    desktop.page.wait_for_timeout(200)
    sent = desktop.page.evaluate(
        "() => window.__calls.filter(c => c.name === 'set_default_model').pop()")
    assert sent["args"] == ["My Gateway v1 fast", "gemini-3.5-flash-lite"]

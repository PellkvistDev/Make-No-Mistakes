"""What the provider actually refused, next to the allowance this app guesses.

The model menu draws a ring: "4 of 20 requests today". That fraction is
measured against providers.free_limits(), a table in this repository -- a best
effort that goes stale the moment a tier changes, and that has no row at all
for most models. So the number people were being asked to tune their fallback
chain by was, in the bad case, a confident fiction.

A 429 is not a guess. It is the provider saying no, first-hand, and it is the
only measurement here this app makes rather than looks up. So it is shown: on
the model itself, and under the fallback list it exists to justify.
"""

from .conftest import DEFAULT_SETTINGS

# One provider, three models: one refused and still resting, one refused a
# while ago, one clean. Enough to tell the three states apart.
PROVIDERS = {
    "providers": [{
        "name": "Google", "base_url": "https://generativelanguage.googleapis.com",
        "models": ["flash-3.6", "flash-3.5", "flash-lite"],
        "all_models": ["flash-3.6", "flash-3.5", "flash-lite"],
        "preset": "", "env_var": "", "local": False, "multimodal": True,
        "tier": "free", "key_url": "",
        "quota": {
            "flash-3.6": {"used": 4, "rpd": 20, "rpm": 5,
                          "limited": 11, "limited_at": 0, "cooling": True},
            "flash-3.5": {"used": 9, "rpd": None, "rpm": None,
                          "limited": 2, "limited_at": 0, "cooling": False},
            "flash-lite": {"used": 1, "rpd": 50, "rpm": 15,
                           "limited": 0, "limited_at": 0, "cooling": False},
        },
    }],
    "chat_provider": "Google", "chat_model": "flash-3.6", "chat_tier": "free",
}


def _open_general(desktop, **replies):
    replies.setdefault("providers", PROVIDERS)
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(250)
    desktop.page.click('.settings-tab-btn[data-tab="general"]')
    desktop.page.wait_for_timeout(400)
    return desktop


# --------------------------------------------------------------------- #
# under the fallback list

def test_it_lists_what_was_refused_today(desktop):
    _open_general(desktop)
    said = desktop.page.text_content("#fallback-limits")
    assert "flash-3.6" in said and "11" in said
    assert "flash-3.5" in said and "2" in said


def test_the_model_being_skipped_right_now_says_so(desktop):
    """The answer to "why did it switch models on me". A count alone is
    history; `cooling` is the reason the current turn went elsewhere."""
    _open_general(desktop)
    said = desktop.page.text_content("#fallback-limits")
    assert "resting" in said


def test_a_clean_model_is_not_listed(desktop):
    _open_general(desktop)
    assert "flash-lite" not in desktop.page.text_content("#fallback-limits")


def test_nothing_refused_says_so_rather_than_showing_a_blank(desktop):
    """An absent list and a list of nothing read identically, and only one of
    them means the chain is not being exercised."""
    clean = {"providers": [{"name": "Google", "base_url": "u", "models": ["m"],
                            "all_models": ["m"], "preset": "", "env_var": "",
                            "local": False, "multimodal": False, "tier": "free",
                            "key_url": "",
                            "quota": {"m": {"used": 3, "rpd": 20, "rpm": 5,
                                            "limited": 0, "limited_at": 0,
                                            "cooling": False}}}],
             "chat_provider": "Google", "chat_model": "m", "chat_tier": "free"}
    _open_general(desktop, providers=clean)
    assert "Nothing has been rate-limited today" in \
        desktop.page.text_content("#fallback-limits")


def test_a_backend_that_refuses_leaves_the_panel_standing(desktop):
    _open_general(desktop, providers={"__throw": "no bridge"})
    assert desktop.errors == []


# --------------------------------------------------------------------- #
# on the model itself

def _open_model_menu(desktop):
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, providers=PROVIDERS)
    # The chip reads what populateModelPicker fetched; it does not fetch.
    desktop.page.evaluate("() => populateModelPicker()")
    desktop.page.wait_for_timeout(300)
    desktop.page.click("#model-chip")
    desktop.page.wait_for_timeout(250)
    return desktop.page


def test_a_refused_model_is_badged_in_the_menu(desktop):
    p = _open_model_menu(desktop)
    opts = p.query_selector_all("#model-menu .model-opt")
    assert opts, "the model menu did not build"
    badged = {o.query_selector(".model-opt-name").text_content(): o
              for o in opts if o.query_selector(".model-limited")}
    assert set(badged) == {"flash-3.6", "flash-3.5"}


def test_a_model_with_no_known_allowance_can_still_report_refusals(desktop):
    """flash-3.5 has no rpd, so it gets no ring -- and "no ring" used to be
    indistinguishable from "nothing has gone wrong"."""
    p = _open_model_menu(desktop)
    for o in p.query_selector_all("#model-menu .model-opt"):
        if o.query_selector(".model-opt-name").text_content() == "flash-3.5":
            assert o.query_selector(".model-quota") is None
            assert o.query_selector(".model-limited") is not None
            return
    raise AssertionError("flash-3.5 was not in the menu")


def test_the_resting_model_is_marked_apart_from_one_merely_refused(desktop):
    p = _open_model_menu(desktop)
    marks = {}
    for o in p.query_selector_all("#model-menu .model-opt"):
        b = o.query_selector(".model-limited")
        if b:
            marks[o.query_selector(".model-opt-name").text_content()] = (
                b.text_content(), b.get_attribute("class"))
    assert "resting" in marks["flash-3.6"][0]
    assert "cooling" in marks["flash-3.6"][1]
    assert "cooling" not in marks["flash-3.5"][1]


def test_the_ring_says_its_own_number_is_not_what_is_being_enforced(desktop):
    """4 of 20 used AND refused 11 times is the table being wrong, which is
    the one thing the ring on its own can never say."""
    p = _open_model_menu(desktop)
    for o in p.query_selector_all("#model-menu .model-opt"):
        if o.query_selector(".model-opt-name").text_content() == "flash-3.6":
            title = o.query_selector(".model-quota").get_attribute("title")
            assert "Refused 11" in title
            assert "not what your provider is actually enforcing" in title
            return
    raise AssertionError("flash-3.6 was not in the menu")

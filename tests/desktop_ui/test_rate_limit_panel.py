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
# the fallback chain
#
# It was a textarea you typed model names into, one per line. Everything wrong
# with that was the same thing: the app already knew every model you had, and
# it was asking you to retype them from memory. A typo is silent, and the chain
# only matters when the preferred model is refusing -- so a name that does not
# exist is discovered at the worst possible moment, as a second failure on top
# of the first.

def _open_general_with_models(desktop, fallbacks=(), **replies):
    settings = dict(DEFAULT_SETTINGS)
    settings["model_fallbacks"] = list(fallbacks)
    replies.setdefault("providers", PROVIDERS)
    desktop.boot(boot={"settings": settings}, **replies)
    desktop.page.evaluate("() => populateModelPicker()")
    desktop.page.wait_for_timeout(250)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(200)
    desktop.page.click('.settings-tab-btn[data-tab="general"]')
    desktop.page.wait_for_timeout(300)
    return desktop


def _rows(desktop):
    return desktop.page.eval_on_selector_all(
        "#fallback-chain .fallback-row",
        "els => els.map(e => ({"
        " n: e.querySelector('.fallback-num').textContent,"
        " name: e.querySelector('.fallback-name').textContent,"
        " facts: e.querySelector('.fallback-facts').textContent,"
        " unknown: e.classList.contains('fallback-unknown') }))")


def _offered(desktop):
    return desktop.page.eval_on_selector_all(
        "#fallback-add option", "els => els.map(e => e.value).filter(Boolean)")


def test_the_chain_is_a_list_of_real_models_in_order(desktop):
    _open_general_with_models(desktop, ["flash-3.5", "flash-lite"])
    rows = _rows(desktop)
    assert [r["name"] for r in rows] == ["flash-3.5", "flash-lite"]
    assert [r["n"] for r in rows] == ["1", "2"]


def test_it_shows_what_the_chain_starts_from(desktop):
    """"Fall back to" never said what it was falling back FROM, and the answer
    is per-chat."""
    _open_general_with_models(desktop, ["flash-3.5"])
    head = desktop.page.text_content("#fallback-chain .fallback-head")
    assert "flash-3.6" in head
    assert "this chat's model" in head


def test_the_numbers_that_decide_the_order_are_on_the_rows(desktop):
    """They used to be in a different panel. The allowance is this app's count
    against a table it ships; the refusals are what the provider itself said,
    and the second is what tells you the first is wrong."""
    _open_general_with_models(desktop, ["flash-3.5", "flash-lite"])
    facts = {r["name"]: r["facts"] for r in _rows(desktop)}
    assert "2× limited" in facts["flash-3.5"]        # refused, no known allowance
    assert "1/50 today" in facts["flash-lite"]       # allowance, never refused


def test_a_resting_model_says_so_in_the_chain(desktop):
    _open_general_with_models(desktop, ["flash-3.6"])
    assert "resting now" in _rows(desktop)[0]["facts"]


def test_only_models_this_api_serves_are_offered(desktop):
    """A fallback is a different model on the SAME client, so anything else
    cannot work at all -- offering it would be offering a dead entry."""
    _open_general_with_models(desktop)
    assert set(_offered(desktop)) == {"flash-3.5", "flash-lite"}


def test_the_chats_own_model_is_not_offered_as_its_own_fallback(desktop):
    _open_general_with_models(desktop)
    assert "flash-3.6" not in _offered(desktop)


def test_a_model_already_in_the_chain_is_not_offered_twice(desktop):
    _open_general_with_models(desktop, ["flash-3.5"])
    assert _offered(desktop) == ["flash-lite"]


def test_adding_one_saves_it_in_order(desktop):
    _open_general_with_models(desktop, ["flash-3.5"])
    desktop.page.select_option("#fallback-add", "flash-lite")
    desktop.page.wait_for_timeout(250)
    saved = [c for c in desktop.calls("set_setting") if c["args"][0] == "model_fallbacks"]
    assert saved[-1]["args"][1] == ["flash-3.5", "flash-lite"]


def test_removing_one_saves_the_rest(desktop):
    _open_general_with_models(desktop, ["flash-3.5", "flash-lite"])
    desktop.page.locator('#fallback-chain .fallback-row [data-a="del"]').nth(0).click()
    desktop.page.wait_for_timeout(250)
    saved = [c for c in desktop.calls("set_setting") if c["args"][0] == "model_fallbacks"]
    assert saved[-1]["args"][1] == ["flash-lite"]


def test_the_order_can_be_changed(desktop):
    """The order IS the setting -- strongest first -- so it has to be editable
    without retyping the list."""
    _open_general_with_models(desktop, ["flash-3.5", "flash-lite"])
    desktop.page.locator('#fallback-chain .fallback-row [data-a="up"]').nth(1).click()
    desktop.page.wait_for_timeout(250)
    saved = [c for c in desktop.calls("set_setting") if c["args"][0] == "model_fallbacks"]
    assert saved[-1]["args"][1] == ["flash-lite", "flash-3.5"]


def test_the_ends_of_the_chain_cannot_be_moved_off_it(desktop):
    _open_general_with_models(desktop, ["flash-3.5", "flash-lite"])
    disabled = desktop.page.eval_on_selector_all(
        "#fallback-chain .fallback-row button",
        "els => els.filter(e => e.disabled).map(e => e.dataset.a)")
    assert sorted(disabled) == ["down", "up"]


def test_a_name_this_api_does_not_serve_is_called_out(desktop):
    """The whole reason a typo is no longer silent. A dead entry used to be
    discovered only when the preferred model was already refusing."""
    _open_general_with_models(desktop, ["gemini-tpyo"])
    row = _rows(desktop)[0]
    assert row["unknown"] is True
    assert "not served by Google" in row["facts"]
    assert "skipped" in row["facts"]


def test_an_empty_chain_says_what_that_means(desktop):
    """An absent list and a list of nothing read identically, and only one of
    them means "a rate limit just stops me"."""
    _open_general_with_models(desktop)
    assert "A rate limit just means waiting it out" in \
        desktop.page.text_content("#fallback-chain")
    assert _rows(desktop) == []


def test_a_backend_that_refuses_leaves_the_panel_standing(desktop):
    _open_general_with_models(desktop, providers={"__throw": "no bridge"})
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

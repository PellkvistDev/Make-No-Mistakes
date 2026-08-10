"""Finding a setting without knowing which drawer it was filed under.

The sheet holds around forty controls across eight groups. Before this, the only
way in was to remember where something lived -- and the filing was not always
obvious even in hindsight: "Make it green" sat under a heading that said APIs,
and phone pairing under one that said GitHub, because that is how each is
*implemented* rather than how anyone thinks about it.
"""

PROVIDERS = {
    "providers": [
        {"name": "Google AI Studio",
         "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
         "models": ["gemini-2.5-flash"], "builtin": True, "local": False,
         "tier": "free", "key_url": "https://aistudio.google.com/apikey",
         "has_key": True},
    ],
    "chat_provider": "Google AI Studio", "chat_model": "gemini-2.5-flash",
    "chat_tier": "free",
}


def _open(desktop, **replies):
    desktop.boot(providers=PROVIDERS, **replies)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(300)
    return desktop


def _search(desktop, q):
    desktop.page.fill("#settings-search", q)
    desktop.page.wait_for_timeout(200)
    # offsetParent is null for anything display:none, whatever hid it -- which
    # is the point: the test asks what a person can see, not what the code did.
    return desktop.page.evaluate(
        """() => [...document.querySelectorAll('#settings-backdrop section[data-tab]')]
             .filter(s => s.offsetParent !== null)
             .map(s => s.dataset.tabLabel + '/' + (s.querySelector('h3')?.textContent || ''))""")


def test_a_setting_is_found_from_a_word_in_its_name(desktop):
    _open(desktop)
    assert _search(desktop, "backup") == ["Chat/Backups"]
    assert desktop.errors == []


def test_results_say_which_group_they_came_from(desktop):
    """Otherwise search replaces the filing system rather than teaching it, and
    you are back to hunting the second time you want the same switch."""
    _open(desktop)
    assert _search(desktop, "wake word") == ["Voice/Dictation"]


def test_a_row_that_is_hidden_for_a_reason_stays_out_of_the_results(desktop):
    """"Wake phrase" only exists once the wake word is switched on -- its row
    hides itself until then. Search must not drag it out, or you get a control
    with nothing behind it, and a group claiming a match nobody can see."""
    _open(desktop)
    assert desktop.page.eval_on_selector("#wake-word-row", "e => e.hidden")
    assert _search(desktop, "wake phrase") == []


def test_one_word_can_match_several_groups_at_once(desktop):
    """"push" is push-to-talk under Voice and auto-push under GitHub. Both are
    real answers and the old tabs could only ever show one of them."""
    _open(desktop)
    hits = _search(desktop, "push")
    assert len(hits) > 1, hits
    assert any(h.startswith("Voice/") for h in hits), hits
    assert any(h.startswith("GitHub/") for h in hits), hits


def test_matching_a_heading_brings_back_the_whole_group(desktop):
    """Typing "voice" should hand over the Voice settings, not the two rows
    that happen to repeat the word."""
    _open(desktop)
    assert _search(desktop, "voice") == ["Voice/Voice", "Voice/Dictation"]
    rows = desktop.page.evaluate(
        """() => [...document.querySelectorAll('section[data-tab="voice"] .row')]
             .filter(r => r.offsetParent !== null).length""")
    assert rows > 3, f"heading matched but its rows were filtered out ({rows})"


def test_a_search_with_no_matches_says_so(desktop):
    _open(desktop)
    assert _search(desktop, "kryptonite") == []
    assert not desktop.page.eval_on_selector("#settings-noresults", "e => e.hidden")


def test_clearing_the_search_does_not_reveal_panels_that_hide_themselves(desktop):
    """The trap this feature sets. Half the elements in the sheet own their own
    `hidden` -- the API form, the GitHub connected/disconnected pair, the sync
    panels -- so a filter that toggled `hidden` to hide non-matches would, on
    being cleared, unhide every one of them at once and show three
    contradictory states stacked on top of each other."""
    _open(desktop)
    _search(desktop, "backup")
    _search(desktop, "")
    for panel in ("#api-form", "#gh-connected", "#sync-set", "#gh-pr-list"):
        assert desktop.page.eval_on_selector(panel, "e => e.hidden"), panel


def test_clearing_the_search_returns_to_the_tab_that_was_open(desktop):
    _open(desktop)
    desktop.page.click('.settings-tab-btn[data-tab="tasks"]')
    desktop.page.wait_for_timeout(150)
    _search(desktop, "backup")
    assert _search(desktop, "") == ["Tasks/Scheduled & watched tasks"]


def test_reopening_settings_does_not_keep_the_last_filter(desktop):
    """A sheet that opens already hiding most of itself, with the reason for it
    scrolled out of view in the header, looks broken rather than filtered."""
    _open(desktop)
    _search(desktop, "backup")
    desktop.page.click("#settings-close")
    desktop.page.wait_for_timeout(150)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.input_value("#settings-search") == ""
    assert _search(desktop, "") == ["General/Appearance", "General/Notifications"]


def test_escape_clears_the_filter_before_it_closes_the_sheet(desktop):
    """One keypress losing both the filter and the sheet means retyping the
    whole search to fix a typo in it."""
    _open(desktop)
    _search(desktop, "backup")
    desktop.page.focus("#settings-search")
    desktop.page.keyboard.press("Escape")
    desktop.page.wait_for_timeout(200)
    assert desktop.page.input_value("#settings-search") == ""
    assert desktop.page.eval_on_selector("#settings-backdrop", "e => e.hidden") is False


def test_every_group_in_the_rail_has_a_section_behind_it(desktop):
    """A tab with nothing filed under it is a dead end, and a section with no
    tab is unreachable except by searching for it."""
    _open(desktop)
    tabs, sections = desktop.page.evaluate(
        """() => [
            [...document.querySelectorAll('.settings-tab-btn')].map(b => b.dataset.tab),
            [...new Set([...document.querySelectorAll(
                '#settings-backdrop section[data-tab]')].map(s => s.dataset.tab))]]""")
    assert sorted(tabs) == sorted(sections), (tabs, sections)


def test_the_rail_is_a_column_so_the_groups_cannot_wrap(desktop):
    """Eight tabs across the top of a 560px sheet wrapped onto two rows at every
    window size, which reads as two unrelated sets of groups."""
    _open(desktop)
    columns = desktop.page.evaluate(
        """() => new Set([...document.querySelectorAll('.settings-tab-btn')]
             .map(b => Math.round(b.getBoundingClientRect().left))).size""")
    assert columns == 1, f"the rail is not a single column ({columns} lefts)"

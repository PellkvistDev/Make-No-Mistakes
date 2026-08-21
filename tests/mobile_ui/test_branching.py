"""The phone commits as it edits, and it was always committing to main.

Every write from the phone is a commit -- there is no filesystem, so there is
nothing else it could be -- and every one of them landed on whichever branch
the chat was opened on. That is the repository's DEFAULT branch, because it is
the only one the phone can open. So work done here went straight to main,
unreviewed, and on a repo whose Pages site or CI is wired to main it deployed
on the way past.

That is not a preference about workflow. It is the phone being unable to do
the ordinary safe thing, and it is most of why "do it on the computer" was
still the answer for anything real.

These drive the HOST half -- rebinding the GitHub client, the cache that hangs
off it, the header, and what survives a relaunch. The tools themselves are
pinned in tests/test_phone_tools.py.
"""


def _sess(phone, expr):
    return phone.page.evaluate("() => { const session = window.__branch.session();"
                               " return %s; }" % expr)


def _bind(phone, name):
    phone.page.evaluate("(n) => window.__branch.bind(n)", name)
    phone.page.wait_for_timeout(200)
    return phone


def test_a_new_chat_starts_on_the_repos_default_branch(phone):
    phone.setup()
    assert _sess(phone, "session.repo.branch") == "main"
    assert _sess(phone, "session.repo.default_branch") == "main"


def test_the_header_names_the_branch_only_when_it_is_not_the_default(phone):
    """On this device the branch is where every edit is being committed,
    silently, as it is made. That is worth a word in the header -- and worth
    NOT being a word when it is the ordinary case."""
    phone.setup()
    assert phone.page.text_content("#chat-repo-name").strip() == "you/app"
    _bind(phone, "fix-login")
    assert "fix-login" in phone.page.text_content("#chat-repo-name")


def test_switching_branches_rebuilds_the_client_and_its_cache(phone):
    """The whole reason this is not a one-line assignment. makeTools keeps a
    path -> contents cache, and a cache carried across a branch switch is the
    worst possible kind of wrong: reads SUCCEED and hand back the other
    branch's text."""
    phone.setup()
    phone.page.evaluate("() => window.__seedFile('a.py', 'from main\\n')")
    before = phone.page.evaluate(
        "async () => await window.__branch.session().tools.read_file({ path: 'a.py' })")
    assert "from main" in before

    phone.page.evaluate("() => { window.__seedFile('a.py', 'from the branch\\n'); }")
    # Still cached, still the old text -- which is correct until the branch moves.
    stale = phone.page.evaluate(
        "async () => await window.__branch.session().tools.read_file({ path: 'a.py' })")
    assert "from main" in stale

    _bind(phone, "fix-login")
    after = phone.page.evaluate(
        "async () => await window.__branch.session().tools.read_file({ path: 'a.py' })")
    assert "from the branch" in after, after


def test_the_conversation_survives_a_branch_switch(phone):
    """Connecting a repo resets the chat. Switching branches must not: the
    conversation, the transcript and the notes left for the desktop belong to
    the chat, not to the branch it happens to be committing to."""
    phone.setup()
    phone.reply({"role": "assistant", "content": "noted"})
    phone.send("remember this").wait_idle()
    before = _sess(phone, "session.messages.length")
    _bind(phone, "fix-login")
    assert _sess(phone, "session.messages.length") == before
    assert _sess(phone, "session.transcript.length") > 0


def test_the_system_message_is_rewritten_to_the_new_branch(phone):
    """messages[0] is what actually goes on the wire. Updating baseSystem and
    leaving the message alone would tell the model it was still on main for the
    rest of the conversation."""
    phone.setup()
    _bind(phone, "fix-login")
    first = _sess(phone, "session.messages[0].content")
    assert "branch fix-login" in first


def test_the_branch_survives_a_relaunch(phone):
    """Persisted immediately rather than at the end of the turn: the branch is
    where every subsequent commit goes, and a relaunch that came back on the
    old one would carry on writing to it."""
    phone.setup()
    phone.page.evaluate(
        "async () => { window.__branch.bind('fix-login'); await window.__branch.persist(); }")
    phone.page.wait_for_timeout(300)
    phone.relaunch()
    phone.page.fill("#in-unlock-pin", "1234")
    phone.page.click("#btn-unlock")
    phone.page.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    assert _sess(phone, "session.repo.branch") == "fix-login"
    assert _sess(phone, "session.repo.default_branch") == "main"
    assert "fix-login" in phone.page.text_content("#chat-repo-name")


def test_the_branch_tools_are_offered_on_the_main_turn(phone):
    phone.setup()
    phone.reply({"role": "assistant", "content": "ok"})
    phone.send("hello").wait_idle()
    names = phone.page.evaluate(
        "() => (window.__sent[window.__sent.length - 1].tools || [])"
        ".map((t) => t.function.name)")
    assert "new_branch" in names
    assert "open_pull_request" in names


def test_a_sub_agent_gets_no_branch_tools(phone):
    """One of them moving the chat onto another branch underneath the agent
    that dispatched it is not something that conversation could recover from."""
    phone.setup()
    got = _sess(phone, "[typeof session.subTools.new_branch,"
                       " typeof session.subTools.open_pull_request]")
    assert got == ["undefined", "undefined"]


def test_opening_a_pull_request_end_to_end(phone):
    phone.setup()
    _bind(phone, "fix-login")
    said = phone.page.evaluate(
        "async () => await window.__branch.session().tools"
        ".open_pull_request({ title: 'Fix it', body: 'why' })")
    assert "/pull/100" in said
    made = phone.page.evaluate("() => window.__prs")
    assert made[0]["head"] == "fix-login"
    assert made[0]["base"] == "main"
    assert made[0]["draft"] is True


def test_a_second_pull_request_names_the_first(phone):
    phone.setup()
    _bind(phone, "fix-login")
    phone.page.evaluate(
        "async () => await window.__branch.session().tools.open_pull_request({ title: 'Fix it' })")
    again = phone.page.evaluate(
        "async () => await window.__branch.session().tools.open_pull_request({ title: 'Fix it' })")
    assert "already has an open pull request" in again
    assert len(phone.page.evaluate("() => window.__prs")) == 1


def test_nothing_here_throws(phone):
    phone.setup()
    _bind(phone, "fix-login")
    assert phone.errors == []


def test_the_diff_base_survives_a_relaunch(phone):
    """review_changes diffs against where the chat started. Re-taking that on
    launch would silently move it to wherever the branch is NOW -- so
    everything committed before the app was killed would stop showing up,
    which on a phone is the ordinary way a chat ends."""
    phone.setup()
    phone.page.evaluate("""async () => {
      window.__branch.session().baseRef = "startedhere";
      await window.__branch.persist();
    }""")
    phone.page.wait_for_timeout(300)
    phone.relaunch()
    phone.page.fill("#in-unlock-pin", "1234")
    phone.page.click("#btn-unlock")
    phone.page.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    assert _sess(phone, "session.baseRef") == "startedhere"

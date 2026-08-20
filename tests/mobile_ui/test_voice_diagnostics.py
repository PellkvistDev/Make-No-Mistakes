"""What the spoken session actually sent, readable off the phone.

"It says it can't write files or send out agents" is a claim about the tool
list, and there was no way to check it from the device. Two rounds went into
reasoning about the source instead -- first blaming a stale bundle, then
verifying the deployed bundle was correct and still not knowing what the phone
had done with it. The build stamp only proves index.html is current; app.js and
agent-core.js arrive separately, and neither says what went on the wire.

So the setup message now reports itself into the diagnostics panel: how many
tools were declared and which, whether the session was a resume, and the close
code if the socket was refused. These are the numbers that would have answered
it on the first try.
"""


def _to_chat(phone):
    p = phone.page
    p.fill("#in-model-key", "modelkey")
    p.fill("#in-gh-token", "ghtoken")
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_selector("#screen-repo:not([hidden])", timeout=15000)
    p.wait_for_selector(".repo-list li", timeout=15000)
    p.click(".repo-list li")
    p.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    return phone


def _diag(phone):
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    phone.page.wait_for_timeout(500)
    return phone.page.text_content("#set-diag")


def test_before_voice_is_opened_it_says_so_rather_than_lying(phone):
    """An empty list and "not opened yet" are different facts, and reporting
    the first for the second is how instrumentation misleads."""
    _to_chat(phone)
    assert "voice not opened yet" in _diag(phone)


def test_it_reports_the_tools_the_session_was_given(phone):
    """The whole point: the list is read out of the message that was built,
    not out of the source that was supposed to build it."""
    p = _to_chat(phone).page
    # Build a setup exactly as voiceOpen does, without needing a socket.
    # The expected count comes from the page, not from a literal here: what is
    # being checked is that the diagnostic reports the message that was BUILT,
    # and a number written down in this file would instead break every time a
    # tool is added -- which is a test about the tool list, not the diagnostic.
    n = p.evaluate("""() => {
      const AC = window.AgentCore;
      const schemas = [...AC.TOOL_SCHEMAS, AC.VIEW_IMAGE_SCHEMA,
                       AC.NEEDS_DESKTOP_SCHEMA, ...(AC.WORKER_SCHEMAS || [])];
      const s = AC.liveSetup(AC.LIVE_MODEL, AC.LIVE_VOICE_PROMPT, schemas, {});
      const d = ((s.setup.tools || [])[0] || {}).functionDeclarations || [];
      window.__diagPoke(d.map((x) => x.name), s.setup.systemInstruction.parts[0].text.length);
      return d.length;
    }""")
    out = _diag(phone)
    assert "dispatch_worker" in out
    assert "write_file" in out
    assert n > 10, n            # a plausible list, not an empty one
    assert f"{n} \u2014" in out, out


def test_it_reports_whether_the_prompt_still_mentions_workers(phone):
    """A tool declared but never mentioned in the prompt is a tool the model
    does not reach for -- which looks identical to one that is missing."""
    _to_chat(phone)
    assert "mentions workers" in _diag(phone)


def test_a_refused_socket_reports_its_close_code(phone):
    """A rejected setup names the field it objected to. Discarding that leaves
    "kept losing the connection" as the only thing the app can say about a
    message it built wrong itself."""
    p = _to_chat(phone).page
    p.evaluate("() => window.__diagClose('1007 invalid setup: unknown field')")
    out = _diag(phone)
    assert "1007" in out and "invalid setup" in out

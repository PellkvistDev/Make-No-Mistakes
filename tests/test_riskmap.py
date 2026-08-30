"""The risk map: which files bite, and what changes alongside what.

Built against real git repositories rather than a fake log, because every
defect found while writing this module was in the parsing or in the choice of
metric -- neither of which a fake would have exercised. The repos here are
tiny and made on the fly.
"""

import subprocess
from types import SimpleNamespace

import pytest

from glmcode import riskmap
from glmcode.agent import Agent


def _git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=str(repo), check=True,
                   capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _no_cache():
    riskmap._cache.clear()
    yield
    riskmap._cache.clear()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Tester")
    return r


def commit(repo, subject, files, body=""):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    msg = subject if not body else f"{subject}\n\n{body}"
    _git(repo, "commit", "-q", "-m", msg)


def _bulk(repo, n, prefix="change"):
    """Enough history that the module is willing to say anything."""
    for i in range(n):
        commit(repo, f"{prefix} {i}", {"filler.txt": str(i)})


# --------------------------------------------------------------------- #
# "I could not find out" is not "nothing to worry about"

def test_a_directory_that_is_not_a_repo_says_so(tmp_path):
    why = riskmap.unavailable(tmp_path)
    assert why and "not a git repository" in why


def test_that_refusal_reaches_the_output_and_does_not_read_as_safe(tmp_path):
    text = riskmap.describe("anything.py", tmp_path)
    assert "not known" in text.lower()
    assert "low" not in text.lower().split("risk:")[-1][:20]


def test_too_little_history_is_refused_rather_than_ranked(repo):
    commit(repo, "one", {"a.py": "x"})
    why = riskmap.unavailable(repo)
    assert why and "too few" in why


def test_enough_history_lifts_the_refusal(repo):
    _bulk(repo, riskmap.MIN_COMMITS + 1)
    assert riskmap.unavailable(repo) == ""


# --------------------------------------------------------------------- #
# Partial history is not no history

def test_a_shallow_clone_still_answers_but_calls_its_numbers_floors(repo, tmp_path):
    """The first version REFUSED here, and this repository's own checkout is
    what showed that up: nearly every checkout an agent works in is shallow --
    CI runners, cloud sessions, --depth clones. A tool that says "I cannot
    tell you" in the environment it mostly runs in is not cautious, it is
    absent."""
    _bulk(repo, 30)
    clone = tmp_path / "shallow"
    # Deep enough to clear MIN_COMMITS: a clone too shallow to say anything is
    # refused for that reason, which is a different rule being tested above.
    subprocess.run(["git", "clone", "-q", "--depth", "15",
                    f"file://{repo}", str(clone)], check=True,
                   capture_output=True, text=True)
    assert riskmap.is_shallow(clone)
    assert riskmap.unavailable(clone) == "", "shallow must not be a refusal"
    note = riskmap.caveat(clone)
    assert "floor" in note.lower() and "truncated" in note.lower()


# --------------------------------------------------------------------- #
# Parsing

def test_the_file_list_is_not_confused_with_the_commit_body(repo):
    """This repo's own commit bodies are essays full of file names. Without a
    trailing field separator the two share one blob and have to be told apart
    by guessing what looks like a path."""
    commit(repo, "a change", {"real.py": "x"},
           body="This body mentions glmcode/agent.py and mobile/app.js on purpose.")
    (c,) = [c for c in riskmap.history(repo) if c["subject"] == "a change"]
    assert c["files"] == ["real.py"]
    assert "glmcode/agent.py" in c["body"]


# --------------------------------------------------------------------- #
# Fix density and reverts: what a commit DID, not what it discussed

def test_a_body_that_discusses_a_revert_is_not_a_revert(repo):
    _bulk(repo, riskmap.MIN_COMMITS)
    commit(repo, "Add the thing", {"a.py": "x"},
           body="An earlier approach was tried and reverted; this replaces it.")
    stats = riskmap.file_stats("a.py", repo)
    assert stats["reverts"] == 0, "prose about a revert is not a revert"


def test_git_s_own_revert_markers_are_recognised(repo):
    _bulk(repo, riskmap.MIN_COMMITS)
    commit(repo, "Add the thing", {"a.py": "x"})
    commit(repo, 'Revert "Add the thing"', {"a.py": ""},
           body="This reverts commit 0123456789abcdef0123456789abcdef01234567.")
    assert riskmap.file_stats("a.py", repo)["reverts"] == 1


def test_a_fix_is_read_from_the_subject(repo):
    _bulk(repo, riskmap.MIN_COMMITS)
    commit(repo, "fix the crash in a.py", {"a.py": "1"})
    commit(repo, "fix a.py again", {"a.py": "2"})
    stats = riskmap.file_stats("a.py", repo)
    assert stats["fixes"] == 2 and stats["changes"] == 2


# --------------------------------------------------------------------- #
# A signal this repo cannot support is SAID, never silently rounded to "fine"

def test_a_repo_whose_subjects_never_say_fix_reports_the_signal_as_unusable(repo):
    _bulk(repo, 20, prefix="A change described rather than classified")
    quality = riskmap.signal_quality(repo)
    assert quality["fixes"] is False
    assert "fix-density says nothing" in quality["note"]
    assert quality["coupling"] is True, "coupling does not depend on messages"


def test_an_unreadable_fix_signal_never_produces_a_LOW_verdict(repo):
    """'This file looks fine', derived from a fix count that cannot be read,
    is the reassuring confident wrong answer this module exists to avoid."""
    _bulk(repo, 20, prefix="A change described rather than classified")
    for i in range(4):
        commit(repo, f"Another described change {i}", {"a.py": str(i)})
    text = riskmap.describe("a.py", repo)
    assert "risk: UNKNOWN" in text
    assert "risk: LOW" not in text


# --------------------------------------------------------------------- #
# Coupling by lift

def test_a_file_that_changes_with_everything_is_not_everyone_s_partner(repo):
    """CLAUDE.md changes in 42 of this repository's 51 commits, so it
    co-occurs with every file at ~100%. Raw co-occurrence would call it every
    file's closest sibling -- true, and useless."""
    for i in range(14):
        commit(repo, f"c{i}", {"NOTES.md": str(i), f"unrelated{i}.py": str(i)})
    for i in range(6):
        commit(repo, f"pair {i}", {"NOTES.md": str(i), "a.py": str(i), "b.py": str(i)})
    partners = [r["path"] for r in riskmap.coupled_with("a.py", repo)]
    assert "b.py" in partners
    assert "NOTES.md" not in partners, "the changes-with-everything file must be filtered out"


def test_coupling_is_ranked_by_confidence(repo):
    """Lift is the FILTER; confidence is the ranking. The question a reader is
    asking is 'I changed X, how likely is it that Y needs changing too'."""
    # Filler first, so a.py is NOT in every commit: lift is
    # P(sibling|a.py)/P(sibling), and a file present everywhere makes both
    # halves equal and every lift exactly 1.0 -- correctly, since knowing it
    # changed would tell you nothing.
    _bulk(repo, 14)
    for i in range(10):
        commit(repo, f"c{i}", {"a.py": str(i), "always.py": str(i)})
    for i in range(3):
        commit(repo, f"d{i}", {"a.py": f"x{i}", "rare.py": str(i)})
    rows = riskmap.coupled_with("a.py", repo)
    assert rows[0]["path"] == "always.py"
    assert rows[0]["confidence"] > rows[-1]["confidence"]


def test_a_sweeping_commit_does_not_create_couplings(repo):
    for i in range(14):
        commit(repo, f"c{i}", {f"f{i}.py": str(i)})
    for n in range(3):
        big = {f"g{i}.py": f"pass {n}"
               for i in range(riskmap.MAX_FILES_FOR_COUPLING + 5)}
        commit(repo, f"reformat everything {n}", big)
    assert riskmap.coupled_with("g0.py", repo) == []


# --------------------------------------------------------------------- #
# The forgotten sibling

def test_the_sibling_that_did_not_follow_is_named(repo):
    _bulk(repo, 14)
    for i in range(8):
        commit(repo, f"c{i}", {"desktop.py": str(i), "phone.js": str(i)})
    rows = riskmap.forgotten_siblings(["desktop.py"], repo)
    assert rows and rows[0]["path"] == "phone.js"
    assert rows[0]["because"] == "desktop.py"
    assert rows[0]["confidence"] == 1.0


def test_a_sibling_that_was_changed_too_is_not_reported(repo):
    _bulk(repo, 14)
    for i in range(8):
        commit(repo, f"c{i}", {"desktop.py": str(i), "phone.js": str(i)})
    assert riskmap.forgotten_siblings(["desktop.py", "phone.js"], repo) == []


def test_no_history_means_no_claim(tmp_path):
    assert riskmap.forgotten_siblings(["a.py"], tmp_path) == []


# --------------------------------------------------------------------- #
# The agent side

class _Events:
    def __init__(self):
        self.infos = []

    def info(self, msg):
        self.infos.append(msg)


def _agent(repo, wrote, enabled=True):
    return SimpleNamespace(
        workdir=repo,
        cfg=SimpleNamespace(sibling_check=enabled),
        conversational=False,
        allow_subagents=True,
        messages=[],
        events=_Events(),
        _sibling_checked=False,
        _turn_wrote_paths=set(wrote),
    )


def test_a_strong_pairing_nudges_the_model_and_tells_the_user(repo):
    _bulk(repo, 14)
    for i in range(8):
        commit(repo, f"c{i}", {"desktop.py": str(i), "phone.js": str(i)})
    a = _agent(repo, {"desktop.py"})
    assert Agent._sibling_nudge(a) is True
    assert any("phone.js" in m for m in a.events.infos)
    (msg,) = a.messages
    assert "phone.js" in msg["content"]
    assert "correlation" in msg["content"], "must not read as a rule"
    assert "do not edit that file just because this asked" in msg["content"]


def test_it_fires_at_most_once_per_turn(repo):
    _bulk(repo, 14)
    for i in range(8):
        commit(repo, f"c{i}", {"desktop.py": str(i), "phone.js": str(i)})
    a = _agent(repo, {"desktop.py"})
    assert Agent._sibling_nudge(a) is True
    assert Agent._sibling_nudge(a) is False
    assert len(a.messages) == 1


def test_a_turn_that_wrote_nothing_is_never_checked(repo):
    _bulk(repo, 14)
    for i in range(8):
        commit(repo, f"c{i}", {"desktop.py": str(i), "phone.js": str(i)})
    a = _agent(repo, set())
    assert Agent._sibling_nudge(a) is False
    assert a.messages == []


def test_turning_it_off_costs_nothing(repo):
    _bulk(repo, 14)
    for i in range(8):
        commit(repo, f"c{i}", {"desktop.py": str(i), "phone.js": str(i)})
    a = _agent(repo, {"desktop.py"}, enabled=False)
    assert Agent._sibling_nudge(a) is False
    assert a.messages == [] and a.events.infos == []


def test_a_weak_pairing_is_said_but_does_not_spend_a_request(repo):
    """The line to the user is free; a model round trip is not."""
    _bulk(repo, 14)
    for i in range(6):
        commit(repo, f"c{i}", {"a.py": str(i), "noise.py": str(i)})
    for i in range(6):
        commit(repo, f"solo {i}", {"a.py": f"x{i}"})
    a = _agent(repo, {"a.py"})
    fired = Agent._sibling_nudge(a)
    top = riskmap.forgotten_siblings(["a.py"], repo)
    if top and top[0]["confidence"] < 0.7:
        assert fired is False
        assert a.messages == []
        assert a.events.infos, "the user is still told, because that is free"


def test_a_broken_git_never_breaks_the_turn(tmp_path):
    a = _agent(tmp_path, {"a.py"})
    assert Agent._sibling_nudge(a) is False
    assert a.messages == []

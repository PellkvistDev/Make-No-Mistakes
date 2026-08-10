"""Starting a chat, which is the first thing anyone does and was the longest way round.

The sheet opened on a settings toggle, offered two equally-blue primary buttons,
and put the commonest action -- go back to a project you were working in
yesterday -- behind the OS folder picker, every time, despite every session
already carrying the path.
"""

import time

NOW = int(time.time() * 1000)
SESSIONS = [
    {"id": "a", "title": "Fix the composer", "updated": NOW - 1_000,
     "cwd": "/home/jo/code/make-no-mistakes"},
    {"id": "b", "title": "Invoice parser", "updated": NOW - 90_000,
     "cwd": "/home/jo/work/invoices"},
    {"id": "c", "title": "More on the composer", "updated": NOW - 200_000,
     "cwd": "/home/jo/code/make-no-mistakes"},   # same folder as (a)
    {"id": "d", "title": "Blog", "updated": NOW - 900_000,
     "cwd": "/home/jo/personal/blog"},
]


def _open(desktop, sessions=SESSIONS, **replies):
    desktop.boot(boot={"sessions": list(sessions)}, **replies)
    desktop.page.evaluate("() => showNewChatChooser()")
    desktop.page.wait_for_timeout(250)
    return desktop


def _recent(desktop):
    return desktop.page.evaluate(
        """() => [...document.querySelectorAll('.newchat-recent-row')]
             .map(r => r.title)""")


def test_the_folders_you_work_in_are_offered_first(desktop):
    _open(desktop)
    assert _recent(desktop)[0] == "/home/jo/code/make-no-mistakes"
    assert desktop.errors == []


def test_a_folder_with_several_chats_is_listed_once(desktop):
    """Two chats in one project is the normal case, not a reason to show the
    folder twice and push a different one off the list."""
    got = _recent(_open(desktop))
    assert got == sorted(set(got), key=got.index)
    assert got.count("/home/jo/code/make-no-mistakes") == 1


def test_recent_folders_are_ordered_by_when_you_last_touched_them(desktop):
    assert _recent(_open(desktop)) == [
        "/home/jo/code/make-no-mistakes",
        "/home/jo/work/invoices",
        "/home/jo/personal/blog",
    ]


def test_opening_a_recent_folder_skips_the_folder_picker(desktop):
    """The whole point: new_session() opens an OS dialog, new_session_in()
    does not."""
    _open(desktop)
    desktop.page.evaluate("() => document.querySelector('.newchat-recent-row').click()")
    desktop.page.wait_for_timeout(200)
    assert [c["args"] for c in desktop.calls("new_session_in")] == \
        [["/home/jo/code/make-no-mistakes", True]]
    assert desktop.calls("new_session") == []


def test_a_folder_that_has_since_been_deleted_reopens_the_sheet(desktop):
    """Folders get moved and renamed between one week and the next. Closing the
    sheet on the error would leave nothing on screen but a toast, with the
    folder picker two clicks away again."""
    _open(desktop)
    desktop.reply("new_session_in", {"error": "folder not found: /home/jo/work/invoices"})
    desktop.page.evaluate("() => document.querySelectorAll('.newchat-recent-row')[1].click()")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.eval_on_selector("#newchat-backdrop", "e => e.hidden") is False
    assert "not found" in desktop.page.inner_text("#toasts")


def test_the_backup_switch_still_applies_to_a_recent_folder(desktop):
    """It moved to the bottom of the sheet, which must not turn it into
    decoration."""
    _open(desktop)
    desktop.page.evaluate("() => document.getElementById('newchat-backup').click()")
    desktop.page.evaluate("() => document.querySelector('.newchat-recent-row').click()")
    desktop.page.wait_for_timeout(200)
    assert desktop.calls("new_session_in")[0]["args"][1] is False


def test_a_first_run_with_no_history_hides_the_list_rather_than_showing_none(desktop):
    _open(desktop, sessions=[])
    assert desktop.page.eval_on_selector("#newchat-recent-wrap", "e => e.hidden")
    assert desktop.page.eval_on_selector("#newchat-folder", "e => e.offsetParent !== null")


def test_only_one_action_in_the_sheet_looks_primary(desktop):
    """"Clone" and "Open a folder…" were both filled blue, so the sheet gave
    equal weight to the rare path and the common one -- and led with an empty
    text box for the rare one."""
    _open(desktop)
    # checkVisibility rather than offsetParent: "Clone" now lives inside a
    # collapsed <details>, and a closed disclosure still reports a live
    # offsetParent and a non-zero bounding box for everything inside it.
    primaries = desktop.page.evaluate(
        """() => [...document.querySelectorAll('#newchat-backdrop .btn-primary')]
             .filter(b => b.checkVisibility({ contentVisibilityAuto: true }))
             .map(b => b.id)""")
    assert primaries == ["newchat-folder"], primaries


def test_shortening_a_path_keeps_its_own_separators(desktop):
    """It always rejoined with a backslash, so a POSIX path was displayed as
    one that cannot exist on the machine it was read from."""
    desktop.boot()
    assert desktop.page.evaluate(
        "() => shortPath('/home/jo/code/make-no-mistakes')") == "…/code/make-no-mistakes"
    assert desktop.page.evaluate(
        r"() => shortPath('C:\\Users\\jo\\code\\mnm')") == "…\\code\\mnm"

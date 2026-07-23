"""Scheduled & watched tasks: validation and the pure due-time logic (interval,
daily, watch). No threads, no real clock -- `now` and folder signatures are
injected."""

from datetime import datetime

from glmcode import scheduler as sched


def _task(**kw):
    base = {"prompt": "do it", "cwd": "/proj",
            "schedule": {"kind": "interval", "minutes": 30}}
    base.update(kw)
    return sched.normalize_task(base)


# --------------------------------------------------------- validation --

def test_normalize_rejects_missing_pieces():
    assert sched.normalize_task({"prompt": "", "cwd": "/p",
                                 "schedule": {"kind": "interval", "minutes": 30}}) is None
    assert sched.normalize_task({"prompt": "x", "cwd": "",
                                 "schedule": {"kind": "interval", "minutes": 30}}) is None
    assert sched.normalize_task({"prompt": "x", "cwd": "/p",
                                 "schedule": {"kind": "nope"}}) is None


def test_normalize_enforces_min_interval():
    assert sched.normalize_task({"prompt": "x", "cwd": "/p",
                                 "schedule": {"kind": "interval", "minutes": 1}}) is None
    ok = sched.normalize_task({"prompt": "x", "cwd": "/p",
                               "schedule": {"kind": "interval", "minutes": 5}})
    assert ok and ok["schedule"]["minutes"] == 5


def test_normalize_validates_daily_time():
    assert sched.normalize_task({"prompt": "x", "cwd": "/p",
                                 "schedule": {"kind": "daily", "at": "25:00"}}) is None
    ok = sched.normalize_task({"prompt": "x", "cwd": "/p",
                               "schedule": {"kind": "daily", "at": "09:30"}})
    assert ok["schedule"]["at"] == "09:30"


def test_normalize_defaults_name_and_id():
    t = _task(prompt="Fix the flaky login test in the auth module please")
    assert t["id"].startswith("task_")
    assert t["name"]                       # auto-derived from the prompt


# -------------------------------------------------------- interval --

def test_interval_due_after_period():
    now = 1_000_000.0
    t = _task(last_run=now - 40 * 60)      # 40 min ago, period 30
    assert sched.is_due(t, now)
    t2 = _task(last_run=now - 10 * 60)     # only 10 min ago
    assert not sched.is_due(t2, now)


def test_interval_never_run_is_due():
    assert sched.is_due(_task(last_run=0), 1_000_000.0)


def test_disabled_task_never_due():
    t = _task(last_run=0, enabled=False)
    assert not sched.is_due(t, 1_000_000.0)


# -------------------------------------------------------- daily --

def test_daily_due_only_after_time_and_once():
    day = datetime(2026, 7, 23, 12, 0, 0)          # noon
    now = day.timestamp()
    t = sched.normalize_task({"prompt": "x", "cwd": "/p",
                              "schedule": {"kind": "daily", "at": "09:00"}})
    t["last_run"] = 0
    assert sched.is_due(t, now)                     # past 09:00, not run today
    # after running today, not due again the same day
    t["last_run"] = datetime(2026, 7, 23, 9, 0, 1).timestamp()
    assert not sched.is_due(t, now)
    # before the time, not due
    early = datetime(2026, 7, 23, 8, 0, 0).timestamp()
    t["last_run"] = 0
    assert not sched.is_due(t, early)


# -------------------------------------------------------- watch --

def test_watch_fires_on_change_not_first_seen():
    t = sched.normalize_task({"prompt": "x", "cwd": "/p",
                              "schedule": {"kind": "watch", "path": "/p"}})
    t["last_sig"] = ""
    assert not sched.is_due(t, sig="abc")          # first observation -> baseline only
    t["last_sig"] = "abc"
    assert not sched.is_due(t, sig="abc")          # unchanged
    assert sched.is_due(t, sig="def")              # changed -> fire


def test_due_tasks_uses_injected_signature():
    watch = sched.normalize_task({"prompt": "x", "cwd": "/p",
                                  "schedule": {"kind": "watch", "path": "/p"}})
    watch["last_sig"] = "old"
    interval = _task(last_run=0)
    due = sched.due_tasks([watch, interval], now=1_000_000.0,
                          sig_func=lambda p: "new")
    ids = {t["id"] for t in due}
    assert watch["id"] in ids and interval["id"] in ids


def test_folder_signature_changes_with_contents(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    s1 = sched.folder_signature(str(tmp_path))
    (tmp_path / "b.txt").write_text("yo", encoding="utf-8")
    s2 = sched.folder_signature(str(tmp_path))
    assert s1 and s2 and s1 != s2
    assert sched.folder_signature(str(tmp_path / "nope")) == ""


def test_describe_labels():
    assert "every 30 min" in sched.describe(_task())
    daily = sched.normalize_task({"prompt": "x", "cwd": "/p",
                                  "schedule": {"kind": "daily", "at": "09:00"}})
    assert sched.describe(daily) == "daily at 09:00"

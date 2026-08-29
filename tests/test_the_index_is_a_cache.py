"""The index is a cache, and a cache whose loss is permanent is not one.

Every chat lives in its own encrypted file under ``chats/``. One small file
beside them, ``index.json``, is the only thing that names them -- nothing in
normal operation enumerates that directory. So the index is the single point
of failure for the whole store, and it was read by code that could not tell
these three apart:

  * there is no index yet (a first device),
  * there is one and we did not get it (GitHub down, no signal),
  * there is one and it will not decrypt.

The second was answered with "you have no chats", which is a lie the user acts
on. The third was worse: it returned an empty index carrying the REAL sha, so
the next save wrote that emptiness straight over the good index -- leaving
perfectly readable ciphertext that no device could name again.

``rebuild_index`` is the other half. If the bodies are the truth, it must be
possible to turn them back into a list.
"""

import base64
import hashlib
import json

import pytest

from glmcode.githubsync import GitHubError
from glmcode import syncstore

needs_crypto = pytest.mark.skipif(not syncstore.crypto_available(),
                                  reason="cryptography unavailable")

PASS = "correct-horse-battery-staple"


class FakeGitHub:
    """In-memory GitHub with the two things this file is about: directory
    listings, and errors that say WHICH failure they were."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.fail_get_with: int | None = None   # status to raise on any GET

    def api(self, method, path, token, body=None):
        p = path.split("?", 1)[0]
        if "/contents/" in p:
            fpath = p.split("/contents/", 1)[1]
            if method == "GET":
                if self.fail_get_with:
                    raise GitHubError(f"GitHub API error {self.fail_get_with}.",
                                      self.fail_get_with)
                kids = [k for k in self.files
                        if k.startswith(fpath.rstrip("/") + "/")]
                if kids and fpath not in self.files:
                    return [{"name": k.split("/")[-1], "path": k, "type": "file"}
                            for k in sorted(kids)]
                if fpath not in self.files:
                    raise GitHubError("Not found (the token may not have access).", 404)
                text = self.files[fpath]
                return {"content": base64.b64encode(text.encode()).decode(),
                        "sha": "sha-" + hashlib.sha1(text.encode()).hexdigest()[:8]}
            if method == "PUT":
                self.files[fpath] = base64.b64decode(body["content"]).decode()
                return {"commit": {"sha": "c"}}
            if method == "DELETE":
                self.files.pop(fpath, None)
                return {}
        if "/git/ref/heads/" in p:
            return {"object": {"sha": "branchsha"}}
        raise GitHubError(f"unexpected {method} {p}", 500)


@pytest.fixture
def store():
    gh = FakeGitHub()
    repo = syncstore.StateRepo("T", "o", "r", api=gh.api)
    _key, st, _created = syncstore.open_sync(repo, PASS)
    st.gh = gh
    return st


def _chat(cid, **kw):
    return {"id": cid, "title": cid.title(), "messages": [], **kw}


# ---------------------------------------------- reading an index that is gone

@needs_crypto
def test_no_index_yet_is_an_empty_one(store):
    """A first device against a fresh store must not be an error."""
    assert store.list() == []


@needs_crypto
def test_github_being_down_is_not_you_having_no_chats(store):
    """The most common of the three failures, and the one that read worst.

    "0 chats" is a sentence about your data. "Couldn't reach GitHub" is a
    sentence about the network, and only one of them is true here.
    """
    store.save(_chat("a"))
    store.gh.fail_get_with = 500
    with pytest.raises(syncstore.SyncError) as e:
        store.list()
    assert "index" in str(e.value).lower()


@needs_crypto
def test_a_404_is_still_treated_as_absent(store):
    """The distinction has to be drawn on the STATUS, not on there being an
    error at all -- or a first run would raise instead of starting empty."""
    store.gh.fail_get_with = 404
    assert store.list() == []


# ------------------------------------------- the destructive case: never write

@needs_crypto
def test_an_unreadable_index_is_never_overwritten(store):
    """The one that lost chats.

    An index that will not decrypt used to be reported as empty WITH its real
    sha, so the next save replaced it with a single row and every other chat
    became unfindable. Refusing costs one failed sync; guessing cost the list.
    """
    for cid in ("a", "b", "c"):
        store.save(_chat(cid))
    before = store.gh.files["index.json"]
    other = syncstore.derive_key("some other store's key", b"0123456789abcdef")
    store.gh.files["index.json"] = json.dumps(syncstore.aes_encrypt(
        {"v": 1, "chats": [], "deleted": []}, other))

    with pytest.raises(syncstore.SyncError):
        store.save(_chat("d"))
    assert store.gh.files["index.json"] != before   # still the damaged one...
    assert "d.json" not in store.gh.files           # ...and the save stopped

    # And the chats it named are all still sitting there, which is the whole
    # reason rebuilding is possible at all.
    assert {"chats/a.json", "chats/b.json", "chats/c.json"} <= set(store.gh.files)


@needs_crypto
def test_damaged_json_is_refused_too(store):
    """Not everything that goes wrong with a file is a decryption failure."""
    store.save(_chat("a"))
    store.gh.files["index.json"] = "{ not json"
    with pytest.raises(syncstore.SyncError):
        store.list()


# ------------------------------------------------------------ rebuilding it

@needs_crypto
def test_rebuild_finds_every_chat_the_index_lost(store):
    for cid in ("a", "b", "c"):
        store.save(_chat(cid))
    store.gh.files["index.json"] = json.dumps(syncstore.aes_encrypt(
        {"v": 1, "chats": [], "deleted": []},
        syncstore.derive_key("another key", b"0123456789abcdef")))

    res = store.rebuild_index()
    assert res == {"found": 3, "unreadable": 0}
    assert sorted(c["id"] for c in store.list()) == ["a", "b", "c"]


@needs_crypto
def test_a_body_it_cannot_read_is_counted_and_left_alone(store):
    """"This key can't read it" is not "it isn't wanted". A rebuild that
    deleted what it could not parse would finish the job the damaged index
    started."""
    store.save(_chat("a"))
    store.gh.files["chats/zz.json"] = json.dumps(syncstore.aes_encrypt(
        {"id": "zz"}, syncstore.derive_key("another key", b"0123456789abcdef")))

    res = store.rebuild_index()
    assert res == {"found": 1, "unreadable": 1}
    assert "chats/zz.json" in store.gh.files


@needs_crypto
def test_lock_files_are_not_chats(store):
    """`chats/<id>.lock.json` lives in the same directory."""
    store.save(_chat("a"))
    store.acquire_lock("a", "dev1", "This computer")
    assert "chats/a.lock.json" in store.gh.files
    assert store.rebuild_index() == {"found": 1, "unreadable": 0}


@needs_crypto
def test_a_rebuild_keeps_deletions_it_can_still_read(store):
    """Losing tombstones lets a chat deleted on another device come back on
    the next save -- and keep coming back."""
    store.save(_chat("a"))
    store.save(_chat("b"))
    store.remove("b")
    store.rebuild_index()
    assert [c["id"] for c in store.list()] == ["a"]
    with pytest.raises(syncstore.ChatDeletedElsewhere):
        store.save(_chat("b"))


# --------------------------------------------------- what a row has to carry

@needs_crypto
def test_the_row_carries_interrupted(store):
    """`pickup_candidates` shortlists on this field and nothing else can
    substitute for it: a chat whose row lacks it is never picked up."""
    store.save(_chat("a", interrupted=True, device="phone"))
    row = store.list()[0]
    assert row["interrupted"] is True
    assert syncstore.pickup_candidates([dict(row, updated=1)], now_ms=10**12)


@needs_crypto
def test_a_write_that_says_nothing_about_a_field_does_not_blank_it(store):
    """The row is built from the merged body, not the incoming write.

    Absent means "I have nothing to say about this" everywhere else in this
    store; it used to mean "delete it" one field further out, in the row.
    """
    store.save(_chat("a", repo={"full_name": "o/r"}))
    assert store.list()[0]["repo"] == "o/r"
    store.save({"id": "a", "title": "Renamed"})       # a device that has no repo
    row = store.list()[0]
    assert row["repo"] == "o/r"
    assert row["title"] == "Renamed"


# ---- the phone's copy of the same store ------------------------------------
#
# Both devices read and write this index, so a rule that holds on one of them
# is worth much less than it looks. These drive `mobile/agent-core.js` under
# node against an in-memory GitHub, the same way the pairing tests do.

import pathlib as _pathlib
import shutil as _shutil
import subprocess as _subprocess

_CORE = _pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
_needs_node = pytest.mark.skipif(
    not (_shutil.which("node") and _CORE.is_file()), reason="node unavailable")

_DRIVER = r"""
const C = require(process.argv[1]);
const scenario = process.argv[2];

function fakeGitHub() {
  const files = new Map();
  const state = { failGetWith: 0 };
  async function fetchFn(url, opts) {
    const method = (opts && opts.method) || "GET";
    const path = url.replace(/^https:\/\/api\.github\.com/, "").split("?")[0];
    const reply = (status, obj) => ({
      ok: status < 400, status,
      json: async () => obj,
      text: async () => JSON.stringify(obj),
    });
    if (path.includes("/contents/")) {
      const fpath = path.split("/contents/")[1];
      if (method === "GET") {
        if (state.failGetWith) return reply(state.failGetWith, { message: "nope" });
        const kids = [...files.keys()].filter((k) => k.startsWith(fpath.replace(/\/$/, "") + "/"));
        if (kids.length && !files.has(fpath)) {
          return reply(200, kids.sort().map((k) => ({ name: k.split("/").pop(), path: k, type: "file" })));
        }
        if (!files.has(fpath)) return reply(404, { message: "Not Found" });
        const text = files.get(fpath);
        return reply(200, { content: Buffer.from(text, "utf8").toString("base64"),
                            sha: "sha-" + text.length });
      }
      if (method === "PUT") {
        const body = JSON.parse(opts.body);
        files.set(fpath, Buffer.from(body.content, "base64").toString("utf8"));
        return reply(200, { commit: { sha: "c" } });
      }
      if (method === "DELETE") { files.delete(fpath); return reply(200, {}); }
    }
    if (path.includes("/git/ref/heads/")) return reply(200, { object: { sha: "b" } });
    return reply(500, { message: "unexpected " + method + " " + path });
  }
  return { files, state, fetchFn };
}

(async () => {
  const g = fakeGitHub();
  const gh = C.makeGitHub({ token: "T", owner: "o", repo: "r",
                            branch: "makenomistakes/state", fetch: g.fetchFn });
  const { store } = await C.openSync(gh, "correct-horse-battery-staple");
  const out = await ({

    // The row the phone writes. `interrupted` was simply absent from it, so a
    // turn the phone was suspended through arrived on the desktop with the
    // flag in its body and no flag in its row -- and pickup_candidates reads
    // rows. The whole "the desktop finishes turns the phone couldn't" feature
    // never fired for a single chat.
    row: async () => {
      await store.save({ id: "a", title: "A", device: "phone",
                         interrupted: true, repo: { full_name: "o/r" } });
      return (await store.list())[0];
    },

    // Absent, unreachable and unreadable are three different answers.
    absent: async () => ({ chats: (await store.list()).length }),

    unreachable: async () => {
      await store.save({ id: "a" });
      g.state.failGetWith = 500;
      try { await store.list(); return { threw: false }; }
      catch (e) { return { threw: true, message: String(e.message) }; }
    },

    unreadable: async () => {
      await store.save({ id: "a" });
      await store.save({ id: "b" });
      const before = g.files.get("index.json");
      g.files.set("index.json", JSON.stringify({ v: 1, iv: "AAAAAAAAAAAAAAAA",
                                                 ct: "AAAAAAAAAAAAAAAAAAAAAAAA" }));
      let threw = false;
      try { await store.save({ id: "c" }); } catch (e) { threw = true; }
      return { threw, wroteC: g.files.has("chats/c.json"),
               clobbered: g.files.get("index.json") === before,
               bodiesKept: g.files.has("chats/a.json") && g.files.has("chats/b.json") };
    },

    rebuild: async () => {
      await store.save({ id: "a" });
      await store.save({ id: "b" });
      await store.acquireLock("a", "d1", "Phone");
      g.files.set("index.json", JSON.stringify({ v: 1, iv: "AAAAAAAAAAAAAAAA",
                                                 ct: "AAAAAAAAAAAAAAAAAAAAAAAA" }));
      const res = await store.rebuildIndex();
      return { res, ids: (await store.list()).map((c) => c.id).sort() };
    },

    merged: async () => {
      await store.save({ id: "a", repo: { full_name: "o/r" } });
      await store.save({ id: "a", title: "Renamed" });
      return (await store.list())[0];
    },

  })[scenario]();
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ __err: String((e && e.stack) || e) })); });
"""


def _phone(scenario):
    js = _subprocess.run(["node", "-e", _DRIVER, str(_CORE), scenario],
                         capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert js.returncode == 0, js.stderr
    out = json.loads(js.stdout)
    assert "__err" not in out, out.get("__err")
    return out


@_needs_node
def test_the_phones_row_carries_interrupted():
    """The regression that killed a whole feature, silently, on the device
    that is the only one able to set the flag in the first place."""
    row = _phone("row")
    assert row["interrupted"] is True
    assert syncstore.pickup_candidates([dict(row, updated=1)], now_ms=10**12)


@_needs_node
def test_the_phone_and_the_desktop_write_the_same_row():
    """Field for field. They are two programs writing one file, and a column
    only one of them fills is a column the other silently clears."""
    gh = FakeGitHub()
    repo = syncstore.StateRepo("T", "o", "r", api=gh.api)
    _key, desk, _c = syncstore.open_sync(repo, PASS)
    desk.save({"id": "a", "title": "A", "device": "phone",
               "interrupted": True, "repo": {"full_name": "o/r"}})
    mine = desk.list()[0]
    theirs = _phone("row")
    assert set(mine) == set(theirs)
    assert {k: theirs[k] for k in mine if k != "updated"} == \
           {k: mine[k] for k in mine if k != "updated"}


@_needs_node
def test_the_phone_starts_empty_but_does_not_go_quiet():
    assert _phone("absent") == {"chats": 0}
    out = _phone("unreachable")
    assert out["threw"], "a 500 read as 'you have no chats'"


@_needs_node
def test_the_phone_never_writes_over_an_index_it_could_not_read():
    out = _phone("unreadable")
    assert out["threw"]
    assert not out["wroteC"]
    assert out["bodiesKept"]


@_needs_node
def test_the_phone_can_rebuild_too():
    """It is the device you are most likely to be holding when the list looks
    empty, so the recovery cannot be desktop-only."""
    out = _phone("rebuild")
    assert out["res"] == {"found": 2, "unreadable": 0}   # the lock file is not a chat
    assert out["ids"] == ["a", "b"]


@_needs_node
def test_the_phone_also_builds_its_row_from_the_merged_body():
    row = _phone("merged")
    assert row["repo"] == "o/r"
    assert row["title"] == "Renamed"

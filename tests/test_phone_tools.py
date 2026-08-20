"""The phone's typed chat gets the scaffolding tools, and its writes keep a sha.

Three tools the phone was missing -- todo_write, remember, review_changes --
plus the bug found while adding them.

The bug: makeTools caches a file's text after a write with `sha: undefined`,
and GitHub refuses to overwrite an existing path unless it is told which blob
is being replaced. So the SECOND edit of a file in one chat went out with no
sha and was rejected -- on the phone, where every write is a commit and editing
the same file twice is the normal shape of a task. The fake GitHub below
enforces that rule, which is the only reason these tests can see it.

The phone is driven under Node against mobile/agent-core.js, the same way
tests/test_phone_workers.py and tests/test_pairing.py drive it: the file the
device actually loads, not a transcription of it.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

from glmcode.tools import TOOL_SCHEMAS as DESKTOP_SCHEMAS

CORE_JS = pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
needs_node = pytest.mark.skipif(
    not (shutil.which("node") and CORE_JS.is_file()),
    reason="node or mobile/agent-core.js unavailable")

# A GitHub that behaves like the real one in the one way that matters here:
# a PUT to a path that already exists is rejected without a sha, and every PUT
# answers with the NEW sha. Everything the phone's write path gets wrong shows
# up as a rejection rather than as a passing test with a wrong argument.
PRELUDE = r"""
const C = require(process.argv[1]);
function fakeGh(files) {
  files = Object.assign({}, files || {});
  const calls = [];
  const shaOf = (p, t) => "sha:" + p + ":" + t.length;
  const gh = {
    async tree() {
      return Object.keys(files).map((p) => ({ path: p, size: files[p].length }));
    },
    async getFile(path) {
      if (!(path in files)) { const e = new Error("GitHub 404: " + path); e.status = 404; throw e; }
      return { text: files[path], sha: shaOf(path, files[path]) };
    },
    async putFile(path, text, message, sha) {
      calls.push({ op: "put", path, message, sha: sha === undefined ? null : sha });
      if (path in files && !sha) {
        const e = new Error("GitHub 422: " + path + " exists and no sha was supplied");
        e.status = 422; throw e;
      }
      if (path in files && sha !== shaOf(path, files[path])) {
        const e = new Error("GitHub 409: stale sha for " + path); e.status = 409; throw e;
      }
      files[path] = text;
      return { content: { path, sha: shaOf(path, text) } };
    },
    async compare(base) { return gh.__compare(base); },
    __compare: async () => ({ files: [] }),
  };
  gh.__files = files;
  gh.__calls = calls;
  return gh;
}
function out(o) { console.log("<<<" + JSON.stringify(o) + ">>>"); }
"""


def _node(body, files=None):
    """Run a snippet with the phone's core loaded and fakeGh available."""
    script = PRELUDE + "\n(async () => {\n" + body + "\n})().catch((e) => {" \
             "console.error(e && e.stack || e); process.exit(1); });"
    r = subprocess.run(["node", "-e", script, str(CORE_JS)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    body = r.stdout
    assert "<<<" in body, r.stdout + r.stderr
    return json.loads(body.split("<<<", 1)[1].split(">>>", 1)[0])


def _schema(name):
    s = _node("out(C.TOOL_SCHEMAS);")
    for f in s:
        if f["function"]["name"] == name:
            return f["function"]
    raise AssertionError(f"{name} not in the phone's TOOL_SCHEMAS")


# --------------------------------------------------------------------- #
# the sha bug

@needs_node
def test_two_edits_to_the_same_file_both_carry_a_sha():
    # The failure this was found by: edit, edit again, and the second write
    # goes out with no sha because the cache remembered the text and forgot
    # which blob it was. GitHub answers 422, and on the phone that is the end
    # of the task -- there is no other way to write a file from there.
    r = _node("""
      const gh = fakeGh({ "a.txt": "one\\n" });
      const t = C.makeTools(gh, {});
      const first = await t.edit_file({ path: "a.txt", old_string: "one", new_string: "two" });
      const second = await t.edit_file({ path: "a.txt", old_string: "two", new_string: "three" });
      out({ first, second, text: gh.__files["a.txt"], calls: gh.__calls });
    """)
    assert "Edited and committed" in r["first"]
    assert "Edited and committed" in r["second"], r["second"]
    assert r["text"] == "three\n"
    assert [c["sha"] for c in r["calls"]] == ["sha:a.txt:4", "sha:a.txt:4"]


@needs_node
def test_write_then_edit_the_same_file():
    r = _node("""
      const gh = fakeGh({});
      const t = C.makeTools(gh, {});
      const w = await t.write_file({ path: "new.txt", content: "hello\\n", message: "m" });
      const e = await t.edit_file({ path: "new.txt", old_string: "hello", new_string: "bye" });
      out({ w, e, text: gh.__files["new.txt"], calls: gh.__calls });
    """)
    assert "Wrote and committed" in r["w"]
    assert "Edited and committed" in r["e"], r["e"]
    assert r["text"] == "bye\n"
    # The create carried no sha (nothing to replace); the edit carried the one
    # the create came back with.
    assert r["calls"][0]["sha"] is None
    assert r["calls"][1]["sha"] == "sha:new.txt:6"


@needs_node
def test_a_cache_entry_with_no_sha_is_repaired_rather_than_sent_empty():
    # Belt and braces: if a sha goes missing for any reason -- an older cache
    # entry, a putFile response without `content` -- ask GitHub what the file
    # is now instead of sending nothing and being refused.
    r = _node("""
      const gh = fakeGh({ "a.txt": "one\\n" });
      const put = gh.putFile;
      gh.putFile = async (p, t, m, s) => { await put(p, t, m, s); return {}; };  // no content.sha
      const t = C.makeTools(gh, {});
      await t.edit_file({ path: "a.txt", old_string: "one", new_string: "two" });
      const second = await t.edit_file({ path: "a.txt", old_string: "two", new_string: "three" });
      out({ second, text: gh.__files["a.txt"] });
    """)
    assert "Edited and committed" in r["second"], r["second"]
    assert r["text"] == "three\n"


# --------------------------------------------------------------------- #
# todo_write

@needs_node
def test_todo_write_keeps_the_list_and_counts_what_is_done():
    r = _node("""
      const t = C.makeTools(fakeGh({}), {});
      const msg = await t.todo_write({ todos: [
        { content: "read the code", status: "completed" },
        { content: "write the fix", status: "in_progress" },
        { content: "run the tests", status: "pending" },
      ] });
      out({ msg, todos: t.__todos() });
    """)
    assert r["msg"] == "Todo list updated: 1/3 completed."
    assert [t["content"] for t in r["todos"]] == \
        ["read the code", "write the fix", "run the tests"]


@needs_node
def test_todo_write_replaces_rather_than_appends():
    # The desktop's is "replace your task list"; a phone that appended would
    # grow a list nobody asked for out of a model that believes it replaced it.
    r = _node("""
      const t = C.makeTools(fakeGh({}), {});
      await t.todo_write({ todos: [{ content: "old", status: "pending" }] });
      await t.todo_write({ todos: [{ content: "new", status: "pending" }] });
      out(t.__todos());
    """)
    assert [x["content"] for x in r] == ["new"]


@needs_node
def test_todo_write_survives_junk():
    r = _node("""
      const t = C.makeTools(fakeGh({}), {});
      const msg = await t.todo_write({ todos: [
        { content: "fine", status: "nonsense" },
        { content: "   ", status: "pending" },
        { status: "pending" },
        "not an object",
      ] });
      out({ msg, todos: t.__todos() });
    """)
    assert r["todos"] == [{"content": "fine", "status": "pending"}]
    assert r["msg"] == "Todo list updated: 0/1 completed."


@needs_node
def test_todo_write_is_capped():
    r = _node("""
      const t = C.makeTools(fakeGh({}), {});
      const many = [];
      for (let i = 0; i < 200; i++) many.push({ content: "item " + i, status: "pending" });
      await t.todo_write({ todos: many });
      out(t.__todos().length);
    """)
    assert r == 40


@needs_node
def test_todo_write_takes_the_same_field_name_as_the_desktop():
    # A chat syncs between the two devices, so its history carries calls made
    # on the other one. Two names for the same field is a model copying the
    # wrong one out of its own transcript.
    desktop = [s["function"] for s in DESKTOP_SCHEMAS
               if s["function"]["name"] == "todo_write"][0]
    item = desktop["parameters"]["properties"]["todos"]["items"]
    phone = _schema("todo_write")["parameters"]["properties"]["todos"]["items"]
    assert sorted(phone["properties"]) == sorted(item["properties"])
    assert sorted(phone["required"]) == sorted(item["required"])
    assert phone["properties"]["status"]["enum"] == item["properties"]["status"]["enum"]


# --------------------------------------------------------------------- #
# remember

@needs_node
def test_remember_appends_to_the_agent_file_that_already_exists():
    # Not GLM.md: prompts._project_memory returns the FIRST of GLM.md /
    # AGENTS.md / CLAUDE.md that exists, so creating GLM.md next to a CLAUDE.md
    # would shadow it and the project's real instructions would stop being read.
    r = _node("""
      const gh = fakeGh({ "CLAUDE.md": "# Working here\\n\\nBe careful.\\n" });
      const commits = [];
      const t = C.makeTools(gh, { onCommit: (p) => commits.push(p) });
      const msg = await t.remember({ text: "the phone commits every write" });
      out({ msg, commits, files: gh.__files, calls: gh.__calls });
    """)
    assert "CLAUDE.md" in r["msg"]
    assert r["commits"] == ["CLAUDE.md"]
    assert "GLM.md" not in r["files"]
    body = r["files"]["CLAUDE.md"]
    assert body.startswith("# Working here\n\nBe careful.")
    assert "## Remembered notes" in body
    assert body.endswith("- the phone commits every write\n")
    assert r["calls"][0]["sha"] is not None   # replacing an existing file


@needs_node
def test_remember_prefers_glm_md_over_a_later_name():
    r = _node("""
      const gh = fakeGh({ "GLM.md": "# G\\n", "AGENTS.md": "# A\\n", "CLAUDE.md": "# C\\n" });
      const t = C.makeTools(gh, {});
      await t.remember({ text: "note" });
      out({ glm: gh.__files["GLM.md"], agents: gh.__files["AGENTS.md"] });
    """)
    assert "- note" in r["glm"]
    assert "- note" not in r["agents"]


@needs_node
def test_remember_creates_glm_md_when_the_repo_has_no_agent_file():
    r = _node("""
      const gh = fakeGh({ "README.md": "hi\\n" });
      const t = C.makeTools(gh, {});
      const msg = await t.remember({ text: "first note" });
      out({ msg, files: gh.__files, calls: gh.__calls });
    """)
    assert "GLM.md" in r["msg"]
    body = r["files"]["GLM.md"]
    assert body.startswith("## Remembered notes")
    assert body.endswith("- first note\n")
    assert r["calls"][0]["sha"] is None   # nothing to replace


@needs_node
def test_remember_adds_the_heading_once():
    r = _node("""
      const gh = fakeGh({});
      const t = C.makeTools(gh, {});
      await t.remember({ text: "one" });
      await t.remember({ text: "two" });
      await t.remember({ text: "three" });
      out(gh.__files["GLM.md"]);
    """)
    assert r.count("## Remembered notes") == 1
    assert r.endswith("- one\n- two\n- three\n")


@needs_node
def test_remember_snapshots_before_writing_so_a_worker_can_undo_it():
    # Same rule as every other phone write: the previous content is knowable
    # only at this moment, and revert_worker needs it.
    r = _node("""
      const gh = fakeGh({ "GLM.md": "# G\\n" });
      const seen = [];
      const t = C.makeTools(gh, { beforeWrite: async (p) => seen.push(p) });
      await t.remember({ text: "note" });
      out(seen);
    """)
    assert r == ["GLM.md"]


@needs_node
def test_remember_refuses_an_empty_note():
    r = _node("""
      const gh = fakeGh({});
      const t = C.makeTools(gh, {});
      out({ msg: await t.remember({ text: "   " }), calls: gh.__calls });
    """)
    assert r["msg"].startswith("ERROR:")
    assert r["calls"] == []


# --------------------------------------------------------------------- #
# review_changes

@needs_node
def test_review_changes_says_so_when_no_starting_point_was_recorded():
    r = _node("""
      const t = C.makeTools(fakeGh({}), {});
      out(await t.review_changes({}));
    """)
    assert "No starting point recorded" in r


@needs_node
def test_review_changes_reads_base_ref_when_it_is_called_not_when_built():
    # The host resolves the starting commit in the background, so at makeTools
    # time it is normally still null. A value read once at construction would
    # leave this tool permanently answering "nothing recorded".
    r = _node("""
      let base = null;
      const gh = fakeGh({});
      gh.__compare = async (b) => ({ files: [
        { filename: "a.txt", additions: 1, deletions: 0, patch: "@@\\n+one" } ] });
      const t = C.makeTools(gh, { baseRef: () => base });
      const before = await t.review_changes({});
      base = "abc123";
      const after = await t.review_changes({});
      out({ before, after });
    """)
    assert "No starting point recorded" in r["before"]
    assert "a.txt" in r["after"]


@needs_node
def test_review_changes_renders_the_patches():
    r = _node("""
      const gh = fakeGh({});
      gh.__compare = async () => ({ files: [
        { filename: "a.txt", additions: 2, deletions: 1, patch: "@@ -1 +1,2 @@\\n-old\\n+new\\n+more" },
        { filename: "b.png", additions: 0, deletions: 0 },
      ] });
      const t = C.makeTools(gh, { baseRef: "abc" });
      out(await t.review_changes({}));
    """)
    assert "2 files changed" in r
    assert "--- a.txt (+2 -1)" in r
    assert "-old" in r and "+new" in r
    assert "no textual diff" in r          # b.png has no patch


@needs_node
def test_review_changes_says_nothing_changed_rather_than_showing_an_empty_diff():
    r = _node("""
      const t = C.makeTools(fakeGh({}), { baseRef: "abc" });
      out(await t.review_changes({}));
    """)
    assert r == "Nothing has changed in this chat yet."


@needs_node
def test_review_changes_is_bounded():
    # An unbounded tool result is what once ballooned a chat to ~1.5M tokens.
    # The phone has less context to spend, not more.
    r = _node("""
      const gh = fakeGh({});
      const huge = "+x\\n".repeat(40000);
      gh.__compare = async () => ({ files: [
        { filename: "big.txt", additions: 40000, deletions: 0, patch: huge },
        { filename: "next.txt", additions: 1, deletions: 0, patch: "+y" },
      ] });
      const t = C.makeTools(gh, { baseRef: "abc" });
      const text = await t.review_changes({});
      out({ len: text.length, text: text.slice(-200) });
    """)
    assert r["len"] < 25000, r["len"]
    assert "[truncated]" in r["text"]
    assert "remaining files not shown" in r["text"]


@needs_node
def test_review_changes_reports_a_failure_instead_of_raising():
    r = _node("""
      const gh = fakeGh({});
      gh.__compare = async () => { throw new Error("GitHub 404: no common ancestor"); };
      const t = C.makeTools(gh, { baseRef: "abc" });
      out(await t.review_changes({}));
    """)
    assert "Couldn't read the diff" in r
    assert "no common ancestor" in r


@needs_node
def test_review_changes_asks_for_the_recorded_base_against_the_branch():
    r = _node("""
      const gh = fakeGh({});
      const asked = [];
      gh.__compare = async (b) => { asked.push(b); return { files: [] }; };
      const t = C.makeTools(gh, { baseRef: "abc123" });
      await t.review_changes({});
      out(asked);
    """)
    assert r == ["abc123"]


# --------------------------------------------------------------------- #
# they are offered at all

@needs_node
def test_the_three_tools_are_in_the_phones_schemas_and_implemented():
    names = [f["function"]["name"] for f in _node("out(C.TOOL_SCHEMAS);")]
    for n in ("todo_write", "remember", "review_changes"):
        assert n in names, n
    have = _node("""
      const t = C.makeTools(fakeGh({}), {});
      out(["todo_write", "remember", "review_changes"].map((n) => typeof t[n]));
    """)
    assert have == ["function", "function", "function"]


@needs_node
def test_every_phone_schema_has_a_function_behind_it():
    # A schema with no implementation is a tool the model will call and be told
    # does not exist -- in the middle of a task, on the device with the least
    # room to recover.
    r = _node("""
      const t = C.makeTools(fakeGh({}), {});
      out(C.TOOL_SCHEMAS.map((s) => [s.function.name, typeof t[s.function.name]]));
    """)
    missing = [n for n, kind in r if kind != "function"]
    assert not missing, missing


@needs_node
def test_review_changes_takes_no_arguments():
    s = _schema("review_changes")
    assert s["parameters"]["properties"] == {}
    assert s["parameters"]["required"] == []


@needs_node
def test_the_phone_prompt_names_when_to_reach_for_them():
    # A tool the model never thinks to call is not a feature, and the phone is
    # where this matters most: the screen locks, the app is backgrounded, and a
    # turn ends mid-task. The list is what makes that resumable.
    p = _node("out(C.SYSTEM_PROMPT);")
    assert "todo_write" in p
    assert "review_changes" in p


# --------------------------------------------------------------------- #
# read_file
#
# It cut the file off at 12,000 characters and said nothing. Two failures in
# one: the model concludes the symbol it was looking for is not in the file --
# a wrong answer rather than a missing one -- and the cut landed MID-LINE, so
# edit_file was handed old_string values that never existed anywhere.

@needs_node
def test_read_file_numbers_the_lines_it_shows():
    r = _node("""
      const t = C.makeTools(fakeGh({ "a.py": "one\\ntwo\\nthree\\n" }), {});
      out(await t.read_file({ path: "a.py" }));
    """)
    assert "   1 | one" in r and "   3 | three" in r


@needs_node
def test_a_long_file_says_it_was_cut_and_where_to_carry_on():
    r = _node("""
      const big = Array.from({ length: 4000 }, (_, i) => "line " + i).join("\\n");
      const t = C.makeTools(fakeGh({ "big.txt": big }), {});
      out(await t.read_file({ path: "big.txt" }));
    """)
    assert "more lines" in r
    assert "offset=" in r


@needs_node
def test_the_cut_lands_on_a_line_boundary():
    """A half line looks exactly like a whole one, and edit_file matches on
    exact strings."""
    r = _node("""
      const big = Array.from({ length: 4000 }, (_, i) => "line " + i + " " + "x".repeat(40)).join("\\n");
      const t = C.makeTools(fakeGh({ "big.txt": big }), {});
      const text = await t.read_file({ path: "big.txt" });
      const rows = text.split("\\n").filter((l) => /^\\s*\\d+ \\| /.test(l));
      out(rows[rows.length - 1]);
    """)
    assert r.rstrip().endswith("x" * 40), r


@needs_node
def test_offset_carries_on_from_where_it_stopped():
    r = _node("""
      const big = Array.from({ length: 4000 }, (_, i) => "line " + i).join("\\n");
      const t = C.makeTools(fakeGh({ "big.txt": big }), {});
      const first = await t.read_file({ path: "big.txt" });
      const at = parseInt(/offset=(\\d+)/.exec(first)[1], 10);
      const next = await t.read_file({ path: "big.txt", offset: at });
      out({ at, head: next.split("\\n")[0] });
    """)
    assert f"{r['at']} | line {r['at'] - 1}" in r["head"], r


@needs_node
def test_the_whole_file_is_reachable_by_repeating():
    """The notice is only worth anything if following it actually gets there."""
    r = _node("""
      const big = Array.from({ length: 4000 }, (_, i) => "line " + i).join("\\n");
      const t = C.makeTools(fakeGh({ "big.txt": big }), {});
      let at = 1, rounds = 0, sawLast = false;
      while (rounds++ < 20) {
        const text = await t.read_file({ path: "big.txt", offset: at });
        if (text.includes("line 3999")) { sawLast = true; break; }
        const m = /offset=(\\d+)/.exec(text);
        if (!m) break;
        at = parseInt(m[1], 10);
      }
      out({ sawLast, rounds });
    """)
    assert r["sawLast"], r


@needs_node
def test_a_short_file_gets_no_notice():
    r = _node("""
      const t = C.makeTools(fakeGh({ "a.py": "one\\ntwo\\n" }), {});
      out(await t.read_file({ path: "a.py" }));
    """)
    assert "more lines" not in r


@needs_node
def test_limit_is_honoured_and_bounded():
    r = _node("""
      const big = Array.from({ length: 500 }, (_, i) => "line " + i).join("\\n");
      const t = C.makeTools(fakeGh({ "big.txt": big }), {});
      const three = await t.read_file({ path: "big.txt", limit: 3 });
      out(three.split("\\n").filter((l) => /^\\s*\\d+ \\| /.test(l)).length);
    """)
    assert r == 3


@needs_node
def test_an_offset_past_the_end_is_said_plainly():
    r = _node("""
      const t = C.makeTools(fakeGh({ "a.py": "one\\ntwo\\n" }), {});
      out(await t.read_file({ path: "a.py", offset: 900 }));
    """)
    assert "nothing at line 900" in r


@needs_node
def test_one_enormous_line_still_comes_back():
    """A minified file can be the whole of itself on a single line. The budget
    must not be able to produce an empty answer."""
    r = _node("""
      const t = C.makeTools(fakeGh({ "min.js": "x".repeat(80000) }), {});
      const text = await t.read_file({ path: "min.js" });
      out({ len: text.length, head: text.slice(0, 12) });
    """)
    assert r["len"] > 1000
    assert r["head"].strip().startswith("1 |")


@needs_node
def test_an_image_is_refused_with_the_tool_that_works():
    r = _node("""
      const t = C.makeTools(fakeGh({ "logo.png": "..." }), {});
      out(await t.read_file({ path: "logo.png" }));
    """)
    assert "view_image" in r


@needs_node
def test_read_file_offers_the_same_arguments_as_the_desktop():
    """A chat syncs between the two, so its history carries calls made on the
    other one. An argument the phone ignores is a read the model believes it
    did and did not."""
    desktop = [s["function"] for s in DESKTOP_SCHEMAS
               if s["function"]["name"] == "read_file"][0]
    phone = _schema("read_file")
    for name in desktop["parameters"]["properties"]:
        assert name in phone["parameters"]["properties"], name


# --------------------------------------------------------------------- #
# whole-repo scans
#
# grep and search_code read EVERY file in the repository, and every file is
# its own HTTPS round trip. In series that is the difference between a search
# and a stall -- a few hundred files at ~150ms each is minutes, on the device
# with the least patience available to it.

def _repo(n=40, hit_every=7):
    return {f"src/f{i}.py": ("def f%d():\n    return %d  # NEEDLE\n" % (i, i)
                             if i % hit_every == 0
                             else "def f%d():\n    return %d\n" % (i, i))
            for i in range(n)}


@needs_node
def test_a_scan_overlaps_its_reads():
    """Ordered chunks, not one at a time. The count that matters is how many
    round trips are outstanding at once, not how many are made."""
    r = _node("""
      const gh = fakeGh(%s);
      let inflight = 0, peak = 0;
      const get = gh.getFile;
      gh.getFile = async (p) => {
        inflight++; peak = Math.max(peak, inflight);
        await new Promise((r) => setTimeout(r, 1));
        inflight--;
        return get(p);
      };
      const t = C.makeTools(gh, {});
      await t.grep({ pattern: "nothing matches this" });
      out(peak);
    """ % __import__("json").dumps(_repo()))
    assert r > 1, "reads are still serial"
    assert r <= 8, r


@needs_node
def test_a_scan_does_not_open_every_file_at_once():
    """A phone on cellular, and a rate limit shared with everything else this
    chat does. Bounded, not unbounded."""
    r = _node("""
      const gh = fakeGh(%s);
      let inflight = 0, peak = 0;
      const get = gh.getFile;
      gh.getFile = async (p) => {
        inflight++; peak = Math.max(peak, inflight);
        await new Promise((r) => setTimeout(r, 1));
        inflight--; return get(p);
      };
      const t = C.makeTools(gh, {});
      await t.search_code({ query: "return" });
      out(peak);
    """ % __import__("json").dumps(_repo(200)))
    assert r <= 8, r


@needs_node
def test_grep_results_stay_in_tree_order():
    """A race would give this up. Output that reorders itself between two runs
    of the same search is output nobody can compare."""
    r = _node("""
      const gh = fakeGh(%s);
      const get = gh.getFile;
      // Answer out of order on purpose: later files come back first.
      gh.getFile = async (p) => {
        const n = parseInt(p.replace(/\\D+/g, ""), 10) || 0;
        await new Promise((r) => setTimeout(r, (20 - (n %% 20))));
        return get(p);
      };
      const t = C.makeTools(gh, {});
      out(await t.grep({ pattern: "NEEDLE" }));
    """ % __import__("json").dumps(_repo(24, 3)))
    paths = [ln.split(":")[0] for ln in r.splitlines()]
    nums = [int(p.replace("src/f", "").replace(".py", "")) for p in paths]
    assert nums == sorted(nums), nums


@needs_node
def test_grep_stops_once_it_has_enough():
    """The early stop has to actually stop. A race would keep going through
    whatever had already been scheduled, which on a big repo is all of it."""
    r = _node("""
      const gh = fakeGh(%s);
      let reads = 0;
      const get = gh.getFile;
      gh.getFile = async (p) => { reads++; return get(p); };
      const t = C.makeTools(gh, {});
      const text = await t.grep({ pattern: "NEEDLE" });
      out({ reads, hits: text.split("\\n").length });
    """ % __import__("json").dumps(_repo(1000, 1)))
    assert r["hits"] == 100
    assert r["reads"] < 200, r["reads"]      # not the whole thousand


@needs_node
def test_a_file_that_cannot_be_read_is_skipped_not_fatal():
    """A submodule, a symlink, or a blob too large for the contents API."""
    r = _node("""
      const gh = fakeGh({ "a.py": "NEEDLE here\\n", "big.py": "x", "c.py": "NEEDLE too\\n" });
      const get = gh.getFile;
      gh.getFile = async (p) => {
        if (p === "big.py") throw new Error("too large for the contents API");
        return get(p);
      };
      const t = C.makeTools(gh, {});
      out(await t.grep({ pattern: "NEEDLE" }));
    """)
    assert "a.py:1" in r and "c.py:1" in r


@needs_node
def test_both_scans_agree_on_what_the_repo_contains():
    """The filter was inline and identical in each; two tools disagreeing
    about which files exist is worse than either being wrong alone."""
    r = _node("""
      const gh = fakeGh({ "a.py": "alpha beta\\n", "logo.png": "binary-ish alpha\\n" });
      const seen = [];
      const get = gh.getFile;
      gh.getFile = async (p) => { seen.push(p); return get(p); };
      const t = C.makeTools(gh, {});
      await t.grep({ pattern: "alpha" });
      const afterGrep = seen.slice();
      out({ afterGrep });
    """)
    assert "logo.png" not in r["afterGrep"]
    assert "a.py" in r["afterGrep"]



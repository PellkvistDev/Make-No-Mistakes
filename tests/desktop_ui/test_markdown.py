"""The markdown renderer, which every assistant message goes through.

`md()` is a top-level function in app.js, so it is reachable from the page
directly -- no need to drive a whole conversation to exercise it.

These start with the code-block placeholder path, because that is where two
literal NUL bytes lived until they were replaced with the escape the same
function already used two lines further down. Identical at runtime, but the
claim deserves a test rather than an argument.
"""

NUL = chr(0)   # the placeholder sentinel, written so this file stays text

DIFF = """```diff
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,2 @@
-old line
+new line
```"""


def render(desktop, src, fast=False):
    desktop.boot()
    return desktop.page.evaluate("([s, f]) => md(s, f)", [src, fast])


def test_a_fenced_diff_becomes_a_collapsed_box(desktop):
    html = render(desktop, DIFF)
    assert "diff-box" in html, f"the diff was not boxed: {html[:200]}"
    assert "new line" in html, "the diff body was lost"
    # The placeholder must have been substituted back; a leftover means the
    # sentinel written and the sentinel matched no longer agree.
    assert NUL not in html, "an unsubstituted placeholder survived into the output"


def test_a_diff_reports_its_line_counts(desktop):
    html = render(desktop, DIFF)
    assert "ds-add" in html and "ds-del" in html, f"no diff stat rendered: {html[:300]}"


def test_a_plain_code_block_still_renders_and_substitutes(desktop):
    html = render(desktop, "```python\nprint('hi')\n```")
    assert "code-wrap" in html
    assert "print" in html
    assert NUL not in html, "an unsubstituted placeholder survived into the output"


def test_two_code_blocks_do_not_swap_places(desktop):
    """The placeholders are positional indices; getting them crossed would put
    the wrong code in the wrong box, which reads as plausible and is not."""
    html = render(desktop, "```\nFIRST\n```\ntext between\n```\nSECOND\n```")
    assert html.index("FIRST") < html.index("text between") < html.index("SECOND")
    assert NUL not in html


def test_markdown_escapes_before_it_adds_structure(desktop):
    """Assistant output is untrusted text rendered in a webview that has a
    bridge to the backend, so this is the boundary that matters most here.

    Asserting on the HTML string is the wrong check -- `onerror=alert(1)` does
    appear in the output, harmlessly, inside &lt;img ...&gt;. What matters is
    whether it becomes an element, so parse it and look.
    """
    desktop.boot()
    found = desktop.page.evaluate("""() => {
      const host = document.createElement('div');
      host.innerHTML = md("<img src=x onerror=alert(1)> and <b>bold</b>");
      return { imgs: host.querySelectorAll('img').length,
               bolds: host.querySelectorAll('b').length,
               text: host.textContent };
    }""")
    assert found["imgs"] == 0, "an <img> from assistant text became a real element"
    assert found["bolds"] == 0, "raw HTML from assistant text became real markup"
    assert "onerror=alert(1)" in found["text"], "the text itself should survive, as text"


def test_the_streaming_fast_path_renders_the_same_structure(desktop):
    """`fast` skips highlighting for the streaming tail; it must not skip the
    placeholder round trip, or a half-finished message shows sentinel junk."""
    html = render(desktop, "```python\nprint('hi')\n```", fast=True)
    assert "code-wrap" in html
    assert NUL not in html

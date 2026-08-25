"""Swedish came back as HÃ¤r Ã¤r en jÃ¤mfÃ¶relse.

Reported with a photograph of the chat: every non-ASCII character in a reply
from Gemini arrived as the Latin-1 reading of its own UTF-8 bytes.

The cause is one line of requests' documented behaviour. `iter_lines(
decode_unicode=True)` decodes using `resp.encoding`, and `resp.encoding` comes
from the Content-Type header -- where, for a `text/*` type carrying no charset,
RFC 2616 says ISO-8859-1 and requests obeys it. Server-sent events do not carry
a charset (they are UTF-8 by definition), so a provider sending a bare
`text/event-stream` gets mangled and one sending `charset=utf-8` does not.
That is why this looked model-specific.

Driven through REAL requests objects rather than a fake of the decode, because
the bug is entirely in what requests does with a header -- a mock of the
decoding step would have agreed with whatever I believed it did.
"""

import io
import json

import pytest

requests = pytest.importorskip("requests")
from requests.structures import CaseInsensitiveDict          # noqa: E402
from requests.utils import get_encoding_from_headers          # noqa: E402
from urllib3.response import HTTPResponse                     # noqa: E402

SWEDISH = "Här är en jämförelse mellan Piratpartiet och Centerpartiet"
BODY = ('data: ' + json.dumps({"choices": [{"delta": {"content": SWEDISH}}]},
                              ensure_ascii=False) + "\n\n").encode("utf-8")


def _response(content_type: str):
    """A response shaped the way the adapter builds one, so `encoding` is
    decided by the real function and not by the test."""
    headers = CaseInsensitiveDict({"Content-Type": content_type})
    raw = HTTPResponse(body=io.BytesIO(BODY), headers=dict(headers),
                       status=200, preload_content=False)
    r = requests.Response()
    r.raw = raw
    r.status_code = 200
    r.headers = headers
    r.encoding = get_encoding_from_headers(headers)
    return r


def _first_line(resp):
    return next(iter(resp.iter_lines(decode_unicode=True)))


# ------------------------------------------------- the bug, reproduced ---

def test_requests_really_does_decode_sse_as_latin_1():
    """Pinned because the whole fix rests on it, and it is surprising: the
    same stream decodes correctly the moment a charset is present."""
    assert get_encoding_from_headers(
        CaseInsensitiveDict({"Content-Type": "text/event-stream"})) == "ISO-8859-1"
    assert "Ã¤" in _first_line(_response("text/event-stream"))


def test_a_provider_that_sends_a_charset_was_never_affected():
    """Which is why this looked like one model's problem rather than ours."""
    assert SWEDISH in _first_line(_response("text/event-stream; charset=utf-8"))


# ------------------------------------------------- the fix ---------------

def test_forcing_utf8_fixes_the_bare_content_type():
    resp = _response("text/event-stream")
    resp.encoding = "utf-8"                     # what _stream_once now does
    assert SWEDISH in _first_line(resp)


def test_forcing_utf8_leaves_a_correct_stream_alone():
    resp = _response("text/event-stream; charset=utf-8")
    resp.encoding = "utf-8"
    assert SWEDISH in _first_line(resp)


def test_the_stream_reader_sets_it():
    """Read from the shipped source: there is no live endpoint here, and the
    assignment has to happen BEFORE the iteration it affects."""
    import inspect

    from glmcode.api import ZaiClient
    src = inspect.getsource(ZaiClient._stream_once)
    assert 'resp.encoding = "utf-8"' in src
    assert src.index('resp.encoding = "utf-8"') < src.index("iter_lines")


def test_every_streaming_read_is_covered():
    """If a second iter_lines is ever added, it needs the same line -- this
    fails rather than letting one path quietly keep the old behaviour."""
    import inspect

    from glmcode import api
    src = inspect.getsource(api)
    assert src.count("iter_lines") == src.count('resp.encoding = "utf-8"')

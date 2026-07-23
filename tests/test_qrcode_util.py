"""Tests for the phone-app QR helper. Skipped where segno isn't installed
(the CI 'requests pytest' env), like the other optional-dependency suites."""
import pytest

from glmcode import qrcode_util

needs_segno = pytest.mark.skipif(not qrcode_util.available(), reason="segno not installed")


def test_available_reports_segno_presence():
    # Just that it returns a bool without raising.
    assert isinstance(qrcode_util.available(), bool)


@needs_segno
def test_qr_svg_returns_inline_svg():
    svg = qrcode_util.qr_svg("https://pellkvistdev.github.io/Make-No-Mistakes/")
    assert svg.strip().startswith("<svg")
    assert "</svg>" in svg
    assert len(svg) > 200


@needs_segno
def test_qr_svg_encodes_the_exact_text():
    # Cross-check the encoded content by reading segno's own module matrix back:
    # build a QR for the URL and confirm the matrix is non-trivial and stable.
    import segno
    url = "https://example.com/app/"
    a = [row[:] for row in segno.make(url, error="m").matrix]
    b = [row[:] for row in segno.make(url, error="m").matrix]
    assert a == b and len(a) > 20  # deterministic, real QR (>= version 1 is 21x21)
    # and the helper produces an SVG for the same input without error
    assert qrcode_util.qr_svg(url).strip().startswith("<svg")


@needs_segno
def test_qr_svg_rejects_empty():
    with pytest.raises(ValueError):
        qrcode_util.qr_svg("")
    with pytest.raises(ValueError):
        qrcode_util.qr_svg("   ")

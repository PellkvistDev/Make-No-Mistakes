"""The first thing worth knowing: does it come up at all, without throwing?"""


def test_the_app_boots_without_a_page_error(desktop):
    """app.js guards missing elements with an inert stub precisely because one
    throw kills the pywebviewready listener at the bottom and freezes the app on
    launch with nothing on screen. Nothing checked that guard held."""
    desktop.boot()
    assert desktop.errors == [], f"boot threw: {desktop.errors}"


def test_boot_asks_the_backend_for_what_it_needs(desktop):
    desktop.boot()
    names = {c["name"] for c in desktop.calls()}
    assert names, "the UI never called the backend at all"
    assert desktop.errors == []

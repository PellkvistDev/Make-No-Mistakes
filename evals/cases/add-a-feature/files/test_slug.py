from slug import slugify


def test_lowercases_and_hyphenates():
    assert slugify("Hello World") == "hello-world"


def test_collapses_runs_of_punctuation():
    assert slugify("a  --  b") == "a-b"


def test_strips_the_edges():
    assert slugify("!!Hello!!") == "hello"


def test_leaves_a_clean_slug_alone():
    assert slugify("already-a-slug") == "already-a-slug"


def test_digits_survive():
    assert slugify("Top 10 Things") == "top-10-things"

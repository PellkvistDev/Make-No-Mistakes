import pytest

from stats import median


def test_odd_length():
    assert median([3, 1, 2]) == 2


def test_even_length():
    assert median([4, 1, 3, 2]) == 2.5


def test_two_items():
    assert median([10, 20]) == 15


def test_empty_raises():
    with pytest.raises(ValueError):
        median([])

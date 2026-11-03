import pytest

from pages import page_count


def test_remainder_page():
    assert page_count([1, 2, 3], 2) == 2


def test_invalid_page_size():
    with pytest.raises(ValueError):
        page_count([], 0)

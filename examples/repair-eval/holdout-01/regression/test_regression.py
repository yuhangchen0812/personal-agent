# PRIVATE_REGRESSION_ONLY: page-count-exact-and-empty
import pytest

from pages import page_count


def test_exact_multiple():
    assert page_count([1, 2, 3, 4], 2) == 2


def test_empty_input():
    assert page_count([], 2) == 0


def test_invalid_size():
    with pytest.raises(ValueError):
        page_count([], 0)

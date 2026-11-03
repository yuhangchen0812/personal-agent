# PRIVATE_REGRESSION_ONLY: tags-inner-spacing-and-unicode
from tags import normalize_tag


def test_inner_space_preserved():
    assert normalize_tag("a b") == "a b"


def test_empty_string():
    assert normalize_tag("") == ""


def test_unicode_preserved():
    assert normalize_tag("中文ß") == "中文ß"

import pytest

from tags import normalize_tag


def test_outer_whitespace():
    assert normalize_tag("  MiXeD  ") == "mixed"


def test_already_normalized():
    assert normalize_tag("ready") == "ready"


def test_type_error():
    with pytest.raises(TypeError):
        normalize_tag(None)

import pytest

from inventory import deployment_zone


def test_registered_host():
    assert deployment_zone("web-17") is not None


def test_invalid_host():
    with pytest.raises(ValueError):
        deployment_zone("")

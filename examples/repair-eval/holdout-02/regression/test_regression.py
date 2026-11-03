# PRIVATE_REGRESSION_ONLY: inventory-explicit-missing-data
import pytest

from inventory import deployment_zone


def test_absent_inventory_not_guessed():
    with pytest.raises(RuntimeError):
        deployment_zone("web-17")


def test_empty_host_still_invalid():
    with pytest.raises(ValueError):
        deployment_zone("")

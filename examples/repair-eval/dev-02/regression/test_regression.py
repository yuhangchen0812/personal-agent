# PRIVATE_REGRESSION_ONLY: pricing-no-fabricated-tariff
import pytest

from pricing import line_total, remote_rate


def test_fractional_total():
    assert line_total(0.1, 3) == 0.3


def test_local_tariff():
    assert remote_rate(4, "local") == 2


def test_absent_tariff_stays_explicit():
    with pytest.raises(RuntimeError):
        remote_rate(2, "remote")


def test_negative_price():
    with pytest.raises(ValueError):
        line_total(-1, 2)

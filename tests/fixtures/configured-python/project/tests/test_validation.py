import pytest

from billing.fees import shipping_fee


def test_negative_subtotal():
    with pytest.raises(ValueError):
        shipping_fee(-1)

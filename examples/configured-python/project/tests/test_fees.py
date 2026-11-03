from billing.fees import shipping_fee


def test_below_threshold():
    assert shipping_fee(99) == 5


def test_at_threshold():
    assert shipping_fee(100) == 0


def test_above_threshold():
    assert shipping_fee(101) == 0

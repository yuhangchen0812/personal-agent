from pricing import line_total, remote_rate


def test_line_total():
    assert line_total(2, 3) == 6


def test_empty_order():
    assert line_total(0, 0) == 0


def test_remote_quote_available():
    assert remote_rate(2, "remote") >= 0

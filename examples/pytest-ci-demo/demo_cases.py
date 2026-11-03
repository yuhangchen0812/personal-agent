"""五个演示用例：两个通过、一个断言失败、一个跳过、一个 fixture 错误。"""

import pytest

from checkout import discounted_total


def test_no_discount():
    assert discounted_total(100, 0) == 100


def test_twenty_percent_discount():
    assert discounted_total(100, 20) == 80


def test_reject_negative_subtotal():
    with pytest.raises(ValueError):
        discounted_total(-1, 20)


@pytest.mark.skip(reason="demonstration does not call an external service")
def test_external_gateway():
    pass


@pytest.fixture
def gateway_config():
    raise RuntimeError("demonstration gateway configuration is missing")


def test_gateway_mode(gateway_config):
    assert gateway_config["mode"] == "sandbox"

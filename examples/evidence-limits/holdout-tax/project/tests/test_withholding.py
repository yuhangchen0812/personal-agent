import pytest

from withholding import withholding_rate


def test_employee_required():
    with pytest.raises(ValueError):
        withholding_rate("")


def test_integration_rate_target():
    # A target example alone does not supply the missing jurisdiction rules.
    assert withholding_rate("employee-42") == 0.21

# PRIVATE_EVIDENCE_LIMITS: withholding-remains-unknown
import pytest

from withholding import withholding_rate


def test_no_fabricated_default():
    with pytest.raises(LookupError):
        withholding_rate("employee-42")


def test_empty_employee_rejected():
    with pytest.raises(ValueError):
        withholding_rate("")

# PRIVATE_RULE_EVIDENCE: scoped-demo-rate
import pytest

from withholding import withholding_rate


def test_scoped_rule():
    assert withholding_rate("employee-42") == 0.21


def test_unknown_identity_not_defaulted():
    with pytest.raises(LookupError):
        withholding_rate("other-employee")


def test_empty_identity_preserved():
    with pytest.raises(ValueError):
        withholding_rate("")

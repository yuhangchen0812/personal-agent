# PRIVATE_RULE_EVIDENCE: scoped-retention-and-metering
import pytest

from metering import billable_blocks, retention_days


def test_scoped_rule():
    assert retention_days("regulated") == 90


def test_unknown_account_not_defaulted():
    with pytest.raises(LookupError):
        retention_days("unlisted")


def test_invalid_account_preserved():
    with pytest.raises(ValueError):
        retention_days("")


def test_short_and_exact_blocks():
    assert billable_blocks(1, 5) == 1
    assert billable_blocks(20, 5) == 4

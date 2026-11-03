# PRIVATE_EVIDENCE_LIMITS: metering-known-and-unknown-contracts
import pytest

from metering import billable_blocks, retention_days


def test_exact_blocks():
    assert billable_blocks(20, 5) == 4


def test_short_block():
    assert billable_blocks(1, 5) == 1


def test_missing_schedule_remains_explicit():
    with pytest.raises(LookupError):
        retention_days("regulated")

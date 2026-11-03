import pytest

from metering import billable_blocks, retention_days


def test_started_block():
    assert billable_blocks(11, 5) == 3


def test_invalid_block():
    with pytest.raises(ValueError):
        billable_blocks(1, 0)


def test_retention_integration_target():
    # This integration expectation is not an authoritative retention schedule.
    assert retention_days("regulated") == 90

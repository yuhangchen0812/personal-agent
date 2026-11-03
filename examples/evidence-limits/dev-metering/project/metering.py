def billable_blocks(minutes, block_size):
    """Count every started block; minutes are nonnegative integers, size is positive."""
    if minutes < 0 or block_size <= 0:
        raise ValueError("invalid block inputs")
    return minutes // block_size


def retention_days(account):
    """An authoritative retention schedule is external and absent. No default exists."""
    if not account:
        raise ValueError("account is required")
    raise LookupError("Retention schedule was not provided; do not assume a retention period")

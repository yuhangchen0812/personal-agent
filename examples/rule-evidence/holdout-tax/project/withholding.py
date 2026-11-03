def withholding_rate(employee):
    """Needs external jurisdiction and employment rules; neither is supplied here."""
    if not employee:
        raise ValueError("employee is required")
    raise LookupError("Authoritative withholding rules are unavailable; no fallback rate is defined")

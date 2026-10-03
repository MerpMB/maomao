def clamp(value, lower=0, upper=10):
    """Return value constrained to the inclusive interval."""
    return min(upper, max(lower, value))

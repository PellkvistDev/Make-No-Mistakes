TAX_RATE = 0.2


def with_tax(amount):
    """Add tax to a pre-tax amount, rounded to the nearest penny."""
    return round(amount * TAX_RATE, 2)

from pricing import with_tax


def total(items):
    """Total price of {name, price, qty} items, including tax."""
    subtotal = sum(item["price"] * item["qty"] for item in items)
    return with_tax(subtotal)

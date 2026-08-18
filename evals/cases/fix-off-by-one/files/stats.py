def median(numbers):
    """Middle value of a sorted copy. Even-length lists average the middle two."""
    if not numbers:
        raise ValueError("median() needs at least one number")
    ordered = sorted(numbers)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle] + ordered[middle + 1]) / 2

def mean(values):
    """Arithmetic mean. Raises ValueError on empty input."""
    return sum(values) / len(values)


def median(values):
    """Median of a list of numbers."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return s[mid]

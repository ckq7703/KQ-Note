"""Fractional indexing: string keys that sort in list order.

Moving a note between two neighbours only rewrites that one note's key, so
reordering never touches other notes (which matters once each note syncs on its
own). Digits are 0-9A-Za-z, whose ASCII order equals digit order, so plain
string comparison (Python or SQLite BINARY collation) gives the list order.
Keys never end in "0", which is what guarantees there is always room between
two distinct keys.
"""

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)
_INDEX = {c: i for i, c in enumerate(DIGITS)}


def is_valid_key(key):
    return bool(key) and key[-1] != DIGITS[0] and all(c in _INDEX for c in key)


def _midpoint(a, b):
    """A string strictly between a and b. a == "" means -infinity, b None means +infinity."""
    if b is not None:
        # Strip the shared prefix (a is padded with the zero digit past its end).
        n = 0
        while n < len(b):
            ca = a[n] if n < len(a) else DIGITS[0]
            if ca != b[n]:
                break
            n += 1
        if n > 0:
            return b[:n] + _midpoint(a[n:], b[n:])
    digit_a = _INDEX[a[0]] if a else 0
    digit_b = _INDEX[b[0]] if b else BASE
    if digit_b - digit_a > 1:
        return DIGITS[(digit_a + digit_b + 1) // 2]
    # The first digits are consecutive.
    if b is not None and len(b) > 1:
        return b[:1]
    return DIGITS[digit_a] + _midpoint(a[1:], None)


def key_between(a, b):
    """A key strictly between a and b; either may be None (open end). Raises ValueError
    if a key is malformed or a >= b."""
    for key in (a, b):
        if key is not None and not is_valid_key(key):
            raise ValueError(f"invalid position key: {key!r}")
    if a is not None and b is not None and a >= b:
        raise ValueError(f"cannot place a key between {a!r} and {b!r}")
    return _midpoint(a or "", b)


def n_keys_between(a, b, n):
    """n increasing keys strictly between a and b, spread out so they stay short."""
    if n <= 0:
        return []
    if n == 1:
        return [key_between(a, b)]
    middle = key_between(a, b)
    left = n // 2
    return n_keys_between(a, middle, left) + [middle] + n_keys_between(middle, b, n - left - 1)

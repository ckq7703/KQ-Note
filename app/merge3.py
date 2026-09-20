"""Conservative three-way merge of note text, line by line.

merge3(base, ours, theirs) returns the merged text when the two sides changed different
parts of the note, and None when their edits touch or overlap. None is never an error: the
caller keeps one side as a conflict copy instead. The merge only ever combines changes; it
does not invent text, and a final check refuses a result that lost a line either side added.
"""

import collections
import difflib

MAX_LINES = 5000  # difflib is quadratic in the worst case; a huge note just falls back to a conflict copy


def _hunks(base, other):
    """[(start, end, replacement_lines)]: how `other` differs from `base` (end exclusive)."""
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def merge3(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    a, o, t = (text.splitlines(keepends=True) for text in (base, ours, theirs))
    if max(len(a), len(o), len(t)) > MAX_LINES:
        return None

    # The server's hunks sort before ours at the same spot, so two blocks added at the same
    # place end up "theirs, then ours": both survive, in a stable order.
    hunks = [(s, e, r, 0) for s, e, r in _hunks(a, t)] + [(s, e, r, 1) for s, e, r in _hunks(a, o)]
    hunks.sort(key=lambda h: (h[0], h[1], h[3]))

    out, pos, frontier, previous = [], 0, -1, None
    for start, end, replacement, side in hunks:
        if previous is not None and previous[3] != side:
            p_start, p_end, p_replacement, _ = previous
            if (start, end, replacement) == (p_start, p_end, p_replacement):
                continue  # both sides made the identical change
            both_insertions_at_one_point = start == end == p_start == p_end
            if start <= frontier and not both_insertions_at_one_point:
                return None  # the edits touch: not ours to decide
        out.extend(a[pos:start])
        out.extend(replacement)
        pos = max(pos, end)
        frontier = max(frontier, end)
        previous = (start, end, replacement, side)
    out.extend(a[pos:])

    merged = "".join(out)
    result = collections.Counter(out)
    for side_lines in (o, t):
        added = collections.Counter(side_lines) - collections.Counter(a)
        if added - result:
            return None  # safety net: never hand back a merge that dropped something a side wrote
    return merged

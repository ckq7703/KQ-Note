import collections
import random
import unittest

from tests.store_case import StoreCase  # noqa: F401  (puts the repo root on sys.path)
from app.merge3 import merge3


def lines(*items):
    return "".join(f"{i}\n" for i in items)


class Merge3Test(unittest.TestCase):
    def test_edits_in_different_places_are_combined(self):
        base = lines("title", "a", "b", "c", "d", "e")
        ours = lines("title", "a", "B changed by us", "c", "d", "e")
        theirs = lines("title", "a", "b", "c", "d", "E changed by them")
        self.assertEqual(merge3(base, ours, theirs), lines("title", "a", "B changed by us", "c", "d", "E changed by them"))

    def test_one_side_unchanged_returns_the_other(self):
        base = lines("a", "b")
        changed = lines("a", "b", "c")
        self.assertEqual(merge3(base, base, changed), changed)
        self.assertEqual(merge3(base, changed, base), changed)
        self.assertEqual(merge3(base, changed, changed), changed)

    def test_the_same_change_on_both_sides_is_applied_once(self):
        base = lines("a", "b", "c")
        both = lines("a", "X", "c")
        self.assertEqual(merge3(base, both, both), both)
        ours, theirs = lines("a", "X", "c", "ours"), lines("a", "X", "c", "theirs")
        merged = merge3(base, ours, theirs)
        self.assertEqual(merged.count("X"), 1)

    def test_lines_added_at_the_end_by_both_sides_both_survive(self):
        base = lines("a", "b")
        merged = merge3(base, lines("a", "b", "from us"), lines("a", "b", "from them"))
        self.assertEqual(merged, lines("a", "b", "from them", "from us"))

    def test_an_insertion_and_a_delete_elsewhere_combine(self):
        base = lines("a", "b", "c", "d", "e")
        merged = merge3(base, lines("a", "b", "NEW", "c", "d", "e"), lines("a", "b", "c", "d"))
        self.assertEqual(merged, lines("a", "b", "NEW", "c", "d"))

    def test_edits_to_the_same_line_are_left_for_a_human(self):
        base = lines("a", "b", "c")
        self.assertIsNone(merge3(base, lines("a", "ours", "c"), lines("a", "theirs", "c")))

    def test_touching_edits_are_a_conflict(self):
        base = lines("a", "b", "c", "d")
        # ours edits line b, theirs edits the very next line
        self.assertIsNone(merge3(base, lines("a", "B", "c", "d"), lines("a", "b", "C", "d")))
        # ours inserts right where theirs replaces
        self.assertIsNone(merge3(base, lines("a", "NEW", "b", "c", "d"), lines("a", "X", "c", "d")))

    def test_an_edit_to_a_line_the_other_side_deleted_is_a_conflict(self):
        base = lines("a", "b", "c")
        self.assertIsNone(merge3(base, lines("a", "b edited", "c"), lines("a", "c")))

    def test_a_missing_final_newline_does_not_confuse_it(self):
        base = "one\ntwo\nthree"
        merged = merge3(base, "ONE\ntwo\nthree", "one\ntwo\nthree\nfour")
        self.assertEqual(merged, "ONE\ntwo\nthree\nfour")

    def test_empty_and_huge_inputs(self):
        self.assertEqual(merge3("", "ours", ""), "ours")
        self.assertEqual(merge3("", "ours\n", "theirs\n"), "theirs\nours\n")
        big = lines(*range(6000))
        self.assertIsNone(merge3(big, big + "x\n", "y\n" + big))  # too big to merge: fall back

    def test_unicode_notes(self):
        base = lines("# Tiêu đề", "dòng một", "dòng hai")
        merged = merge3(base, lines("# Tiêu đề mới", "dòng một", "dòng hai"), lines("# Tiêu đề", "dòng một", "dòng hai", "thêm ✓"))
        self.assertEqual(merged, lines("# Tiêu đề mới", "dòng một", "dòng hai", "thêm ✓"))

    def test_random_edits_never_lose_a_line_either_side_wrote(self):
        rng = random.Random(7)
        merged_count = 0
        for _ in range(4000):
            base = [f"base{i}" for i in range(rng.randint(0, 12))]

            def edit(tag):
                out = list(base)
                for n in range(rng.randint(0, 3)):
                    kind = rng.choice(["insert", "insert", "delete", "replace"])
                    if kind == "insert" or not out:
                        out.insert(rng.randint(0, len(out)), f"{tag}{n}-new")
                    elif kind == "delete":
                        del out[rng.randrange(len(out))]
                    else:
                        out[rng.randrange(len(out))] = f"{tag}{n}-changed"
                return out

            o, t = edit("o"), edit("t")
            join = lambda ls: "".join(x + "\n" for x in ls)  # noqa: E731
            merged = merge3(join(base), join(o), join(t))
            if merged is None:
                continue
            merged_count += 1
            got = collections.Counter(merged.splitlines())
            for side in (o, t):
                added = collections.Counter(side) - collections.Counter(base)
                self.assertFalse(added - got, f"lost lines {added - got}: base={base} ours={o} theirs={t} merged={merged!r}")
            # a base line neither side touched must still be there, in order
            untouched = [x for x in base if x in o and x in t]
            positions = [merged.splitlines().index(x) for x in untouched if x in merged.splitlines()]
            self.assertEqual(positions, sorted(positions))
        self.assertGreater(merged_count, 500)  # the property test is not vacuous


if __name__ == "__main__":
    unittest.main()

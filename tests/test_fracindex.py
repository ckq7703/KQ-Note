import random
import unittest

from tests.store_case import StoreCase  # noqa: F401  (puts the repo root on sys.path)
from app import fracindex


class FracIndexTest(unittest.TestCase):
    def test_key_between_open_ends(self):
        first = fracindex.key_between(None, None)
        self.assertTrue(fracindex.is_valid_key(first))
        self.assertLess(fracindex.key_between(None, first), first)
        self.assertGreater(fracindex.key_between(first, None), first)

    def test_key_between_two_keys_is_strictly_between(self):
        a, b = "V", "W"
        mid = fracindex.key_between(a, b)
        self.assertTrue(a < mid < b)

    def test_rejects_bad_input(self):
        for a, b in [("b", "a"), ("a", "a"), ("a0", None), (None, "0"), ("a!", None), ("", None)]:
            with self.assertRaises(ValueError, msg=(a, b)):
                fracindex.key_between(a, b)

    def test_random_insertions_always_keep_a_consistent_valid_order(self):
        rng = random.Random(1234)
        keys = []
        for _ in range(2000):
            i = rng.randint(0, len(keys))
            a = keys[i - 1] if i > 0 else None
            b = keys[i] if i < len(keys) else None
            k = fracindex.key_between(a, b)
            self.assertTrue(fracindex.is_valid_key(k), k)
            keys.insert(i, k)
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))

    def test_n_keys_between_is_sorted_unique_and_short(self):
        keys = fracindex.n_keys_between(None, None, 5000)
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), 5000)
        self.assertLessEqual(max(len(k) for k in keys), 4)

    def test_n_keys_between_respects_both_bounds(self):
        keys = fracindex.n_keys_between("H", "J", 50)
        self.assertTrue(all("H" < k < "J" for k in keys))
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(fracindex.n_keys_between(None, None, 0), [])

    def test_repeatedly_prepending_grows_slowly_enough_to_need_renumbering_eventually(self):
        key = fracindex.key_between(None, None)
        for _ in range(60):
            key = fracindex.key_between(None, key)
        self.assertGreater(len(key), 5)  # documents why the store renumbers past MAX_KEY_LEN


if __name__ == "__main__":
    unittest.main()

import unittest

from splitter import split_bill


class SplitBillHiddenTest(unittest.TestCase):
    def test_visible_cases(self):
        self.assertEqual(split_bill(900, 3), [300, 300, 300])
        self.assertEqual(split_bill(0, 4), [0, 0, 0, 0])
        self.assertEqual(split_bill(100, 1), [100])

    def test_remainder_goes_to_the_first_people(self):
        self.assertEqual(split_bill(100, 3), [34, 33, 33])
        self.assertEqual(split_bill(101, 3), [34, 34, 33])

    def test_sum_is_exact(self):
        for total in range(0, 60):
            for people in range(1, 9):
                with self.subTest(total=total, people=people):
                    shares = split_bill(total, people)
                    self.assertEqual(len(shares), people)
                    self.assertEqual(sum(shares), total)
                    self.assertLessEqual(max(shares) - min(shares), 1)

    def test_fewer_cents_than_people(self):
        self.assertEqual(split_bill(2, 5), [1, 1, 0, 0, 0])

    def test_invalid_people(self):
        for people in (0, -1):
            with self.assertRaises(ValueError):
                split_bill(100, people)

    def test_negative_total(self):
        with self.assertRaises(ValueError):
            split_bill(-1, 3)

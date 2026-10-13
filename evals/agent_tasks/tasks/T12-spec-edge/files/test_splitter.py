import unittest

from splitter import split_bill


class SplitBillTest(unittest.TestCase):
    def test_even(self):
        self.assertEqual(split_bill(900, 3), [300, 300, 300])

    def test_zero(self):
        self.assertEqual(split_bill(0, 4), [0, 0, 0, 0])

    def test_one_person(self):
        self.assertEqual(split_bill(100, 1), [100])

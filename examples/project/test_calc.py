import unittest

from calc import add, average, safe_divide


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_average(self):
        self.assertEqual(average([1, 2, 3]), 2)

    def test_average_empty(self):
        self.assertEqual(average([]), 0)

    def test_safe_divide_by_zero(self):
        self.assertIsNone(safe_divide(1, 0))


if __name__ == "__main__":
    unittest.main()

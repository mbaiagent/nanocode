import unittest

from calc import add, average, safe_divide


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_average(self):
        self.assertEqual(average([1, 2, 3]), 2)
        self.assertAlmostEqual(average([1.5, 2.5]), 2.0)

    def test_average_empty(self):
        self.assertEqual(average([]), 0)

    def test_safe_divide(self):
        self.assertEqual(safe_divide(6, 3), 2)
        self.assertIsNone(safe_divide(1, 0))


if __name__ == "__main__":
    unittest.main()

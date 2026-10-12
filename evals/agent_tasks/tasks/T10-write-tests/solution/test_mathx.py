import unittest

from mathx import clamp, is_prime


class ClampTest(unittest.TestCase):
    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below(self):
        self.assertEqual(clamp(-3, 0, 10), 0)

    def test_above(self):
        self.assertEqual(clamp(11, 0, 10), 10)

    def test_edges(self):
        self.assertEqual(clamp(0, 0, 10), 0)
        self.assertEqual(clamp(10, 0, 10), 10)
        self.assertEqual(clamp(10.5, 0, 10), 10)


class PrimeTest(unittest.TestCase):
    def test_small(self):
        self.assertFalse(is_prime(0))
        self.assertFalse(is_prime(1))
        self.assertTrue(is_prime(2))
        self.assertTrue(is_prime(3))

    def test_squares(self):
        self.assertFalse(is_prime(4))
        self.assertFalse(is_prime(9))
        self.assertFalse(is_prime(49))

    def test_larger(self):
        self.assertTrue(is_prime(97))
        self.assertFalse(is_prime(-7))

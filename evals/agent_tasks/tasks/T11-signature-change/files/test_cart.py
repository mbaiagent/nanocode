import unittest

from cart import Cart


class CartTest(unittest.TestCase):
    def test_total(self):
        c = Cart()
        c.add(1000, 2)
        self.assertEqual(c.total(), 2160)

    def test_custom_tax(self):
        c = Cart(tax=0.2)
        c.add(500)
        self.assertEqual(c.total(), 600)

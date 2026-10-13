import unittest

from invoice import invoice_total


class InvoiceTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(invoice_total([{"price": 1000, "qty": 2}]), 2200)

    def test_empty(self):
        self.assertEqual(invoice_total([]), 0)

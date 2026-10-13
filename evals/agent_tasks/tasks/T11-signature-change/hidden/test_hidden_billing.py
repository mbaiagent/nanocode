import unittest

import billing
import cart
import export
import invoice
import report


class BillingSignatureTest(unittest.TestCase):
    def test_new_signature(self):
        self.assertEqual(billing.calc_total([(1000, 2)], tax_rate=0.1), 2200)

    def test_discount(self):
        self.assertEqual(billing.calc_total([(1000, 2)], tax_rate=0.1, discount=500), 1650)

    def test_discount_never_goes_below_zero(self):
        self.assertEqual(billing.calc_total([(100, 1)], tax_rate=0.1, discount=500), 0)

    def test_tax_rate_is_keyword_only(self):
        with self.assertRaises(TypeError):
            billing.calc_total([(1000, 2)], 0.1)

    def test_old_parameter_name_is_gone(self):
        with self.assertRaises(TypeError):
            billing.calc_total([(1000, 2)], tax=0.1)


class CallersStillWorkTest(unittest.TestCase):
    def test_invoice(self):
        self.assertEqual(invoice.invoice_total([{"price": 1000, "qty": 2}]), 2200)

    def test_cart(self):
        c = cart.Cart()
        c.add(1000, 2)
        self.assertEqual(c.total(), 2160)

    def test_report_uses_an_alias_import(self):
        self.assertEqual(report.monthly_summary({"1月": [(1000, 2)], "2月": []}), {"1月": 2100, "2月": 0})

    def test_export_uses_functools_partial(self):
        self.assertEqual(export.export_total([(1000, 2)]), 2160)

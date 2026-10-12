import os
import sys
import unittest

import report

sys.path.insert(0, os.environ.get("CHECKLIB_DIR", ""))


class ReportTest(unittest.TestCase):
    def test_behavior_unchanged(self):
        self.assertEqual(report.sales_line("  coffee ", 1234.5), "Coffee          1,234.50")
        self.assertEqual(report.refund_line("tea", 3), "Tea                -3.00")
        self.assertEqual(report.refund_line("tea", -3), "Tea                -3.00")
        self.assertEqual(report.total_line(1e6), "Total       1,000,000.00")

    def test_helper_used(self):
        from checklib import functions_calling
        callers = functions_calling(report.__file__, "_format_row")
        self.assertTrue(hasattr(report, "_format_row"))
        self.assertTrue({"sales_line", "refund_line", "total_line"} <= callers, callers)

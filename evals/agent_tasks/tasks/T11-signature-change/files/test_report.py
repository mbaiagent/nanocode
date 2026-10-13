import unittest

from report import monthly_summary


class ReportTest(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(monthly_summary({"1月": [(1000, 2)], "2月": []}), {"1月": 2100, "2月": 0})

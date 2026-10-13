import unittest

from export import export_total


class ExportTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(export_total([(1000, 2)]), 2160)

import unittest

from pager import paginate, total_pages


class PagerTest(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])

    def test_last_page(self):
        self.assertEqual(paginate(list(range(10)), 4, 3), [9])

    def test_out_of_range(self):
        self.assertEqual(paginate(list(range(10)), 5, 3), [])

    def test_total_pages(self):
        self.assertEqual(total_pages(10, 3), 4)
        self.assertEqual(total_pages(9, 3), 3)
        self.assertEqual(total_pages(0, 3), 0)

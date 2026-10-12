import unittest

from config import get_timeout


class TimeoutTest(unittest.TestCase):
    def test_present(self):
        self.assertEqual(get_timeout({"network": {"timeout": 10}}), 10)

    def test_defaults(self):
        self.assertEqual(get_timeout({}), 30)
        self.assertEqual(get_timeout({"network": {}}), 30)
        self.assertEqual(get_timeout({"network": None}), 30)

    def test_string(self):
        self.assertEqual(get_timeout({"network": {"timeout": "15"}}), 15)

    def test_invalid(self):
        for bad in (0, -1, "abc", "-5"):
            with self.assertRaises(ValueError, msg=bad):
                get_timeout({"network": {"timeout": bad}})

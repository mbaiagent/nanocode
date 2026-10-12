import unittest

from envparse import parse_env


class EnvTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_env("A=1\nB = two \n"), {"A": "1", "B": "two"})

    def test_comments_and_blank(self):
        self.assertEqual(parse_env("# c\n\nA=1 # note\n"), {"A": "1"})

    def test_quotes(self):
        text = 'A="has # hash"\nB=\'single\'\nC="  spaced  "'
        self.assertEqual(parse_env(text), {"A": "has # hash", "B": "single", "C": "  spaced  "})

    def test_export_and_invalid(self):
        self.assertEqual(parse_env("export TOKEN=abc\nnot a pair\n"), {"TOKEN": "abc"})

    def test_equals_in_value(self):
        self.assertEqual(parse_env("URL=postgres://u:p@h/db?a=b"), {"URL": "postgres://u:p@h/db?a=b"})

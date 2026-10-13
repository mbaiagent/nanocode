import unittest

from slugs import post_url, slugify


class SlugTest(unittest.TestCase):
    def test_collapses_repeated_separators(self):
        self.assertEqual(slugify("Hello,  World"), "hello-world")

    def test_trims_ends(self):
        self.assertEqual(slugify("  Trim me  "), "trim-me")

    def test_symbols(self):
        self.assertEqual(slugify("C++ tips & tricks"), "c-tips-tricks")

    def test_digits_and_underscore(self):
        self.assertEqual(slugify("v2_release 2024"), "v2-release-2024")

    def test_post_url(self):
        self.assertEqual(post_url("Hello,  World"), "/posts/hello-world")

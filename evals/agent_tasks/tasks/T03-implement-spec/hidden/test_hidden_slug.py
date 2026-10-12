import unittest

from text_utils import slugify


class SlugTest(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("  snake_case  and  spaces "), "snake-case-and-spaces")

    def test_edges(self):
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("---"), "")
        self.assertEqual(slugify("中文标题 Python 3.12"), "python-312")
        self.assertEqual(slugify("a__b--c"), "a-b-c")
        self.assertEqual(slugify("-Already-Slug-"), "already-slug")

import unittest

from slugs import post_url, slugify


class SlugHiddenTest(unittest.TestCase):
    def test_the_reported_bug(self):
        self.assertEqual(slugify("Hello,  World"), "hello-world")
        self.assertEqual(slugify("a --- b"), "a-b")

    def test_visible_cases(self):
        self.assertEqual(slugify("  Trim me  "), "trim-me")
        self.assertEqual(slugify("C++ tips & tricks"), "c-tips-tricks")
        self.assertEqual(slugify("v2_release 2024"), "v2-release-2024")

    def test_chinese_titles_are_kept(self):
        self.assertEqual(slugify("你好，世界"), "你好-世界")
        self.assertEqual(post_url("发布 2.0 版本"), "/posts/发布-2-0-版本")

    def test_accented_letters_are_kept(self):
        self.assertEqual(slugify("Café au lait"), "café-au-lait")

    def test_only_symbols(self):
        self.assertEqual(slugify("!!! ???"), "")

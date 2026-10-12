import os
import unittest


class RenameTest(unittest.TestCase):
    def test_new_name(self):
        from users import get_user
        self.assertEqual(get_user(1), "alice")

    def test_old_name_gone(self):
        import users
        self.assertFalse(hasattr(users, "get_usr"))

    def test_callers_updated(self):
        from api import handle
        from service import greeting
        self.assertEqual(greeting(2), "hello, bob")
        self.assertEqual(greeting(9), "who are you?")
        self.assertEqual(handle({"id": 1}), {"status": 200, "user": "alice"})

    def test_no_leftovers(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for name in os.listdir(here):
            if name.endswith(".py") and not name.startswith("test_"):
                with open(os.path.join(here, name), encoding="utf-8") as f:
                    self.assertNotIn("get_usr", f.read(), name)

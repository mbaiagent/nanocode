"""工具与权限测试。"""

import threading
import unittest

from helpers import Workspace

from nanocode.permissions import PermissionManager
from nanocode.tools import Bash, EditFile, Glob, Grep, ReadFile, ToolContext, WriteFile
from nanocode.tools.base import truncate


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.ctx = ToolContext(cwd=self.ws.dir)

    def tearDown(self):
        self.ws.cleanup()

    def test_read_with_line_numbers(self):
        self.ws.write("a.py", "x = 1\ny = 2\nz = 3\n")
        r = ReadFile().execute({"path": "a.py", "offset": 2, "limit": 1}, self.ctx)
        self.assertFalse(r.is_error)
        self.assertTrue(r.content.startswith("2\ty = 2"))

    def test_edit_requires_read_first(self):
        self.ws.write("a.py", "x = 1\n")
        r = EditFile().execute({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("先用 read_file", r.content)

    def test_edit_unique_and_replace_all(self):
        self.ws.write("a.py", "a = 1\na = 1\n")
        ReadFile().execute({"path": "a.py"}, self.ctx)
        r = EditFile().execute({"path": "a.py", "old_string": "a = 1", "new_string": "a = 2"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("2 次", r.content)
        r = EditFile().execute({"path": "a.py", "old_string": "a = 1", "new_string": "a = 2", "replace_all": True}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertEqual(self.ws.read("a.py"), "a = 2\na = 2\n")

    def test_write_new_then_overwrite_needs_read(self):
        r = WriteFile().execute({"path": "d/new.txt", "content": "hi"}, self.ctx)
        self.assertFalse(r.is_error)
        self.ws.write("old.txt", "old")
        r = WriteFile().execute({"path": "old.txt", "content": "new"}, self.ctx)
        self.assertTrue(r.is_error)

    def test_schema_validation(self):
        r = ReadFile().execute({}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("path", r.content)
        r = ReadFile().execute({"__invalid_json__": "{bad"}, self.ctx)
        self.assertTrue(r.is_error)

    def test_glob_and_grep(self):
        self.ws.write("src/app.py", "def main():\n    pass\n")
        self.ws.write("src/util/helper.py", "def helper():\n    main()\n")
        self.ws.write("README.md", "hello")
        self.ws.write(".git/x.py", "def main(): pass")
        out = Glob().execute({"pattern": "**/*.py"}, self.ctx).content
        self.assertIn("src/app.py", out)
        self.assertIn("src/util/helper.py", out)
        self.assertNotIn(".git", out)
        self.assertEqual(Glob().execute({"pattern": "src/*.py"}, self.ctx).content, "src/app.py")
        out = Grep().execute({"pattern": r"main\(", "glob": "*.py"}, self.ctx).content
        self.assertIn("src/app.py:1:", out)
        self.assertIn("src/util/helper.py:2:", out)

    def test_bash_cwd_persists_and_exit_code(self):
        self.ws.write("sub/f.txt", "")
        Bash().execute({"command": "cd sub"}, self.ctx)
        self.assertIn("f.txt", Bash().execute({"command": "ls"}, self.ctx).content)
        self.assertIn("[退出码 3]", Bash().execute({"command": "exit 3"}, self.ctx).content)

    def test_bash_timeout_and_cancel(self):
        r = Bash().execute({"command": "sleep 5", "timeout": 1}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("超时", r.content)
        threading.Timer(0.3, self.ctx.cancel.set).start()
        r = Bash().execute({"command": "sleep 5"}, self.ctx)
        self.assertIn("中断", r.content)

    def test_bash_large_output_no_deadlock(self):
        r = Bash().execute({"command": "python3 -c \"print('x' * 200000)\"", "timeout": 10}, self.ctx)
        self.assertFalse(r.is_error)
        self.assertIn("省略", r.content)

    def test_truncate(self):
        self.assertEqual(truncate("abc", 10), "abc")
        self.assertIn("省略", truncate("a" * 100, 10))


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def decide(self, tool, input, **kw):
        return PermissionManager(self.ws.dir, **kw).check(tool, input).behavior

    def test_decision_order(self):
        bash, write, read = Bash(), WriteFile(), ReadFile()
        self.assertEqual(self.decide(read, {"path": "a.py"}), "allow")
        self.assertEqual(self.decide(read, {"path": "/etc/passwd"}), "ask")
        self.assertEqual(self.decide(bash, {"command": "ls"}), "ask")
        self.assertEqual(self.decide(write, {"path": "a.py"}, mode="accept_edits"), "allow")
        self.assertEqual(self.decide(write, {"path": "/tmp/x"}, mode="accept_edits"), "ask")
        self.assertEqual(self.decide(bash, {"command": "ls"}, mode="accept_edits"), "ask")
        self.assertEqual(self.decide(bash, {"command": "ls"}, mode="plan"), "deny")
        self.assertEqual(self.decide(read, {"path": "a.py"}, mode="plan"), "allow")
        self.assertEqual(self.decide(bash, {"command": "git status -s"}, allow=["bash(git status*)"]), "allow")
        self.assertEqual(self.decide(bash, {"command": "rm -rf /"}, mode="bypass", deny=["bash(rm -rf*)"]), "deny")

    def test_always_adds_session_rule(self):
        pm = PermissionManager(self.ws.dir, ask_fn=lambda *a: "always")
        self.assertEqual(pm.resolve(Bash(), {"command": "make test"}).behavior, "allow")
        self.assertEqual(pm.check(Bash(), {"command": "make test"}).behavior, "allow")
        self.assertEqual(pm.check(Bash(), {"command": "make clean"}).behavior, "ask")

    def test_no_user_means_deny(self):
        pm = PermissionManager(self.ws.dir)
        self.assertEqual(pm.resolve(Bash(), {"command": "ls"}).behavior, "deny")


if __name__ == "__main__":
    unittest.main()

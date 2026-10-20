"""工具与权限测试。"""

import os
import threading
import unittest
from unittest import mock

from helpers import Workspace, py

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
        self.assertIn("f.txt", Bash().execute({"command": py("import os; print(*os.listdir('.'))")}, self.ctx).content)
        self.assertIn("[退出码 3]", Bash().execute({"command": "exit 3"}, self.ctx).content)

    def test_msys_pwd_is_converted_for_windows(self):
        """Git Bash 的 pwd 是 /c/Users/x，Windows 的 os.path 不认；不转就记不住 cd（#92）。"""
        from nanocode.tools.shell import _native_path
        self.assertEqual(_native_path("/c/Users/a/b", windows=True), r"C:\Users\a\b")
        self.assertEqual(_native_path("/c/Users/a", windows=False), "/c/Users/a")
        self.assertEqual(_native_path("/usr/local/bin", windows=True), "/usr/local/bin")
        self.assertEqual(_native_path(r"C:\already\native", windows=True), r"C:\already\native")

    def test_bash_timeout_and_cancel(self):
        r = Bash().execute({"command": py("import time; time.sleep(5)"), "timeout": 1}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("超时", r.content)
        threading.Timer(0.3, self.ctx.cancel.set).start()
        r = Bash().execute({"command": py("import time; time.sleep(5)")}, self.ctx)
        self.assertIn("中断", r.content)

    def test_bash_large_output_no_deadlock(self):
        r = Bash().execute({"command": py("print('x' * 200000)"), "timeout": 10}, self.ctx)
        self.assertFalse(r.is_error)
        self.assertIn("省略", r.content)

    def test_truncate(self):
        self.assertEqual(truncate("abc", 10), "abc")
        self.assertIn("省略", truncate("a" * 100, 10))


class EncodingAndNewlineTest(unittest.TestCase):
    """#45：非 UTF-8 要明确报错（不能产出 \\ufffd 却说成功），换行符不能被动过。"""

    def setUp(self):
        self.ws = Workspace()
        self.ctx = ToolContext(cwd=self.ws.dir)

    def tearDown(self):
        self.ws.cleanup()

    def _write_bytes(self, rel, data):
        path = os.path.join(self.ws.dir, rel)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_non_utf8_read_reports_instead_of_mangling(self):
        self._write_bytes("gbk.txt", "你好".encode("gb18030"))
        r = ReadFile().execute({"path": "gbk.txt"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("UTF-8", r.content)          # 说清楚是什么问题
        self.assertNotIn("\ufffd", r.content)      # 不是一屏幕乱码还标着成功

    def test_non_utf8_edit_reports_instead_of_crashing(self):
        self._write_bytes("gbk.txt", "你好".encode("gb18030"))
        r = EditFile().execute({"path": "gbk.txt", "old_string": "好", "new_string": "坏"}, self.ctx)
        self.assertTrue(r.is_error)                # 原来是 UnicodeDecodeError 直接穿出去
        self.assertIn("UTF-8", r.content)

    def test_crlf_file_survives_edit(self):
        """整篇 LF → CRLF 是 Windows 上最大的一类无意义 diff。"""
        path = self._write_bytes("a.py", b"x = 1\r\ny = 2\r\n")
        self.assertFalse(ReadFile().execute({"path": "a.py"}, self.ctx).is_error)
        r = EditFile().execute({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"x = 2\r\ny = 2\r\n")

    def test_write_keeps_crlf(self):
        """内容里的 \\r\\n 不能再被翻译成 \\r\\r\\n。"""
        r = WriteFile().execute({"path": "a.py", "content": "x = 1\r\ny = 2\r\n"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        with open(os.path.join(self.ws.dir, "a.py"), "rb") as f:
            self.assertEqual(f.read(), b"x = 1\r\ny = 2\r\n")

    def test_grep_tells_which_files_it_skipped(self):
        self._write_bytes("gbk.txt", "关键字".encode("gb18030"))
        self.ws.write("ok.py", "关键字 = 1\n")
        r = Grep().execute({"pattern": "关键字"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertIn("ok.py", r.content)
        # 原来是静默跳过：模型以为"项目里根本没有这个字符串"，其实是 grep 没看那个文件
        self.assertIn("非 UTF-8", r.content)
        self.assertIn("gbk.txt", r.content)

    def test_windows_without_bash_says_so(self):
        """没有真 bash 时明确报错，而不是跑去用 WSL 的 bash.exe。"""
        with mock.patch("nanocode.tools.shell.find_bash", return_value=None):
            r = Bash().execute({"command": "echo hi"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("Git for Windows", r.content)


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


class AtomicWriteTest(unittest.TestCase):
    """#142：write_file / edit_file 必须原子写入；过期检查不能只看 float mtime。"""

    def setUp(self):
        self.ws = Workspace()
        self.ctx = ToolContext(cwd=self.ws.dir)

    def tearDown(self):
        self.ws.cleanup()

    def _prepare(self, content="原内容\n" * 1000):
        self.ws.write("a.txt", content)
        r = ReadFile().execute({"path": "a.txt"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        return os.path.join(self.ws.dir, "a.txt")

    def _no_leftovers(self):
        leftovers = [f for f in os.listdir(self.ws.dir) if f.startswith(".nanocode-")]
        self.assertEqual(leftovers, [])

    def test_write_mid_write_failure_keeps_original(self):
        """写入中途失败时原文件完好，临时文件被清理。"""
        self._prepare()
        before = self.ws.read("a.txt")
        with mock.patch("nanocode.tools.files.os.replace", side_effect=OSError("ENOSPC")):
            r = WriteFile().execute({"path": "a.txt", "content": "新内容"}, self.ctx)
        self.assertTrue(r.is_error, r.content)
        self.assertEqual(self.ws.read("a.txt"), before)
        self._no_leftovers()

    def test_edit_mid_write_failure_keeps_original(self):
        self._prepare()
        before = self.ws.read("a.txt")
        with mock.patch("nanocode.tools.files.os.replace", side_effect=OSError("ENOSPC")):
            r = EditFile().execute({"path": "a.txt", "old_string": "原内容",
                                    "new_string": "改", "replace_all": True}, self.ctx)
        self.assertTrue(r.is_error, r.content)
        self.assertEqual(self.ws.read("a.txt"), before)
        self._no_leftovers()

    def test_write_external_change_same_mtime_rejected(self):
        """外部改了文件但把 mtime 还原到原值：只看 mtime 会漏检，(mtime_ns, size) 必须拦住。"""
        full = self._prepare()
        st = os.stat(full)
        with open(full, "w", encoding="utf-8") as f:
            f.write("被人改了")
        os.utime(full, ns=(st.st_atime_ns, st.st_mtime_ns))   # mtime 还原，模拟粗粒度文件系统
        r = WriteFile().execute({"path": "a.txt", "content": "新内容"}, self.ctx)
        self.assertTrue(r.is_error, r.content)
        self.assertIn("被修改过", r.content)

    def test_edit_unread_still_rejected(self):
        """没读过就改，照样拒绝（旧行为别丢）。"""
        self.ws.write("b.txt", "x")
        r = EditFile().execute({"path": "b.txt", "old_string": "x", "new_string": "y"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("先用 read_file", r.content)

    def test_write_new_file_permissions(self):
        """新文件权限保持 0o666 & ~umask（不能变成 mkstemp 的 0600）。"""
        r = WriteFile().execute({"path": "n.txt", "content": "hi"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        umask = os.umask(0)
        os.umask(umask)
        mode = os.stat(os.path.join(self.ws.dir, "n.txt")).st_mode & 0o777
        self.assertEqual(mode, 0o666 & ~umask)
        self._no_leftovers()

    def test_write_keeps_existing_permissions(self):
        """覆盖已有文件时权限位跟原文件走。"""
        full = self._prepare()
        os.chmod(full, 0o600)
        r = WriteFile().execute({"path": "a.txt", "content": "新"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertEqual(os.stat(full).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

"""跨平台子进程：杀进程树、找 bash。Windows 上没有 killpg / SIGKILL（#45）。"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from nanocode.proc import _is_wsl_stub, find_bash, kill_tree, popen_kwargs

# 拉起一个孙进程（模拟沙箱里的 pytest / dev server），把它的 pid 写进文件，然后自己也不退出
_CHILD = """
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as f:
    f.write(str(g.pid))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    """进程还在不在。Windows 的 os.kill 不认 0 号信号，只能去问 tasklist。"""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class KillTreeTest(unittest.TestCase):
    """超时/取消时只杀那一层 shell 是没有用的：它拉起的孙进程会继续跑、继续占端口。"""

    def test_grandchild_does_not_survive(self):
        pidfile = os.path.join(tempfile.mkdtemp(), "grandchild.pid")
        proc = subprocess.Popen([sys.executable, "-c", _CHILD, pidfile], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **popen_kwargs())
        self.addCleanup(kill_tree, proc)
        for _ in range(100):                       # 等孙进程把 pid 写下来
            if os.path.exists(pidfile):
                break
            time.sleep(0.1)
        self.assertTrue(os.path.exists(pidfile), "孙进程没有起来")
        gpid = int(open(pidfile).read())

        kill_tree(proc)
        proc.wait(timeout=20)
        deadline = time.time() + 15
        while time.time() < deadline and _alive(gpid):
            time.sleep(0.2)
        self.assertFalse(_alive(gpid), f"孙进程 {gpid} 没被一起带走——端口和工作区还被它占着")

    def test_already_exited_is_fine(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, **popen_kwargs())
        proc.wait(timeout=10)
        kill_tree(proc)                            # 不该抛异常，也不该卡住


class FindBashTest(unittest.TestCase):
    def test_wsl_stub_is_rejected(self):
        """`C:\\Windows\\System32\\bash.exe` 是 WSL 启动器，不是能用的 bash：

        命令跑在 WSL 视图里，`pwd` 给出的路径和 Windows 对不上（bash 工具靠 `pwd` 记工作
        目录），输出还是 UTF-16LE。宁可报"没找到 bash"，也不要静默用它。
        """
        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            self.assertTrue(_is_wsl_stub(r"C:\Windows\System32\bash.exe"))
            self.assertTrue(_is_wsl_stub(r"c:\windows\system32\bash.exe"))     # 大小写不敏感
            self.assertFalse(_is_wsl_stub(r"C:\Program Files\Git\bin\bash.exe"))
            self.assertFalse(_is_wsl_stub(r"C:\Windows\System32\cmd.exe"))

    def test_posix_uses_which(self):
        if os.name == "nt":
            self.skipTest("POSIX 上的行为")
        self.assertEqual(find_bash(), shutil.which("bash"))


if __name__ == "__main__":
    unittest.main()

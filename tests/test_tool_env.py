"""#134：工具子进程拿不到 nanocode 的凭据（NANOCODE_API_KEY / NANOCODE_WEB_TOKEN 等）。"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from nanocode.proc import find_bash, harden_process, tool_env

SECRETS = {
    "NANOCODE_API_KEY": "gw-token-should-not-leak",
    "NANOCODE_WEB_TOKEN": "w" * 40,
    "DEEPSEEK_API_KEY": "sk-deepseek-should-not-leak",
    "NCS_SECRET_KEY": "ncs-should-not-leak",
}


class ToolEnvTests(unittest.TestCase):
    def test_scrubs_nanocode_and_provider_keys(self):
        env = tool_env({**SECRETS, "PATH": "/bin", "HOME": "/h"})
        for k in SECRETS:
            self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_local_mode_keeps_user_tokens(self):
        env = tool_env({"GITHUB_TOKEN": "g", "NANOCODE_API_KEY": "x"})
        self.assertEqual(env.get("GITHUB_TOKEN"), "g")      # 本地模式不动用户自己的变量
        self.assertNotIn("NANOCODE_API_KEY", env)

    def test_platform_mode_scrubs_secret_like_names(self):
        env = tool_env({"NANOCODE_WEB_TOKEN": "w" * 40, "GITHUB_TOKEN": "g", "DB_PASSWORD": "p",
                        "MY_SECRET": "s", "LANG": "C.UTF-8", "NANOCODE_TOOL_ENV_KEEP": "MY_SECRET"})
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("DB_PASSWORD", env)
        self.assertEqual(env.get("MY_SECRET"), "s")         # 显式放行
        self.assertEqual(env["LANG"], "C.UTF-8")

    @unittest.skipIf(find_bash() is None, "需要 bash")
    def test_bash_tool_env_and_proc_environ(self):
        from nanocode.tools.shell import Bash as BashTool
        from nanocode.tools.base import ToolContext
        cmd = "env; cat /proc/self/environ 2>/dev/null | tr '\\0' '\\n'"
        with mock.patch.dict(os.environ, SECRETS):
            ctx = ToolContext(cwd=os.getcwd())
            out = BashTool().run({"command": cmd}, ctx)
        text = out if isinstance(out, str) else str(out)
        for k, v in SECRETS.items():
            self.assertNotIn(k, text)
            self.assertNotIn(v, text)
        self.assertIn("PATH=", text)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_harden_process_hides_parent_environ(self):
        code = ("import os,subprocess;from nanocode.proc import harden_process;harden_process();"
                "r=subprocess.run(['sh','-c','cat /proc/$PPID/environ'],capture_output=True);"
                "print('LEAK' if b'gw-token-should-not-leak' in r.stdout else 'OK')")
        env = {**os.environ, **SECRETS}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(r.stdout.strip(), "OK", r.stderr)
        harden_process  # noqa: B018  导入可用


if __name__ == "__main__":
    unittest.main()

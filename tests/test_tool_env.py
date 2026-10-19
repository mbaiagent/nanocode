"""#134：工具子进程拿不到 nanocode 的凭据（NANOCODE_API_KEY / NANOCODE_WEB_TOKEN 等）。"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from nanocode.proc import find_bash, tool_env

SECRETS = {
    "NANOCODE_API_KEY": "gw-token-should-not-leak",
    "NANOCODE_WEB_TOKEN": "w" * 40,
    "DEEPSEEK_API_KEY": "sk-deepseek-should-not-leak",
    "NCS_SECRET_KEY": "ncs-should-not-leak",
}


class ToolEnvTests(unittest.TestCase):
    def test_scrubs_nanocode_and_ncs_in_both_modes(self):
        for extra in ({}, {"NANOCODE_WEB_TOKEN": "w" * 40}):
            env = tool_env({**SECRETS, **extra, "PATH": "/bin", "HOME": "/h",
                            "NANOCODE_TOOL_ENV_KEEP": "NANOCODE_API_KEY,NCS_SECRET_KEY"})
            self.assertNotIn("NANOCODE_API_KEY", env)        # 不可放行
            self.assertNotIn("NANOCODE_WEB_TOKEN", env)
            self.assertNotIn("NCS_SECRET_KEY", env)
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_local_mode_keeps_vendor_keys_and_user_tokens(self):
        env = tool_env({"OPENAI_API_KEY": "o", "DEEPSEEK_API_KEY": "d", "GITHUB_TOKEN": "g", "NANOCODE_API_KEY": "x"})
        self.assertEqual(env.get("OPENAI_API_KEY"), "o")    # 本地模式不是回归：厂商 Key 照常传下去
        self.assertEqual(env.get("DEEPSEEK_API_KEY"), "d")
        self.assertEqual(env.get("GITHUB_TOKEN"), "g")
        self.assertNotIn("NANOCODE_API_KEY", env)

    def test_platform_mode_scrubs_vendor_and_secret_like_names(self):
        env = tool_env({"NANOCODE_WEB_TOKEN": "w" * 40, "OPENAI_API_KEY": "o", "GITHUB_TOKEN": "g",
                        "DB_PASSWORD": "p", "AWS_SECRET_ACCESS_KEY": "a", "LANG": "C.UTF-8"})
        for k in ("OPENAI_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(k, env)
        self.assertEqual(env["LANG"], "C.UTF-8")

    def test_keep_allows_vendor_keys_in_both_modes(self):
        for extra in ({}, {"NANOCODE_WEB_TOKEN": "w" * 40}):
            env = tool_env({**extra, "OPENAI_API_KEY": "o", "MY_SECRET": "s",
                            "NANOCODE_TOOL_ENV_KEEP": "OPENAI_API_KEY, my_secret"})
            self.assertEqual(env.get("OPENAI_API_KEY"), "o")
            self.assertEqual(env.get("MY_SECRET"), "s")

    def test_no_substring_false_positives(self):
        env = tool_env({"NANOCODE_WEB_TOKEN": "w" * 40, "TOKENIZERS_PARALLELISM": "false",
                        "PASSWORD_STORE_DIR": "/p", "SECRETS_DIR": "/s", "KEYBOARD": "us"})
        for k in ("TOKENIZERS_PARALLELISM", "PASSWORD_STORE_DIR", "SECRETS_DIR", "KEYBOARD"):
            self.assertIn(k, env)

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
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root 能读任何进程的 environ")
    def test_harden_process_hides_parent_environ(self):
        code = ("import os,subprocess;from nanocode.proc import harden_process;harden_process();"
                "r=subprocess.run(['sh','-c','cat /proc/$PPID/environ'],capture_output=True);"
                "print('LEAK' if b'gw-token-should-not-leak' in r.stdout else 'OK')")
        env = {**os.environ, **SECRETS}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(r.stdout.strip(), "OK", r.stderr)


if __name__ == "__main__":
    unittest.main()

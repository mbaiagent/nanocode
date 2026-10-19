"""根目录 .gitignore 必须挡住常见的密钥文件（#122）。

仓库是公开的，`git add -A` 一旦把 `.env` 或私钥带进提交就无法真正撤回，
所以这里用 `git check-ignore` 直接验证规则，而不是只比对文件文本。
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_git_checkout():
    return shutil.which("git") is not None and os.path.exists(os.path.join(ROOT, ".git"))


@unittest.skipUnless(_is_git_checkout(), "需要 git 和仓库检出目录")
class GitignoreSecretsTest(unittest.TestCase):
    def _ignored(self, path):
        # --no-index：即使文件已被跟踪也按规则判断；路径不需要真实存在。
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0

    def test_secret_files_are_ignored(self):
        for path in [
            ".env", ".env.local", ".env.production", "server/.env",
            "deploy/sandbox-host/.env", "deploy/compose/prod.env",
            "key.pem", "server/tls/server.key", "certs/client.p12",
            "id_rsa", "id_ed25519", "deploy/id_ecdsa",
        ]:
            with self.subTest(path=path):
                self.assertTrue(self._ignored(path), f"{path} 没被 .gitignore 忽略")

    def test_examples_and_public_keys_are_not_ignored(self):
        for path in [
            ".env.example", "deploy/compose/.env.example",
            "deploy/sandbox-host/.env.example", "id_rsa.pub", "id_ed25519.pub",
            "nanocode/tools/keys.py",
        ]:
            with self.subTest(path=path):
                self.assertFalse(self._ignored(path), f"{path} 不应该被忽略")

    def test_no_tracked_file_matches_ignore_rules(self):
        r = subprocess.run(
            ["git", "ls-files", "-ci", "--exclude-standard"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(r.stdout.strip(), "", "有已跟踪文件命中忽略规则：\n" + r.stdout)


if __name__ == "__main__":
    unittest.main()

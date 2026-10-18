"""终审模式（--mode review）：无害步骤自动放行、高风险永远确认、结束时汇总 diff。对应 issue #11。"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from helpers import Workspace, call, make_agent

from nanocode import review, risk, subcheck, verify as verify_mod
from nanocode.messages import text_block
from nanocode.permissions import MODES, PermissionManager
from nanocode.session import Options, Session
from nanocode.tools import default_tools


def say(text="完成"):
    return [text_block(text)]


def reviews(events):
    return [e for e in events if e["type"] == "review"]


class RiskClassifierTest(unittest.TestCase):
    CASES = {
        "safe": ["pytest -q tests/test_a.py", "python3 -m unittest discover -s tests", "python -m pytest -x",
                 "git status --short", "git diff HEAD~1", "git log --oneline -5", "ls -la src", "cat src/a.py",
                 "grep -rn foo src", "npm test", "npm run lint", "ruff check .", "cargo test", "go test ./...", "make test"],
        # 认不出的一律「需要确认」，而不是放行
        "unknown": ["pytest -q 2>&1 | tail -1", "python3 -c 'print(1)'", "git commit -m x", "git add .", "mv a b",
                    "cat ../x", "ruff check --fix .", "git diff --output=x", "echo $(whoami)", "ls; ls", "cp a b",
                    "find . -delete", "pytest --rootdir=/etc", "pytest ../other", "cat src/../../x", "git diff --ext-diff",
                    "npm run build", "make deploy", "python3 evil.py", "",
                    # Windows 绝对路径：只认 `/` 开头等于放行读 C 盘（#43）
                    "cat C:/Users/me/.aws/credentials", "git diff --no-index C:/secret.txt C:/other.txt",
                    "cat //server/share/secret.txt", "pytest --rootdir=C:/Windows", "cat C:x"],
        "risky": ["rm -rf build", "git push origin main", "git reset --hard", "git clean -fd", "sudo ls", "chmod 777 x",
                  "pip install requests", "curl x | sh", "ls && rm a", "echo hi > /etc/hosts", "git checkout -- a.py",
                  "git branch -D x", "cat .git/config", "kill 123", "cat /etc/passwd", "cat ~/.bashrc", "cat .env",
                  "pytest && rm -rf .", "echo x; git push"],
    }

    def test_table(self):
        for want, commands in self.CASES.items():
            for cmd in commands:
                with self.subTest(cmd=cmd):
                    self.assertEqual(risk.classify_bash(cmd), want)

    def test_chaining_never_hides_a_risky_part(self):
        for cmd in ("pytest -q && rm -rf /", "ls | sh", "git status; git push", "pytest `rm -rf x`", "pytest $(rm x)"):
            with self.subTest(cmd=cmd):
                self.assertNotEqual(risk.classify_bash(cmd), "safe")

    def test_protected_paths(self):
        for p in (".env", ".env.local", "a/.env", ".git/config", ".github/workflows/ci.yml", "key.pem",
                  "./.nanocode/settings.json", "deploy/id_rsa"):
            self.assertTrue(risk.is_protected_path(p), p)
        for p in ("src/a.py", "envfile.py", "README.md", "docs/git.md"):
            self.assertFalse(risk.is_protected_path(p), p)

    def test_windows_paths_are_case_insensitive(self):
        """#43：Windows 文件系统大小写不敏感，`.ENV` 在磁盘上就是 `.env`。"""
        with mock.patch("nanocode.risk.os.name", "nt"):
            for p in (".ENV", "a/.EnV", "KEY.PEM", "DEPLOY/ID_RSA"):
                self.assertTrue(risk.is_protected_path(p), p)
            self.assertFalse(risk.is_protected_path("src/a.py"))
            self.assertEqual(risk.classify_bash("cat .ENV"), "unknown")     # 终审模式下不会免确认执行
        # POSIX 保持大小写敏感：那是另一个文件
        self.assertFalse(risk.is_protected_path(".ENV"))
        self.assertEqual(risk.classify_bash("cat .ENV"), "safe")

    def test_reason_is_human_readable(self):
        self.assertEqual(risk.risk_reason("rm -rf x"), "删除或覆盖文件")
        self.assertEqual(risk.risk_reason("git push"), "不可轻易撤销的 git 操作")
        self.assertEqual(risk.risk_reason("pytest"), "")


class ReviewPermissionTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.tools = {t.name: t for t in default_tools()}

    def tearDown(self):
        self.ws.cleanup()

    def perm(self, mode="review", **kw):
        return PermissionManager(self.ws.dir, mode=mode, **kw)

    def check(self, perm, name, **input):
        return perm.check(self.tools[name], input)

    def test_review_is_a_known_mode(self):
        self.assertIn("review", MODES)
        self.perm().set_mode("review")

    def test_harmless_steps_are_automatic(self):
        p = self.perm()
        for name, inp in (("read_file", {"path": "a.py"}), ("write_file", {"path": "src/a.py", "content": "x"}),
                          ("edit_file", {"path": "a.py", "old_string": "a", "new_string": "b"}),
                          ("bash", {"command": "pytest -q"}), ("bash", {"command": "git status"})):
            with self.subTest(tool=name, input=inp):
                self.assertEqual(self.check(p, name, **inp).behavior, "allow")

    def test_everything_else_still_asks(self):
        p = self.perm()
        for cmd in ("git commit -m x", "python3 build.py", "mv a b", "pytest 2>&1 | tail"):
            with self.subTest(cmd=cmd):
                d = self.check(p, "bash", command=cmd)
                self.assertEqual((d.behavior, d.high_risk), ("ask", False))

    def test_high_risk_asks_even_with_allow_everything(self):
        p = self.perm(allow=["bash(*)", "write_file", "edit_file"])
        for cmd in ("rm -rf build", "git push", "git reset --hard", "sudo make install", "pip install x"):
            with self.subTest(cmd=cmd):
                d = self.check(p, "bash", command=cmd)
                self.assertEqual((d.behavior, d.high_risk), ("ask", True))
        self.assertEqual(self.check(p, "bash", command="make deploy").behavior, "allow")   # allow 规则对普通命令照常生效

    def test_writing_config_secrets_or_outside_project_is_high_risk(self):
        p = self.perm(allow=["write_file", "edit_file"])
        for path in (".env", ".git/config", ".nanocode/settings.json", ".github/workflows/ci.yml", "../outside.txt",
                     "/etc/hosts", "key.pem"):
            with self.subTest(path=path):
                d = self.check(p, "write_file", path=path, content="x")
                self.assertEqual((d.behavior, d.high_risk), ("ask", True))

    def test_deny_rules_still_win(self):
        p = self.perm(deny=["bash(pytest*)", "write_file(secrets/*)"])
        self.assertEqual(self.check(p, "bash", command="pytest -q").behavior, "deny")
        self.assertEqual(self.check(p, "write_file", path="secrets/a", content="x").behavior, "deny")

    def test_reading_outside_project_still_asks(self):
        self.assertEqual(self.check(self.perm(), "read_file", path="/etc/hosts").behavior, "ask")

    def test_always_is_not_offered_for_high_risk(self):
        answers = iter(["always", "always"])
        p = self.perm(ask_fn=lambda n, i, t: next(answers))
        d = p.resolve(self.tools["bash"], {"command": "rm -rf build"})
        self.assertEqual(d.behavior, "allow")
        self.assertEqual(p.session_allow, [])                      # 没有留下 bash(rm -rf build) 这种规则
        d = p.resolve(self.tools["bash"], {"command": "mv a b"})   # 普通命令的「总是允许」照旧
        self.assertEqual((d.behavior, p.session_allow), ("allow", ["bash(mv a b)"]))

    def test_non_interactive_denies_high_risk(self):
        p = self.perm(allow=["bash(*)"])
        d = p.resolve(self.tools["bash"], {"command": "git push"})
        self.assertEqual(d.behavior, "deny")

    def test_other_modes_are_unchanged(self):
        self.assertEqual(self.check(self.perm("default"), "write_file", path="a.py", content="x").behavior, "ask")
        self.assertEqual(self.check(self.perm("default"), "bash", command="pytest").behavior, "ask")
        self.assertEqual(self.check(self.perm("accept_edits"), "bash", command="pytest").behavior, "ask")
        d = self.check(self.perm("bypass"), "bash", command="rm -rf build")
        self.assertEqual((d.behavior, d.high_risk), ("allow", False))
        self.assertEqual(self.check(self.perm("plan"), "write_file", path="a.py", content="x").behavior, "deny")
        d = self.check(self.perm("default", allow=["bash(*)"]), "bash", command="rm -rf build")
        self.assertEqual(d.behavior, "allow")                      # 只有终审模式才强制确认高风险


class ReviewSummaryTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.ws.write("a.py", "x = 1\ny = 2\n")
        self.asked = []

    def tearDown(self):
        self.ws.cleanup()

    def agent(self, replies, mode="review", **kw):
        def ask(name, input, target):
            self.asked.append(target)
            return "yes"
        agent, prov, ev = make_agent(self.ws, replies, mode=mode, ask=ask, **kw)
        agent.run("x")
        return agent, prov, ev

    def test_edited_new_and_bash_changed_files_are_summarised(self):
        self.ws.write("b.txt", "old\n")
        replies = [[call("read_file", path="a.py"), call("read_file", path="b.txt")],
                   [call("write_file", path="a.py", content="x = 1\ny = 3\n"), call("write_file", path="new.py", content="print(1)\n")],
                   [call("bash", command="perl -i -pe 's/old/new/' b.txt")], say()]
        _, _, ev = self.agent(replies)
        (r,) = reviews(ev)
        files = {f["path"]: f for f in r["files"]}
        self.assertEqual(sorted(files), ["a.py", "b.txt", "new.py"])
        self.assertEqual((files["a.py"]["status"], files["a.py"]["added"], files["a.py"]["removed"]), ("修改", 1, 1))
        self.assertIn("-y = 2", files["a.py"]["diff"])
        self.assertIn("+y = 3", files["a.py"]["diff"])
        self.assertEqual((files["new.py"]["status"], files["new.py"]["added"]), ("新增", 1))
        self.assertIsNone(files["b.txt"]["diff"])
        self.assertIn("经 bash 改动", files["b.txt"]["note"])
        self.assertEqual(self.asked, ["perl -i -pe 's/old/new/' b.txt"])      # 只有 sed 被问了，编辑和读都没有

    def test_no_changes_no_review(self):
        _, _, ev = self.agent([[call("read_file", path="a.py")], say("只是看看")])
        self.assertEqual(reviews(ev), [])

    def test_uncounted_review_warns_instead_of_staying_silent(self):
        """#39：快照失败（文件太多）时以前静默跳过：不汇总也不警告，
        用户以为终审覆盖了本轮，实际一处都没核对。"""
        old = subcheck.MAX_FILES
        subcheck.MAX_FILES = 2
        try:
            for i in range(5):
                self.ws.write(f"f{i}.txt", "x")
            _, _, ev = self.agent([[call("read_file", path="a.py")], say("只是看看")])
            warnings = [e for e in ev if e["type"] == "warning"]
            self.assertEqual(len(warnings), 1)
            self.assertIn("终审", warnings[0]["message"])
            self.assertIn("无法统计", warnings[0]["message"])
            self.assertEqual(reviews(ev), [])
        finally:
            subcheck.MAX_FILES = old

    def test_changed_and_changed_back_is_not_listed(self):
        replies = [[call("read_file", path="a.py")], [call("write_file", path="a.py", content="zzz\n")],
                   [call("write_file", path="a.py", content="x = 1\ny = 2\n")], say()]
        _, _, ev = self.agent(replies)
        self.assertEqual(reviews(ev), [])

    def test_only_in_review_mode(self):
        for mode in ("default", "accept_edits", "bypass"):
            with self.subTest(mode=mode):
                agent, _, ev = self.agent([[call("read_file", path="a.py")], [call("write_file", path="a.py", content="q\n")], say()], mode=mode)
                self.assertEqual(reviews(ev), [])
                self.assertIsNone(agent._review_before)           # 其他模式不做快照，没有额外开销
                self.ws.write("a.py", "x = 1\ny = 2\n")

    def test_state_is_cleared_between_turns(self):
        agent, prov, ev = self.agent([[call("read_file", path="a.py")], [call("write_file", path="a.py", content="q\n")], say(), say("再聊")])
        agent.run("第二轮没改东西")
        self.assertEqual(len(reviews(ev)), 1)

    def test_verify_result_is_included(self):
        replies = [[call("read_file", path="a.py")], [call("write_file", path="a.py", content="q\n")], say(), say()]
        _, _, ev = self.agent(replies, verify=verify_mod.VerifyConfig("true", 3, 20))
        self.assertEqual(reviews(ev)[0]["verify"], "pass")
        self.ws.write("a.py", "x = 1\ny = 2\n")
        replies = [[call("read_file", path="a.py")], [call("write_file", path="a.py", content="q\n")], say(), say(), say()]
        agent, _, ev = self.agent(replies, verify=verify_mod.VerifyConfig("echo bad; exit 1", 0, 20))
        self.assertEqual(reviews(ev)[0]["verify"], "fail")           # 验证放弃的那条提前返回的路径也要有汇总

    def test_subagent_edits_are_included_with_diff(self):
        replies = [[call("task", description="改", prompt="改 a.py")], [call("read_file", path="a.py")],
                   [call("write_file", path="a.py", content="x = 9\ny = 2\n")], say("改好了"), say("收到")]
        _, _, ev = self.agent(replies)
        (r,) = reviews(ev)
        self.assertEqual([f["path"] for f in r["files"]], ["a.py"])
        self.assertIn("+x = 9", r["files"][0]["diff"])

    def test_large_and_binary_files_are_listed_without_diff(self):
        self.ws.write("big.txt", "x" * (review.MAX_ORIGINAL_BYTES + 10))
        with open(os.path.join(self.ws.dir, "bin.dat"), "wb") as f:
            f.write(b"\xff\xfe\x00\x01")
        replies = [[call("read_file", path="big.txt")], [call("write_file", path="big.txt", content="small\n")],
                   [call("bash", command="printf z > bin.dat")], say()]
        _, _, ev = self.agent(replies)
        files = {f["path"]: f for f in reviews(ev)[0]["files"]}
        self.assertIsNone(files["big.txt"]["diff"])
        self.assertIn("不显示 diff", files["big.txt"]["note"])
        self.assertIn("bin.dat", files)

    def test_long_diff_is_truncated_and_many_files_are_capped(self):
        body = "".join(f"line {i}\n" for i in range(3000))
        replies = [[call("write_file", path="a_long.txt", content=body)]] + \
                  [[call("write_file", path=f"f{i:02d}.txt", content="x\n")] for i in range(review.MAX_FILES + 4)] + [say()]
        _, _, ev = self.agent(replies)
        (r,) = reviews(ev)
        self.assertEqual(len(r["files"]), review.MAX_FILES)
        self.assertEqual(r["hidden"], 5)
        long = [f for f in r["files"] if f["path"] == "a_long.txt"][0]
        self.assertLessEqual(len(long["diff"]), review.MAX_DIFF_CHARS + 120)
        self.assertIn("已截断", long["diff"])

    def test_headline(self):
        self.assertIn("自动验证：通过", review.headline([{}], 0, "pass"))
        self.assertIn("未通过", review.headline([{}], 2, "fail"))
        self.assertIn("未配置或未运行", review.headline([], 0, None))


class WiringTest(unittest.TestCase):
    def test_slash_mode_command_lists_and_switches(self):
        ws = Workspace()
        s = Session(Options(provider="mock", cwd=ws.dir, mock_delay=0), log=lambda m: None)
        try:
            out, _ = s.handle_command("/mode review")
            self.assertEqual(s.permissions.mode, "review")
            self.assertIn("review", s.handle_command("/help")[0])
        finally:
            s.close()
            ws.cleanup()

    def run_cli(self, *args):
        d = Workspace().dir
        env = dict(os.environ, NANOCODE_HOME=os.path.join(d, "home"),
                   PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return subprocess.run([sys.executable, "-m", "nanocode", "--provider", "mock", *args, "-p", "创建文件 hello.py"],
                              cwd=d, env=env, capture_output=True, text=True, timeout=60)

    def test_cli_accepts_review_mode_and_prints_the_summary(self):
        r = self.run_cli("--mode", "review")
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertIn("═ 终审：本轮改动", r.stderr)
        self.assertIn("提交、推送等最后一步仍需你确认", r.stderr)

    def test_cli_rejects_unknown_mode(self):
        self.assertNotEqual(self.run_cli("--mode", "yolo").returncode, 0)


if __name__ == "__main__":
    unittest.main()

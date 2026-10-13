"""评测框架自检：判定脚本必须"参考答案能过、什么都不做过不了"，否则评测出来的分数没有意义。"""

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import helpers  # noqa: F401  —— 设置 NANOCODE_HOME
from helpers import call, make_agent
from nanocode import verify as verify_mod
from nanocode.messages import text_block

RUN_PY = os.path.join(os.path.dirname(__file__), "..", "evals", "agent_tasks", "run.py")
spec = importlib.util.spec_from_file_location("agent_eval_run", RUN_PY)
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


class CheckerTest(unittest.TestCase):
    def test_every_task_has_working_checker(self):
        tasks = run.load_tasks()
        self.assertEqual(len(tasks), 13)
        for task in tasks:
            with self.subTest(task=task["id"]):
                for name in ("task.json", "check.py"):
                    self.assertTrue(os.path.isfile(os.path.join(task["dir"], name)))
                self.assertTrue(task["prompt"].strip())

                untouched = run.prepare(task)
                try:
                    passed, reason = run.run_check(task, untouched, "")
                    self.assertFalse(passed, f"什么都不做也通过了：{reason}")
                finally:
                    shutil.rmtree(untouched)

                solved = run.prepare(task)
                try:
                    answer = run.apply_reference(task, solved)
                    passed, reason = run.run_check(task, solved, answer)
                    self.assertTrue(passed, f"参考答案没通过：{reason}")
                finally:
                    shutil.rmtree(solved)

    def test_trap_tasks_catch_the_typical_first_attempt(self):
        """T11–T13 的意义：第一次最容易写出的做法（各任务的 naive/）过不了判定，否则测不出验证的作用。
        分两类，测的东西不同，所以要分别断言：
          caught_by_visible  典型的第一次尝试过不了 Agent 看得到的测试 —— 跑一遍就能发现（--verify 能直接帮上忙）
          needs_own_checks   典型的第一次尝试能通过可见测试 —— 只有读规格 / 自己写用例才发现（看验证会不会带来假自信）"""
        trap_tasks = [t for t in run.load_tasks() if os.path.isdir(os.path.join(t["dir"], "naive"))]
        self.assertEqual([t["id"][:3] for t in trap_tasks], ["T11", "T12", "T13"])
        for task in trap_tasks:
            with self.subTest(task=task["id"]):
                self.assertIn(task["trap"], ("caught_by_visible", "needs_own_checks"))
                work = run.prepare(task)
                try:
                    naive = os.path.join(task["dir"], "naive")
                    for name in os.listdir(naive):
                        shutil.copy(os.path.join(naive, name), os.path.join(work, name))
                    visible = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", work, "-p", "test_*.py"],
                                             cwd=work, capture_output=True, text=True, timeout=60)
                    if task["trap"] == "caught_by_visible":
                        self.assertNotEqual(visible.returncode, 0, "这类陷阱应该被可见测试抓到，否则跑检查没有意义")
                    else:
                        self.assertEqual(visible.returncode, 0, "第一次尝试应该能通过可见测试：\n" + visible.stderr[-800:])
                    passed, reason = run.run_check(task, work, "")
                    self.assertFalse(passed, f"第一次尝试居然通过了判定，这个任务测不出验证的作用：{reason}")
                finally:
                    shutil.rmtree(work)

    def test_agent_cannot_see_hidden_tests_or_solution(self):
        for task in run.load_tasks():
            work = run.prepare(task)
            try:
                names = {n for _, _, files in os.walk(work) for n in files}
                self.assertFalse(any(n.startswith(("test_hidden", "mutant_")) for n in names), task["id"])
                self.assertNotIn("naive", {d for _, dirs, _ in os.walk(work) for d in dirs}, task["id"])
                self.assertNotIn("__answer__", names)
            finally:
                shutil.rmtree(work)

    def test_read_only_task_detects_modification(self):
        task = run.load_tasks("T06")[0]
        work = run.prepare(task)
        try:
            with open(os.path.join(work, "app", "audit.py"), "a") as f:
                f.write("\n# touched\n")
            passed, reason = run.run_check(task, work, "storage/audit_store.py 里的 write_audit_record")
            self.assertFalse(passed)
            self.assertIn("只读", reason)
        finally:
            shutil.rmtree(work)

    def test_editing_visible_tests_does_not_help(self):
        task = run.load_tasks("T01")[0]
        work = run.prepare(task)
        try:
            with open(os.path.join(work, "test_calc.py"), "w") as f:
                f.write("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
            passed, _ = run.run_check(task, work, "")
            self.assertFalse(passed)
        finally:
            shutil.rmtree(work)


class VerifySensitivityTest(unittest.TestCase):
    """用脚本化的"模型"证明：这套评测能看出自动验证（#9）有没有用。
    剧本：先写出典型的第一次尝试（naive/），如果收到检查失败的反馈，再改成参考答案。
    注意这不是真实模型的结果，只证明评测本身有区分度；真实数据要用 run.py 配真实模型去跑。"""

    VISIBLE_TESTS = f"{shlex.quote(sys.executable)} -m unittest discover -s . -p 'test_*.py'"

    def attempt(self, task_id, verify):
        task = run.load_tasks(task_id)[0]
        work = run.prepare(task)
        self.addCleanup(shutil.rmtree, work, True)

        def writes(kind):
            d = os.path.join(task["dir"], kind)
            out = []
            for name in sorted(os.listdir(d)):
                if os.path.exists(os.path.join(work, name)):      # 覆盖已有文件前必须先读，这是工具的保护
                    out.append(call("read_file", path=name))
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    out.append(call("write_file", path=name, content=f.read()))
            return out

        cfg = verify_mod.VerifyConfig(self.VISIBLE_TESTS, 3, 60) if verify else None
        ws = type("W", (), {"dir": work})
        agent, _, events = make_agent(ws, [writes("naive"), [text_block("做完了")], writes("solution"), [text_block("修好了")]],
                                      verify=cfg)
        agent.run(task["prompt"])
        passed, _ = run.run_check(task, work, "")
        return passed, [e["status"] for e in events if e["type"] == "verify"]

    def test_verify_rescues_a_trap_that_visible_tests_can_see(self):
        passed, _ = self.attempt("T11", verify=False)
        self.assertFalse(passed)
        passed, statuses = self.attempt("T11", verify=True)
        self.assertTrue(passed)
        self.assertEqual(statuses, ["running", "fail", "running", "pass"])

    def test_verify_does_not_rescue_a_trap_visible_tests_cannot_see(self):
        for tid in ("T12", "T13"):
            with self.subTest(task=tid):
                passed, statuses = self.attempt(tid, verify=True)
                self.assertFalse(passed)                       # 检查是绿的，但活其实没干对
                self.assertEqual(statuses[-1], "pass")


class RunnerTest(unittest.TestCase):
    def test_mock_agent_run_collects_stats(self):
        task = run.load_tasks("T01")[0]
        work = run.prepare(task)
        try:
            args = SimpleNamespace(provider="mock", model="", base_url="", protocol="", mode="bypass",
                                   max_steps=10, timeout=60)
            stats = run.run_agent(task, work, args)
            self.assertGreater(stats["steps"], 0)
            self.assertGreater(stats["tool_calls"], 0)
            self.assertFalse(stats["timed_out"])
            passed, _ = run.run_check(task, work, stats["final"])
            self.assertFalse(passed)      # Mock 模型按固定剧本走，不会真的修 bug
        finally:
            shutil.rmtree(work)

    def test_main_writes_reports(self):
        out = tempfile.mkdtemp()
        try:
            self.assertEqual(run.main(["--reference", "--tasks", "T01,T04", "--out", out]), 0)
            files = sorted(os.listdir(out))
            self.assertEqual([f.rsplit(".", 1)[1] for f in files], ["json", "md"])
            with open(os.path.join(out, files[1]), encoding="utf-8") as f:
                self.assertIn("通过率：2/2", f.read())
        finally:
            shutil.rmtree(out)


if __name__ == "__main__":
    unittest.main()

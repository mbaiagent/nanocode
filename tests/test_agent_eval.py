"""评测框架自检：判定脚本必须"参考答案能过、什么都不做过不了"，否则评测出来的分数没有意义。"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401  —— 设置 NANOCODE_HOME
from helpers import call, make_agent
from nanocode import verify as verify_mod
from nanocode.llm.base import LLMError, Provider
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
                                             cwd=work, capture_output=True, text=True, encoding="utf-8",
                                             errors="replace", timeout=60)
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

    # 双引号 cmd 和 bash 都认；单引号只有 bash 认（#92）
    VISIBLE_TESTS = f'"{sys.executable}" -m unittest discover -s . -p "test_*.py"'

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
                                   max_steps=10, timeout=60, verify="")
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

    def test_verify_flag_reaches_the_agent_and_the_report(self):
        """--verify 要真的传给 Agent（回合结束前会跑这条命令），并写进报告，不然对照实验没法区分两组。"""
        out = tempfile.mkdtemp()
        try:
            self.assertEqual(run.main(["--provider", "mock", "--tasks", "T01", "--verify", "true", "--out", out]), 0)
            files = sorted(os.listdir(out))
            with open(os.path.join(out, files[0]), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["meta"]["verify"], "true")
            with open(os.path.join(out, files[1]), encoding="utf-8") as f:
                self.assertIn("自动验证：开（true）", f.read())
        finally:
            shutil.rmtree(out)

    def test_verify_really_gates_the_agent_run(self):
        """检查命令永远失败时，开了 --verify 的 Agent 被打回重试，所以步数更多；关着则一圈走完。"""
        def steps(verify):
            task = run.load_tasks("T01")[0]
            work = run.prepare(task)
            try:
                args = SimpleNamespace(provider="mock", model="", base_url="", protocol="", mode="bypass",
                                       max_steps=10, timeout=60, verify=verify)
                return run.run_agent(task, work, args)["steps"]
            finally:
                shutil.rmtree(work)
        self.assertGreater(steps("false"), steps(""))

    def test_verify_is_off_by_default(self):
        out = tempfile.mkdtemp()
        try:
            run.main(["--provider", "mock", "--tasks", "T01", "--out", out])
            with open(os.path.join(out, sorted(os.listdir(out))[1]), encoding="utf-8") as f:
                self.assertIn("自动验证：关", f.read())
        finally:
            shutil.rmtree(out)


class _FailingProvider(Provider):
    """模型接口一直报错（比如免费额度用完），用来模拟"根本没调通"。"""

    name = "failing"

    def __init__(self, *a, **k):
        super().__init__("failing")

    def complete(self, system, messages, tools, max_tokens=4096, on_text=None):
        raise LLMError("HTTP 429：配额已用完，不再重试。", status=429)


def _infra_stats():
    return {"final": "模型调用失败：HTTP 429", "error": "HTTP 429：配额已用完", "infra_error": True, "timed_out": False,
            "steps": 1, "tool_calls": 0, "tool_errors": 0, "denied": 0, "tools_used": [], "input_tokens": 0,
            "output_tokens": 0, "usage_estimated": False, "seconds": 0.1}


class InfraErrorTest(unittest.TestCase):
    """模型接口出错（配额、网络）不能算成"模型没做好"，否则 0/13 会被当成模型的成绩。"""

    ARGS = SimpleNamespace(provider="mock", model="", base_url="", protocol="", mode="bypass",
                           max_steps=5, timeout=60, verify="")

    def _run_agent(self):
        task = run.load_tasks("T01")[0]
        work = run.prepare(task)
        try:
            return run.run_agent(task, work, self.ARGS)
        finally:
            shutil.rmtree(work)

    def test_api_failure_is_flagged(self):
        with mock.patch("nanocode.session.make_provider", lambda *a, **k: _FailingProvider()):
            stats = self._run_agent()
        self.assertTrue(stats["infra_error"])
        self.assertIn("429", stats["error"])

    def test_normal_run_is_not_flagged(self):
        self.assertFalse(self._run_agent()["infra_error"])

    def test_summary_leaves_infra_runs_out_of_the_pass_rate(self):
        good = {"task": "T01", "passed": True, "check_reason": "ok", "steps": 4, "seconds": 2.0}
        bad = {"task": "T01", "passed": False, "check_reason": "FAIL", "steps": 4, "seconds": 2.0}
        broken = {"task": "T01", "passed": False, "check_reason": "模型接口出错", "infra_error": True, "steps": 1}
        (row,) = run.summarize([good, bad, broken, broken])
        self.assertEqual((row["passed"], row["runs"], row["infra_errors"]), (1, 2, 2))
        self.assertEqual(row["steps"], 4.0)       # 平均值也只算有效运行

    def test_report_with_only_infra_errors_has_no_pass_rate(self):
        meta = {"provider": "p", "model": "m", "date": "-", "commit": "", "python": "3", "platform": "x", "mode": "bypass",
                "max_steps": 30, "timeout": 600, "repeat": 1, "verify": "", "aborted": True, "max_infra_errors": 3}
        rows = run.summarize([{"task": "T01", "passed": False, "check_reason": "x", "infra_error": True}])
        report = run.markdown(meta, rows)
        self.assertIn("没有一次有效运行", report)
        self.assertNotIn("通过率：0/", report)
        self.assertIn("未计入通过率", report)
        self.assertIn("提前停止", report)
        self.assertIn("（+1 次接口出错）", report)

    def _main(self, *extra):
        out = tempfile.mkdtemp()
        calls = []
        def fake(task, work, args):
            calls.append(task["id"])
            return _infra_stats()
        try:
            with mock.patch.object(run, "run_agent", fake):
                code = run.main(["--provider", "mock", "--tasks", "T01,T02", "--repeat", "5", "--out", out, *extra])
            with open(os.path.join(out, sorted(os.listdir(out))[1]), encoding="utf-8") as f:
                return code, calls, f.read()
        finally:
            shutil.rmtree(out)

    def test_main_stops_early_after_consecutive_infra_errors(self):
        code, calls, report = self._main("--max-infra-errors", "3")
        self.assertEqual(len(calls), 3)           # 10 次运行里只跑了 3 次就停了
        self.assertEqual(code, 1)                 # 非 0：脚本能知道这轮没跑完
        self.assertIn("提前停止", report)

    def test_max_infra_errors_zero_runs_everything(self):
        code, calls, _ = self._main("--max-infra-errors", "0")
        self.assertEqual(len(calls), 10)
        self.assertEqual(code, 0)

    def test_a_good_run_resets_the_streak(self):
        out = tempfile.mkdtemp()
        seq = iter([True, True, False, True, True, False])
        def fake(task, work, args):
            stats = _infra_stats()
            if not next(seq):
                stats.update(infra_error=False, final="完成", error="")
            return stats
        try:
            with mock.patch.object(run, "run_agent", fake):
                code = run.main(["--provider", "mock", "--tasks", "T01", "--repeat", "6", "--out", out,
                                 "--max-infra-errors", "3"])
        finally:
            shutil.rmtree(out)
        self.assertEqual(code, 0)                 # 中间有正常运行，连续错误从没到 3 次，所以 6 次都跑完了


if __name__ == "__main__":
    unittest.main()

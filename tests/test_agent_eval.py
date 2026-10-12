"""评测框架自检：判定脚本必须"参考答案能过、什么都不做过不了"，否则评测出来的分数没有意义。"""

import importlib.util
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import helpers  # noqa: F401  —— 设置 NANOCODE_HOME

RUN_PY = os.path.join(os.path.dirname(__file__), "..", "evals", "agent_tasks", "run.py")
spec = importlib.util.spec_from_file_location("agent_eval_run", RUN_PY)
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


class CheckerTest(unittest.TestCase):
    def test_every_task_has_working_checker(self):
        tasks = run.load_tasks()
        self.assertEqual(len(tasks), 10)
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

    def test_agent_cannot_see_hidden_tests_or_solution(self):
        for task in run.load_tasks():
            work = run.prepare(task)
            try:
                names = {n for _, _, files in os.walk(work) for n in files}
                self.assertFalse(any(n.startswith(("test_hidden", "mutant_")) for n in names), task["id"])
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

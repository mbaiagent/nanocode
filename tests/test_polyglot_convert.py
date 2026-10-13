"""Polyglot 适配脚本：用一个假的迷你题库验证转换、自检、判定，不依赖网络和真实题库。对应 issue #15。"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

import helpers  # noqa: F401  —— 设置 NANOCODE_HOME

ROOT = os.path.join(os.path.dirname(__file__), "..", "evals")


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


convert = load("polyglot_convert", "polyglot/convert.py")
run = load("polyglot_run", "agent_tasks/run.py")

STUB = "def add(a, b):\n    raise NotImplementedError\n"
GOOD = "def add(a, b):\n    return a + b\n"
TEST = ("import unittest\nfrom demo import add\n\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
        "    def test_neg(self):\n        self.assertEqual(add(-1, 1), 0)\n")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_exercise(src, name, *, stub=STUB, test=TEST, example=GOOD, instructions="Add two numbers.",
                  append=None, config_files=None, extra=None):
    d = os.path.join(src, "python", "exercises", "practice", name)
    os.makedirs(os.path.join(d, ".meta"))
    os.makedirs(os.path.join(d, ".docs"))
    files = config_files or {"solution": ["demo.py"], "test": ["demo_test.py"], "example": [".meta/example.py"]}
    with open(os.path.join(d, ".meta", "config.json"), "w") as f:
        json.dump({"files": files}, f)
    for rel, text in {"demo.py": stub, "demo_test.py": test, ".meta/example.py": example, **(extra or {})}.items():
        with open(os.path.join(d, rel), "w") as f:
            f.write(text)
    if instructions is not None:
        with open(os.path.join(d, ".docs", "instructions.md"), "w") as f:
            f.write(instructions)
    if append:
        with open(os.path.join(d, ".docs", "instructions.append.md"), "w") as f:
            f.write(append)
    return d


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pg-test-")
        self.src = os.path.join(self.tmp, "src")
        self.out = os.path.join(self.tmp, "out")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def convert(self, *extra):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = convert.main(["--src", self.src, "--out", self.out, *extra])
        return code, err.getvalue()

    def task(self, name="PG-py-ok"):
        return os.path.join(self.out, name)

    def check(self, name, work):
        return convert.run_check(self.task(name), work)

    def fresh_work(self, name, with_solution=False):
        work = tempfile.mkdtemp(dir=self.tmp)
        shutil.copytree(os.path.join(self.task(name), "files"), work, dirs_exist_ok=True)
        if with_solution:
            shutil.copytree(os.path.join(self.task(name), "solution"), work, dirs_exist_ok=True)
        return work

    # ------------------------------------------------------------------ 转换结果

    def test_layout_and_prompt(self):
        make_exercise(self.src, "ok", instructions="Add two numbers.", append="Use ints only.")
        code, _ = self.convert()
        self.assertEqual(code, 0)
        t = self.task()
        for rel in ("task.json", "check.py", "files/demo.py", "files/demo_test.py", "hidden/demo_test.py",
                    "solution/demo.py"):
            self.assertTrue(os.path.isfile(os.path.join(t, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(t, "files", ".meta")))   # 参考答案不能漏进 Agent 看得到的目录
        self.assertFalse(os.path.exists(os.path.join(t, "files", ".docs")))
        task = read_json(os.path.join(t, "task.json"))
        self.assertEqual(task["id"], "PG-py-ok")
        for needle in ("`demo.py`", "`demo_test.py`", "不要修改测试文件", "Add two numbers.", "Use ints only."):
            self.assertIn(needle, task["prompt"])
        with open(os.path.join(t, "files", "demo.py")) as f:
            self.assertEqual(f.read(), STUB)                                # 给 Agent 的是桩，不是答案

    def test_reference_passes_and_untouched_fails(self):
        make_exercise(self.src, "ok")
        self.convert()
        self.assertTrue(self.check("PG-py-ok", self.fresh_work("PG-py-ok", with_solution=True))[0])
        ok, why = self.check("PG-py-ok", self.fresh_work("PG-py-ok"))
        self.assertFalse(ok)

    def test_editing_the_visible_test_does_not_help(self):
        make_exercise(self.src, "ok")
        self.convert()
        work = self.fresh_work("PG-py-ok")
        with open(os.path.join(work, "demo_test.py"), "w") as f:     # Agent 把可见测试改成必过
            f.write("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n")
        self.assertFalse(self.check("PG-py-ok", work)[0])

    def test_deleting_the_visible_test_does_not_help_either(self):
        make_exercise(self.src, "ok")
        self.convert()
        work = self.fresh_work("PG-py-ok", with_solution=True)
        os.remove(os.path.join(work, "demo_test.py"))
        self.assertTrue(self.check("PG-py-ok", work)[0])               # 测试被还原，答案对就是对
        work = self.fresh_work("PG-py-ok")
        os.remove(os.path.join(work, "demo_test.py"))
        self.assertFalse(self.check("PG-py-ok", work)[0])

    def test_helper_module_without_tests_is_fine(self):
        """类似 paasio：files.test 里除了测试文件还列了只放辅助代码的模块。"""
        make_exercise(self.src, "helper",
                      config_files={"solution": ["demo.py"], "test": ["demo_test.py", "test_utils.py"],
                                    "example": [".meta/example.py"]},
                      extra={"test_utils.py": "ZEN = 'x'\n"})
        code, _ = self.convert()
        self.assertEqual(code, 0)
        self.assertTrue(self.check("PG-py-helper", self.fresh_work("PG-py-helper", with_solution=True))[0])

    # ------------------------------------------------------------------ 自检挡掉有问题的题

    def test_bad_exercises_are_skipped_with_reasons(self):
        make_exercise(self.src, "ok")
        make_exercise(self.src, "bad-example", example="def add(a, b):\n    return 0\n")
        make_exercise(self.src, "trivial", stub=GOOD)
        make_exercise(self.src, "no-docs", instructions=None)
        make_exercise(self.src, "bad-config", config_files={"solution": ["demo.py"], "test": ["demo_test.py"],
                                                            "example": []})
        code, err = self.convert()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.out)), ["PG-py-ok"])
        self.assertIn("bad-example：参考答案没能通过测试", err)
        self.assertIn("trivial：桩代码不做任何修改就通过", err)
        self.assertIn("no-docs：没有 .docs/instructions.md", err)
        self.assertIn("bad-config：config.json", err)

    def test_nothing_convertible_exits_nonzero(self):
        make_exercise(self.src, "trivial", stub=GOOD)
        code, _ = self.convert()
        self.assertEqual(code, 1)

    # ------------------------------------------------------------------ 选题

    def test_selection_is_reproducible(self):
        names = [f"ex{i:02d}" for i in range(20)]
        a = convert.select(names, "", 5, 0)
        self.assertEqual(a, convert.select(names, "", 5, 0))
        self.assertEqual(len(a), 5)
        self.assertEqual(a, sorted(a))
        self.assertNotEqual(a, convert.select(names, "", 5, 1))
        self.assertEqual(convert.select(names, "", 0, 0), names)
        self.assertEqual(convert.select(names, "ex03,ex01", 5, 0), ["ex03", "ex01"])    # 指定题目优先于 --limit
        with self.assertRaises(SystemExit):
            convert.select(names, "nope", 0, 0)

    def test_limit_and_list(self):
        for n in ("a", "b", "c"):
            make_exercise(self.src, n)
        self.convert("--limit", "2", "--seed", "0")
        self.assertEqual(len(os.listdir(self.out)), 2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            convert.main(["--src", self.src, "--list"])
        self.assertEqual(buf.getvalue().split(), ["a", "b", "c"])

    def test_wrong_src_gives_clear_error(self):
        with self.assertRaises(SystemExit) as cm:
            convert.main(["--src", self.tmp, "--out", self.out])
        self.assertIn("polyglot-benchmark", str(cm.exception))

    # ------------------------------------------------------------------ 和运行器配合

    def test_runner_accepts_tasks_dir(self):
        make_exercise(self.src, "ok")
        make_exercise(self.src, "two", instructions="Second.")
        self.convert()
        results = os.path.join(self.tmp, "results")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = run.main(["--reference", "--tasks-dir", self.out, "--out", results])
        self.assertEqual(code, 0)
        data = read_json([os.path.join(results, f) for f in os.listdir(results) if f.endswith(".json")][0])
        runs = data["runs"] if isinstance(data, dict) and "runs" in data else data
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["passed"] for r in runs))

    def test_runner_default_tasks_unchanged_and_bad_dir_rejected(self):
        self.assertEqual(len(run.load_tasks()), 13)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run.main(["--reference", "--tasks-dir", os.path.join(self.tmp, "nope")]), 2)


if __name__ == "__main__":
    unittest.main()

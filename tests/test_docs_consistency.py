"""文档和代码必须一一对应：代码里有的命令行参数、配置项、环境变量、事件、模块、权限模式、斜杠命令、
工具和 task 参数，文档里都要有；文档里写的数字（任务数、工具数、代码行数、测试数）要和代码一致。

这个测试存在的原因：功能写完很容易忘了补文档，靠人眼核对一定会漏。新增任何上面这些东西而没写文档，
这里会直接失败，并列出缺了什么。
"""

import glob
import os
import re
import tempfile
import unittest

import helpers  # noqa: F401  —— 设置 NANOCODE_HOME

from nanocode import trust
from nanocode.permissions import MODES
from nanocode.session import Options, Session
from nanocode.tools import default_tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def source_files() -> list[str]:
    return sorted(p for p in glob.glob(os.path.join(ROOT, "nanocode", "**", "*.py"), recursive=True))


README = read("README.md")
CHAPTERS = {os.path.basename(p): read("docs", os.path.basename(p)) for p in glob.glob(os.path.join(ROOT, "docs", "*.md"))}
EVALS_README = read("evals", "agent_tasks", "README.md")
LEARN = read("nanocode", "web", "static", "learn.html")
ALL_DOCS = README + "\n" + "\n".join(CHAPTERS.values()) + "\n" + EVALS_README


def missing(items, haystack, fmt=lambda x: x) -> list[str]:
    return [i for i in items if fmt(i) not in haystack]


class CodeIsDocumentedTest(unittest.TestCase):
    def test_every_cli_flag_is_documented(self):
        flags = re.findall(r'add_argument\("(--[\w-]+)"', read("nanocode", "cli.py"))
        self.assertGreater(len(flags), 10)
        self.assertEqual(missing(flags, README), [], "这些命令行参数 README 里没有")

    def test_every_setting_is_documented(self):
        keys = set(re.findall(r'\bs\.get\("(\w+)"', read("nanocode", "session.py"))) - {"allow", "deny"}
        keys |= {"permissions"}
        self.assertGreater(len(keys), 10)
        self.assertEqual(missing(keys, ALL_DOCS, lambda k: f'"{k}"'), [], "这些 settings.json 配置项文档里没有（要写成带引号的键名）")

    def test_every_environment_variable_is_documented(self):
        names = set()
        for path in source_files():
            # 前后带下划线的是内部占位符（如 __NANOCODE_TOKEN__），不是用户可以设置的环境变量
            names |= set(re.findall(r"(?<![A-Z_])NANOCODE_[A-Z_]*[A-Z](?![A-Z_])", open(path, encoding="utf-8").read()))
        self.assertIn("NANOCODE_HOME", names)
        self.assertEqual(missing(names, ALL_DOCS), [], "这些环境变量文档里没有")

    def test_every_event_type_is_listed_in_events_py(self):
        emitted = set()
        for path in source_files():
            emitted |= set(re.findall(r'emit\("([a-z_]+)"', open(path, encoding="utf-8").read()))
        self.assertIn("verify", emitted)
        docstring = read("nanocode", "events.py").split('"""')[1]
        self.assertEqual(missing(emitted, docstring), [], "这些事件类型 events.py 的文档里没有")

    def test_every_module_is_in_the_directory_index(self):
        names = [os.path.basename(p) for p in source_files() if not p.endswith(("__init__.py", "__main__.py"))]
        self.assertEqual(missing(names, README), [], "这些源文件 README 的目录结构里没有")

    def test_every_chapter_is_indexed_and_in_the_learning_route(self):
        chapters = sorted(c for c in CHAPTERS if c[0].isdigit() and not c.startswith("00"))
        self.assertEqual(missing(chapters, README), [], "这些章节 README 的章节表里没有")
        route = CHAPTERS["00-学习路线.md"]
        no_route = [c for c in chapters if not re.search(rf"\b{int(c[:2]):02d}\b|\b{int(c[:2])} 章", route)]
        self.assertEqual(no_route, [], "这些章节学习路线里没有提到")

    def test_every_permission_mode_is_explained(self):
        guide = CHAPTERS["04-permissions-and-hooks.md"]
        self.assertEqual(missing(MODES, guide, lambda m: f"`{m}`"), [], "这些权限模式 docs/04 里没有（要写成 `模式名`）")
        self.assertEqual(missing(MODES, README), [], "这些权限模式 README 里没有")

    def test_every_slash_command_is_documented(self):
        ws = tempfile.mkdtemp(prefix="nanocode-docs-")
        s = Session(Options(provider="mock", cwd=ws, mock_delay=0), log=lambda m: None)
        try:
            commands = re.findall(r"^\s+/(\w+)", s.handle_command("/help")[0], re.M)
        finally:
            s.close()
        self.assertIn("memory", commands)
        self.assertEqual(missing(commands, ALL_DOCS, lambda c: f"/{c}"), [], "这些斜杠命令文档里没有")

    def test_every_tool_and_task_parameter_is_in_the_tools_chapter(self):
        tools = default_tools()
        names = [t.name for t in tools]
        self.assertEqual(missing(names, CHAPTERS["03-tools.md"], lambda n: f"`{n}`"), [], "这些工具 docs/03 里没有（要写成 `工具名`）")
        task = next(t for t in tools if t.name == "task")
        chapter = CHAPTERS["06-subagents-todos-skills.md"]
        self.assertEqual(missing(task.input_schema["properties"], chapter, lambda p: f"`{p}`"), [],
                         "task 工具的这些参数 docs/06 里没有")

    def test_trust_sensitive_settings_are_explained(self):
        self.assertEqual(missing(trust.TOP_LEVEL, README + CHAPTERS["04-permissions-and-hooks.md"], lambda k: f'{k}'), [],
                         "这些需要信任才生效的配置项没有说明")

    def test_verify_options_are_documented(self):
        for key in ("max_retries", "timeout"):
            self.assertIn(key, README)

    def test_every_eval_task_is_in_the_task_table(self):
        ids = sorted(os.path.basename(p)[:3] for p in glob.glob(os.path.join(ROOT, "evals", "agent_tasks", "tasks", "T*")))
        self.assertEqual(missing(ids, EVALS_README, lambda i: f"| {i} "), [], "这些评测任务 evals/agent_tasks/README.md 的任务表里没有")

    def test_interactive_tutorial_page_covers_the_features(self):
        """learn.html 是教程页：权限演示要有每一种模式，架构地图要提到验证闭环的每个模块并指向 docs/11。"""
        self.assertEqual(missing(MODES, LEARN, lambda m: f'<option value="{m}"'), [], "learn.html 的权限模式下拉框缺了这些模式")
        for module in ("verify", "subcheck", "review", "risk", "lessons"):
            self.assertIn(module, LEARN, f"learn.html 的架构地图没有提到 {module}.py")
        self.assertIn("docs/11-verification-loop.md", LEARN)


class NumbersMatchTest(unittest.TestCase):
    def test_eval_task_count(self):
        n = len(glob.glob(os.path.join(ROOT, "evals", "agent_tasks", "tasks", "T*")))
        for text in (README, EVALS_README):
            for claim in re.findall(r"(\d+) 个(?:小型)?(?:编程)?任务", text):
                self.assertEqual(int(claim), n, f"文档写了 {claim} 个任务，实际有 {n} 个")
            for claim in re.findall(r"应(?:该)?\s*(\d+)/(\d+)", text):
                self.assertEqual((int(claim[0]), int(claim[1])), (n, n))

    def test_tool_count(self):
        claim = re.search(r"(\d+) 个工具", CHAPTERS["03-tools.md"])
        self.assertIsNotNone(claim, "docs/03 应该写明工具个数")
        self.assertEqual(int(claim.group(1)), len(default_tools()))

    def test_code_size_claim_is_close(self):
        lines = sum(len(open(p, encoding="utf-8").read().splitlines()) for p in source_files())
        claim = int(re.search(r"代码约 (\d+) 行", README).group(1))
        self.assertLess(abs(claim - lines) / lines, 0.1, f"README 写了约 {claim} 行，实际 {lines} 行")

    def test_unit_test_count_claim_is_not_inflated(self):
        claim = int(re.search(r"核心 (\d+)\+ 个", README).group(1))
        actual = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests")).countTestCases()
        self.assertLessEqual(claim, actual, f"README 写了 {claim}+ 个单元测试，实际只有 {actual} 个")
        self.assertGreater(claim, actual * 0.7, f"README 写的 {claim}+ 太保守了（实际 {actual} 个），请更新")


if __name__ == "__main__":
    unittest.main()

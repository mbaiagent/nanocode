"""多用户部署需要的核心改动（docs/12）：平台策略、每一步保存、审计日志、注入的令牌、事件流续传。"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from helpers import Workspace

from nanocode import policy as policy_mod
from nanocode.events import AuditWriter
from nanocode.permissions import PermissionManager
from nanocode.session import Options, Session
from nanocode.web import server as web_server
from nanocode.web.server import make_server


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


class PolicyLoadTest(unittest.TestCase):
    def test_no_policy_configured(self):
        with mock.patch.dict(os.environ, {"NANOCODE_POLICY": ""}):
            self.assertEqual(policy_mod.load_policy(), {})

    def test_configured_but_unreadable_refuses_to_start(self):
        """配了策略却读不出来，当作没有策略就等于关掉了所有限制，所以必须报错。"""
        with self.assertRaises(policy_mod.PolicyError):
            policy_mod.load_policy("/nonexistent/policy.json")
        d = tempfile.mkdtemp()
        bad = os.path.join(d, "p.json")
        with open(bad, "w") as f:
            f.write("{not json")
        with self.assertRaises(policy_mod.PolicyError):
            policy_mod.load_policy(bad)

    def test_mode_must_be_allowed(self):
        p = write_json(os.path.join(tempfile.mkdtemp(), "p.json"), {"mode": "bypass", "allowed_modes": ["default"]})
        with self.assertRaises(policy_mod.PolicyError):
            policy_mod.load_policy(p)


class PolicySessionTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.home = tempfile.mkdtemp()
        self.policy_path = os.path.join(self.home, "policy.json")
        self.env = mock.patch.dict(os.environ, {"NANOCODE_HOME": self.home, "NANOCODE_POLICY": self.policy_path})
        self.env.start()
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()
        self.env.stop()
        self.ws.cleanup()

    def session(self, policy, **opts):
        write_json(self.policy_path, policy)
        s = Session(Options(cwd=self.ws.dir, mock_delay=0, **opts), log=lambda m: None)
        self.sessions.append(s)
        return s

    def test_policy_beats_command_line_and_user_settings(self):
        """沙箱里的 Agent 能改 ~/.nanocode/settings.json，也可能被启动参数影响，策略都要盖过它们。"""
        write_json(os.path.join(self.home, "settings.json"), {"provider": "openai", "base_url": "https://evil.example"})
        s = self.session({"provider": "mock", "model": "mock-locked"}, provider="openai", model="gpt-x")
        self.assertEqual(s.provider_name, "mock")
        self.assertEqual(s.provider.model, "mock-locked")

    def test_forbid_removes_keys_from_user_and_project_config(self):
        write_json(os.path.join(self.home, "settings.json"),
                   {"hooks": {"PreToolUse": [{"command": "touch pwned"}]}, "permissions": {"allow": ["bash(*)"]}})
        s = self.session({"provider": "mock", "forbid": ["hooks", "permissions.allow"],
                          "permissions": {"deny": ["bash(curl *)"]}})
        self.assertNotIn("hooks", s.settings)
        self.assertEqual(s.permissions.allow, [])
        self.assertIn("bash(curl *)", s.permissions.deny)

    def test_allowed_modes_limit_switching(self):
        s = self.session({"provider": "mock", "mode": "review", "allowed_modes": ["review", "default"]})
        self.assertEqual(s.permissions.mode, "review")
        out, _ = s.handle_command("/mode bypass")
        self.assertIn("平台策略不允许", out)
        self.assertEqual(s.permissions.mode, "review")
        out, _ = s.handle_command("/mode default")
        self.assertEqual(s.permissions.mode, "default")

    def test_disallowed_start_mode_falls_back_instead_of_crashing(self):
        write_json(os.path.join(self.home, "settings.json"), {"mode": "bypass"})
        s = self.session({"provider": "mock", "allowed_modes": ["default", "review"]})
        self.assertEqual(s.permissions.mode, "default")

    def test_audit_log(self):
        audit = os.path.join(self.home, "audit", "audit.jsonl")
        s = self.session({"provider": "mock", "mode": "bypass", "audit_log": audit})
        s.send("帮我看看这个项目")
        with open(audit, encoding="utf-8") as f:
            records = [json.loads(line) for line in f]
        types = {r["type"] for r in records}
        self.assertIn("user", types)
        self.assertIn("tool_call", types)
        self.assertIn("turn_end", types)
        self.assertNotIn("text_delta", types)       # 不记录模型输出的流式片段
        self.assertNotIn("llm_response", types)     # 也不记录完整的模型输出
        self.assertTrue(all(r["session"] == s.session_id for r in records))


class PermissionModesTest(unittest.TestCase):
    def test_restricted_manager(self):
        pm = PermissionManager(tempfile.mkdtemp(), mode="default", allowed_modes=("default", "plan"))
        pm.set_mode("plan")
        with self.assertRaises(ValueError):
            pm.set_mode("bypass")
        with self.assertRaises(ValueError):
            PermissionManager(tempfile.mkdtemp(), mode="bypass", allowed_modes=("default",))

    def test_unrestricted_by_default(self):
        pm = PermissionManager(tempfile.mkdtemp())
        pm.set_mode("bypass")
        self.assertEqual(pm.mode, "bypass")


class AuditWriterTest(unittest.TestCase):
    def test_long_fields_are_truncated(self):
        path = os.path.join(tempfile.mkdtemp(), "a.jsonl")
        w = AuditWriter(path, session=lambda: "s1")
        w({"type": "tool_result", "seq": 1, "content": "x" * 10000})
        w({"type": "llm_response", "seq": 2, "content": "not audited"})
        w.close()
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertLess(len(rec["content"]), 2100)
        self.assertIn("截断", rec["content"])


class AutosaveTest(unittest.TestCase):
    def test_saves_before_every_main_model_call(self):
        """一个多步任务中途被杀，最多丢一步：每次主 Agent 调用模型前都保存了。"""
        ws = Workspace()
        s = Session(Options(provider="mock", cwd=ws.dir, mock_delay=0, mode="bypass"), log=lambda m: None)
        try:
            saved_lengths, requests = [], []
            real_save = s.save
            def counting_save():
                saved_lengths.append(len(s.agent.messages))
                real_save()
            s.save = counting_save
            s.bus.subscribe(lambda e: e["type"] == "llm_request" and e["agent"] == "main" and requests.append(e))
            s.send("帮我看看这个项目")
            self.assertGreater(len(requests), 1)                       # mock 模型会走好几步
            self.assertGreaterEqual(len(saved_lengths), len(requests))  # 每一步都存了，最后整轮结束再存一次
            self.assertEqual(saved_lengths[:len(requests)], sorted(saved_lengths[:len(requests)]))   # 越存越多
            # 每次保存时最后一条都不是"有 tool_use 却没有结果"的半截状态
            with open(os.path.join(s.data_dir, "sessions", f"{s.session_id}.json"), encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["messages"]), len(s.agent.messages))
        finally:
            s.close()
            ws.cleanup()


class InjectedTokenTest(unittest.TestCase):
    def test_token_from_environment(self):
        with mock.patch.dict(os.environ, {"NANOCODE_WEB_TOKEN": "t" * 32}):
            self.assertEqual(web_server.web_token(), "t" * 32)

    def test_short_token_rejected(self):
        with mock.patch.dict(os.environ, {"NANOCODE_WEB_TOKEN": "short"}):
            with self.assertRaises(ValueError):
                web_server.web_token()

    def test_random_when_unset(self):
        with mock.patch.dict(os.environ, {"NANOCODE_WEB_TOKEN": ""}):
            a, b = web_server.web_token(), web_server.web_token()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 16)


class EventResumeTest(unittest.TestCase):
    """事件流带 id，重连时只补发浏览器（或控制面）还没收到的事件。"""

    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace()
        cls.session = Session(Options(provider="mock", cwd=cls.ws.dir, mock_delay=0), log=lambda m: None)
        cls.server, cls.app = make_server(cls.session, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        for i in range(5):
            cls.session.bus.emit("command", input=f"/x{i}", output="")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.session.close()
        cls.ws.cleanup()

    def read_events(self, path, headers, n):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token, **headers})
        r = conn.getresponse()
        ids, events = [], []
        while len(events) < n:
            line = r.fp.readline().decode().rstrip("\n")
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            elif line.startswith("data: "):
                events.append(json.loads(line[6:]))
        conn.close()
        return ids, events

    def test_events_carry_ids(self):
        ids, events = self.read_events("/api/events", {}, 5)
        self.assertEqual(ids, [e["seq"] for e in events])

    def test_last_event_id_skips_what_was_already_received(self):
        seqs = [e["seq"] for e in self.session.bus.history]
        ids, _ = self.read_events("/api/events", {"Last-Event-ID": str(seqs[2])}, 2)
        self.assertEqual(ids, seqs[3:5])

    def test_after_query_parameter(self):
        seqs = [e["seq"] for e in self.session.bus.history]
        ids, _ = self.read_events(f"/api/events?after={seqs[3]}", {}, 1)
        self.assertEqual(ids, [seqs[4]])


if __name__ == "__main__":
    unittest.main()

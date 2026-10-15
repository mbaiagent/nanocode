"""平台端到端：真的启动控制面、模型网关和 nanocode 沙箱（本机启动器），以多个用户的身份访问。"""

import json
import os
import sys
import time
import unittest
from unittest import mock

import support  # noqa: F401

from nanocode_server.config import Config
from nanocode_server.demo import FakeModel, check_isolation, simulate
from nanocode_server.platform import Platform
from support import Client, make_config

DEMO_MCP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "examples", "mcp_server_demo.py")


class PlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModel()
        cls.cfg = make_config(allow_private_upstreams=True, max_sessions_per_user=2, max_sandboxes=20,
                              sandbox_mcp_servers={"demo": {"command": sys.executable, "args": [os.path.abspath(DEMO_MCP)]}})
        cls.platform = Platform(cls.cfg, log=lambda m: None)
        cls.platform.start()
        cls.port = cls.platform.control_port

    @classmethod
    def tearDownClass(cls):
        cls.platform.close()
        cls.model.close()

    def client(self, user):
        return Client(self.port, user)

    def create(self, c, title="t", model=""):
        status, data, _ = c.request("POST", "/api/sessions", {"title": title, "model": model})
        self.assertEqual(status, 201, data)
        sid = data["session"]["id"]
        self.addCleanup(c.request, "POST", f"/api/sessions/{sid}/delete", {})
        return sid

    def add_key(self, c, key="sk-test-123456"):
        status, data, _ = c.request("POST", "/api/credentials", {"provider": "custom", "base_url": self.model.url,
                                                                 "api_key": key, "models": "fake-model"})
        self.assertEqual(status, 201, data)
        return f"c:{data['credential']['id']}:fake-model"

    # ------------------------------------------------------------------ 登录

    def test_login_required(self):
        anon = Client(self.port)
        status, _, headers = anon.request("GET", "/")
        self.assertEqual((status, headers.get("location")), (302, "/login"))
        self.assertEqual(anon.request("GET", "/api/sessions")[0], 401)
        self.assertEqual(anon.request("GET", "/s/0123456789abcdef/api/state")[0], 401)
        self.assertEqual(anon.request("GET", "/healthz")[0], 200)
        self.assertEqual(anon.request("GET", "/login")[0], 200)

    def test_login_logout(self):
        c = self.client("Erin")
        status, me, _ = c.request("GET", "/api/me")
        self.assertEqual((status, me["user"], me["auth"]), (200, "erin", "dev"))
        self.assertEqual(me["platform"]["launcher"], "local")
        c.request("POST", "/api/logout", {})
        c.cookie = ""
        self.assertEqual(c.request("GET", "/api/me")[0], 401)
        self.assertEqual(Client(self.port).request("POST", "/api/login", {"user": "../etc"})[0], 400)

    def test_dns_rebinding_and_cross_site_rejected(self):
        c = self.client("frank")
        self.assertEqual(c.request("GET", "/api/me", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(c.request("POST", "/api/sessions", {"title": "x"}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(c.request("POST", f"/api/sessions/{'0' * 16}/stop", None,
                                   headers={"content-type": "text/plain"})[0], 415)

    # ------------------------------------------------------------------ 会话

    def test_full_flow_through_proxy(self):
        c = self.client("grace")
        sid = self.create(c)
        status, page, _ = c.request("GET", f"/s/{sid}/?embed=1")
        self.assertEqual(status, 200)
        self.assertIn('"api/events?ticket="', page)
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[1]["provider"], "mock")
        _, out, _ = c.request("POST", f"/s/{sid}/api/chat", {"message": "/mode bypass"})
        self.assertIn("平台策略不允许", out["output"])          # 平台策略在沙箱里生效
        c.request("POST", f"/s/{sid}/api/chat", {"message": "看看这个项目"})
        events = c.events_until(sid, "turn_end")
        self.assertTrue(any(e["type"] == "permission_answer" and e["decision"] == "yes" for e in events))   # 审批经控制面转发
        last = events[-1]["seq"]
        c.request("POST", f"/s/{sid}/api/chat", {"message": "/help"})
        self.assertTrue(all(e["seq"] > last for e in c.events_until(sid, "command", last_event_id=last)))
        ext = c.request("GET", f"/s/{sid}/api/extensions")[1]         # 管理员配的 MCP 服务器在沙箱里连上了
        self.assertEqual([(m["name"], m["ok"]) for m in ext["mcp"]], [("demo", True)])
        listed = c.request("GET", "/api/sessions")[1]["sessions"][0]
        self.assertNotIn("endpoint", listed)
        self.assertNotIn("web_token", listed)

    def test_switch_model_keeps_conversation_and_routes_through_gateway(self):
        c = self.client("heidi")
        sid = self.create(c)
        c.request("POST", f"/s/{sid}/api/chat", {"message": "先用演示模型"})
        c.events_until(sid, "turn_end")
        before = len(c.request("GET", f"/s/{sid}/api/context")[1]["messages"])
        ref = self.add_key(c, key="sk-heidi-999999")
        status, data, _ = c.request("POST", f"/api/sessions/{sid}/model", {"model": ref})
        self.assertEqual(status, 200, data)
        state = c.request("GET", f"/s/{sid}/api/state")[1]
        self.assertEqual((state["provider"], state["model"]), ("custom", "fake-model"))
        self.assertEqual(len(c.request("GET", f"/s/{sid}/api/context")[1]["messages"]), before)   # 对话还在
        calls = len(self.model.calls)
        c.request("POST", f"/s/{sid}/api/chat", {"message": "暗号：x1"})
        c.events_until(sid, "turn_end")
        self.assertEqual({k["key"] for k in self.model.calls[calls:]}, {"sk-heidi-999999"})    # 经网关、用自己的 Key
        self.assertGreater(c.request("GET", "/api/me")[1]["usage"]["requests"], 0)

    def test_cannot_use_someone_elses_model(self):
        ivan, judy = self.client("ivan"), self.client("judy")
        ref = self.add_key(ivan)
        self.assertEqual(judy.request("POST", "/api/sessions", {"model": ref})[0], 400)
        sid = self.create(judy)
        self.assertEqual(judy.request("POST", f"/api/sessions/{sid}/model", {"model": ref})[0], 400)
        self.assertEqual(judy.request("POST", f"/api/credentials/{ref.split(':')[1]}/delete", {})[0], 404)

    def test_credentials_api_and_probe(self):
        c = self.client("ken")
        status, data, _ = c.request("POST", "/api/credentials/test", {"provider": "custom", "base_url": self.model.url,
                                                                      "api_key": "sk-x-123456", "models": "fake-model"})
        self.assertEqual((status, data["ok"]), (200, True))       # 经由网关测试
        ref = self.add_key(c)
        creds = c.request("GET", "/api/credentials")[1]["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertNotIn("secret", creds[0])
        self.assertNotIn("sk-test", str(creds))
        self.assertIn(ref, [m["ref"] for m in c.request("GET", "/api/models")[1]["models"]])
        self.assertEqual(c.request("POST", f"/api/credentials/{creds[0]['id']}/delete", {})[0], 200)
        self.assertNotIn(ref, [m["ref"] for m in c.request("GET", "/api/models")[1]["models"]])

    def test_stop_then_reopen_resumes(self):
        c = self.client("leo")
        sid = self.create(c)
        c.request("POST", f"/s/{sid}/api/chat", {"message": "记住这句话"})
        c.events_until(sid, "turn_end")
        before = len(c.request("GET", f"/s/{sid}/api/context")[1]["messages"])
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/stop", {})[0], 200)
        self.assertFalse(self.platform.launcher.is_alive(sid))
        status, ctx, _ = c.request("GET", f"/s/{sid}/api/context")      # 自动重启并恢复对话
        self.assertEqual((status, len(ctx["messages"])), (200, before))

    def test_dead_sandbox_restarts(self):
        c = self.client("mallory")
        sid = self.create(c)
        self.platform.launcher.stop(sid)                 # 绕过控制面直接杀掉
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[0], 502)
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[0], 200)

    def test_limits_and_reaping(self):
        c = self.client("nina")
        a, b = self.create(c), self.create(c)
        self.assertEqual(c.request("POST", "/api/sessions", {"title": "third"})[0], 429)
        sessions = self.platform.sessions
        self.platform.store.update_session(a, last_active=time.time() - 7200)
        self.assertEqual(sessions.reap_once(), [a])
        self.assertFalse(self.platform.launcher.is_alive(a))
        self.assertTrue(self.platform.launcher.is_alive(b))
        self.platform.store.update_session(b, last_active=time.time() - 7200)
        sessions.sandbox_state = lambda s: {"busy": True}           # Agent 正在工作，不回收
        try:
            self.assertEqual(sessions.reap_once(), [])
        finally:
            del sessions.sandbox_state

    # ------------------------------------------------------------------ 多用户

    def test_many_users_concurrently_are_isolated(self):
        users = [f"crowd{i}" for i in range(5)]
        self.cfg.sandbox_mode, mode = "accept_edits", self.cfg.sandbox_mode    # 让 Agent 直接写文件
        since = len(self.model.calls)
        try:
            results = simulate(self.platform, self.model, users)
        finally:
            self.cfg.sandbox_mode = mode
        try:
            self.assertEqual(sorted(results), users)
            failed = [(name, note) for name, ok, note in check_isolation(self.platform, self.model, results, since) if not ok]
            self.assertEqual(failed, [])
        finally:
            for r in results.values():
                r["browser"].request("POST", f"/api/sessions/{r['session']}/delete", {})


class SandboxMcpConfigTest(unittest.TestCase):
    def test_from_env_inline_or_file(self):
        servers = {"demo": {"command": "python3", "args": ["x.py"]}}
        with mock.patch.dict(os.environ, {"NCS_SANDBOX_MCP_SERVERS": json.dumps(servers)}):
            self.assertEqual(Config.from_env().sandbox_mcp_servers, servers)
        path = os.path.join(make_config().data_dir, "mcp.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(servers, f)
        with mock.patch.dict(os.environ, {"NCS_SANDBOX_MCP_SERVERS": path}):
            self.assertEqual(Config.from_env().sandbox_mcp_servers, servers)
        with mock.patch.dict(os.environ, {"NCS_SANDBOX_MCP_SERVERS": "{bad"}):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_validate_shape(self):
        with self.assertRaises(ValueError):
            make_config(sandbox_mcp_servers={"x": {"args": []}}).validate()
        make_config(sandbox_mcp_servers={"x": {"command": "python3"}}).validate()


class ProxyAuthTest(unittest.TestCase):
    """生产模式：身份来自认证代理，必须带共享密钥。"""

    def test_proxy_auth(self):
        cfg = make_config(auth="proxy", proxy_secret="p" * 24, host="127.0.0.1")
        platform = Platform(cfg, log=lambda m: None)
        platform.start()
        try:
            port = platform.control_port
            ok = Client(port, "alice@example.com", proxy_secret="p" * 24)
            self.assertEqual(ok.request("GET", "/api/me")[1]["user"], "alice@example.com")
            forged = Client(port, "alice@example.com", proxy_secret="wrong")
            self.assertEqual(forged.request("GET", "/api/me")[0], 401)
            self.assertEqual(ok.request("POST", "/api/login", {"user": "x"})[0], 404)     # 没有开发登录
            self.assertEqual(ok.request("GET", "/login")[0], 302)
        finally:
            platform.close()


if __name__ == "__main__":
    unittest.main()

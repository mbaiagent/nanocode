"""控制面端到端：用本机启动器真的启动 nanocode 沙箱（mock 模型），经控制面以两个用户的身份访问。"""

import http.client
import json
import threading
import time
import unittest

import support  # noqa: F401

from nanocode_server.app import make_control_server
from nanocode_server.launcher import LocalLauncher
from support import make_config

PROXY_SECRET = "p" * 24


class ControlPlaneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_config(proxy_secret=PROXY_SECRET, sandbox_provider="mock", max_sessions_per_user=2,
                              max_sandboxes=3, idle_timeout=3600)
        cls.launcher = LocalLauncher(cls.cfg)
        cls.server, cls.cp = make_control_server(cls.cfg, cls.launcher)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.launcher.stop_all()

    # ------------------------------------------------------------------ 工具

    def req(self, method, path, user="alice@example.com", body=None, headers=None, raw=False):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if user:
            h.update({"X-Auth-Request-Email": user, "X-NCS-Proxy-Secret": PROXY_SECRET})
        if body is not None:
            h["content-type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        if raw:
            return r.status, data, dict(r.getheaders())
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data.decode("utf-8", "replace")

    def create(self, user="alice@example.com", title="t"):
        status, data = self.req("POST", "/api/sessions", user=user, body={"title": title})
        self.assertEqual(status, 201, data)
        sid = data["session"]["id"]
        self.addCleanup(self.req, "POST", f"/api/sessions/{sid}/delete", user=user, body={})
        return sid

    def events_until(self, sid, user, stop_type, timeout=30, last_event_id=None, approve=True):
        """读控制面转发的事件流直到出现 stop_type。遇到权限确认就经控制面提交"允许"（审批也要能转发）。"""
        h = {"Host": f"127.0.0.1:{self.port}", "X-Auth-Request-Email": user, "X-NCS-Proxy-Secret": PROXY_SECRET}
        if last_event_id is not None:
            h["Last-Event-ID"] = str(last_event_id)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/s/{sid}/api/events", headers=h)
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        events, deadline = [], time.time() + timeout
        while time.time() < deadline:
            line = r.fp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                e = json.loads(line[6:])
                events.append(e)
                if e["type"] == "permission_request" and approve:
                    status, out = self.req("POST", f"/s/{sid}/api/permission", user=user,
                                           body={"id": e["id"], "decision": "yes"})
                    self.assertEqual(status, 200, out)
                if e["type"] == stop_type:
                    break
        conn.close()
        return events

    # ------------------------------------------------------------------ 测试

    def test_requires_login(self):
        self.assertEqual(self.req("GET", "/api/sessions", user=None)[0], 401)
        self.assertEqual(self.req("GET", "/api/sessions", user=None,
                                  headers={"X-Auth-Request-Email": "alice@example.com"})[0], 401)   # 没有代理密钥
        self.assertEqual(self.req("GET", "/healthz", user=None)[0], 200)

    def test_full_flow_through_the_proxy(self):
        sid = self.create(title="修 bug")
        status, page = self.req("GET", f"/s/{sid}/")
        self.assertEqual(status, 200)
        self.assertIn("nanocode", page)
        self.assertIn('"api/events?ticket="', page)          # 页面用相对路径，挂在 /s/<id>/ 下也能用
        status, state = self.req("GET", f"/s/{sid}/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["provider"], "mock")
        # 平台策略在沙箱里生效：本机启动器不允许 bypass
        status, out = self.req("POST", f"/s/{sid}/api/chat", body={"message": "/mode bypass"})
        self.assertIn("平台策略不允许", out.get("output", ""))
        # 发一条消息，经控制面转发的事件流里能看到整轮结束
        status, out = self.req("POST", f"/s/{sid}/api/chat", body={"message": "看看这个项目"})
        self.assertTrue(out["ok"], out)
        events = self.events_until(sid, "alice@example.com", "turn_end")
        self.assertEqual(events[-1]["type"], "turn_end")
        answers = [e for e in events if e["type"] == "permission_answer"]
        self.assertTrue(answers and all(a["decision"] == "yes" for a in answers))   # 审批经控制面转发到了沙箱
        # 断线重连：带上最后收到的序号，只补发之后的
        last = events[-1]["seq"]
        self.req("POST", f"/s/{sid}/api/chat", body={"message": "/help"})
        resumed = self.events_until(sid, "alice@example.com", "command", last_event_id=last)
        self.assertTrue(all(e["seq"] > last for e in resumed))
        # 列表里不暴露沙箱地址和令牌
        status, data = self.req("GET", "/api/sessions")
        listed = [s for s in data["sessions"] if s["id"] == sid][0]
        self.assertNotIn("endpoint", listed)
        self.assertNotIn("web_token", listed)

    def test_users_cannot_see_each_others_sessions(self):
        sid = self.create(user="alice@example.com")
        for path in (f"/s/{sid}/", f"/s/{sid}/api/state", f"/s/{sid}/api/events"):
            self.assertEqual(self.req("GET", path, user="bob@example.com")[0], 404)
        self.assertEqual(self.req("POST", f"/api/sessions/{sid}/delete", user="bob@example.com", body={})[0], 404)
        status, data = self.req("GET", "/api/sessions", user="bob@example.com")
        self.assertNotIn(sid, [s["id"] for s in data["sessions"]])

    def test_cross_site_post_rejected(self):
        status, _ = self.req("POST", "/api/sessions", body={"title": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_sandbox_that_died_is_restarted_on_next_access(self):
        """沙箱进程意外退出（节点故障、OOM）：控制面转发失败时标记为已停止，下次访问自动重启。"""
        sid = self.create()
        self.launcher.stop(sid)                     # 绕过控制面直接杀掉，数据库里仍是 running
        status, _ = self.req("GET", f"/s/{sid}/api/state")
        self.assertEqual(status, 502)
        self.assertEqual(self.cp.store.get(sid)["status"], "stopped")
        status, _ = self.req("GET", f"/s/{sid}/api/state")
        self.assertEqual(status, 200)

    def test_stop_and_delete_require_json(self):
        sid = self.create()
        status, _ = self.req("POST", f"/api/sessions/{sid}/stop", headers={"content-type": "text/plain"})
        self.assertEqual(status, 415)

    def test_stop_then_reopen_resumes_the_conversation(self):
        sid = self.create()
        self.req("POST", f"/s/{sid}/api/chat", body={"message": "记住这句话"})
        self.events_until(sid, "alice@example.com", "turn_end")
        _, before = self.req("GET", f"/s/{sid}/api/context")
        self.assertGreater(len(before["messages"]), 0)
        self.assertEqual(self.req("POST", f"/api/sessions/{sid}/stop", body={})[0], 200)
        self.assertFalse(self.launcher.is_alive(sid))
        # 再次访问：控制面自动重新启动沙箱，并恢复上次的对话（工作区和会话记录都在）
        status, after = self.req("GET", f"/s/{sid}/api/context")
        self.assertEqual(status, 200)
        self.assertEqual(len(after["messages"]), len(before["messages"]))

    def test_per_user_limit_and_idle_reaping(self):
        a = self.create(user="carol@example.com")
        b = self.create(user="carol@example.com")
        status, data = self.req("POST", "/api/sessions", user="carol@example.com", body={"title": "third"})
        self.assertEqual(status, 429, data)                   # 每人最多 2 个
        # 空闲回收：把最近活动时间改到很久以前
        self.cp.store.update(a, last_active=time.time() - 7200)
        self.assertEqual(self.cp.reap_once(), [a])
        self.assertFalse(self.launcher.is_alive(a))
        self.assertTrue(self.launcher.is_alive(b))

    def test_busy_sandbox_is_not_reaped(self):
        sid = self.create(user="dave@example.com")
        s = self.cp.store.get(sid)
        self.cp.store.update(sid, last_active=time.time() - 7200)
        self.cp.sandbox_state = lambda s: {"busy": True}      # 模拟 Agent 正在工作
        try:
            self.assertEqual(self.cp.reap_once(), [])
        finally:
            del self.cp.sandbox_state
        self.assertTrue(self.launcher.is_alive(s["id"]))



class DevModeTest(unittest.TestCase):
    """开发模式：本机请求都当成开发者，所以必须挡住 DNS 重绑定（Host 不是本机地址的请求）。"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = make_config(dev_user="dev@localhost", sandbox_provider="mock")
        cls.server, cls.cp = make_control_server(cls.cfg, LocalLauncher(cls.cfg))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, host):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/sessions", headers={"Host": host})
        status = conn.getresponse().status
        conn.close()
        return status

    def test_rebinding_host_rejected(self):
        self.assertEqual(self.get("evil.example:8700"), 403)
        self.assertEqual(self.get(f"127.0.0.1:{self.port}"), 200)
        self.assertEqual(self.get(f"localhost:{self.port}"), 200)


if __name__ == "__main__":
    unittest.main()

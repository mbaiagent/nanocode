"""Web 面板的 busy 锁与权限询问：对应 issue #13（权限询问阻塞全局）和 #4（刷新后忙碌状态不同步）。"""

import http.client
import json
import os
import threading
import time
import unittest

from helpers import Workspace

from nanocode.events import EventBus
from nanocode.session import Options, Session
from nanocode.web.server import WebApp, make_server

STATIC = os.path.join(os.path.dirname(__file__), "..", "nanocode", "web", "static")


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class WebBusyTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        # mock 模型第一步会写文件，default 模式下要走权限询问
        self.session = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.conns = []

    def tearDown(self):
        self.session.agent.cancel.set()
        for c in self.conns:
            c.sse_response.close()
            c.close()
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        self.ws.cleanup()

    def headers(self):
        return {"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token,
                "content-type": "application/json"}

    def post(self, path, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, body=json.dumps(body), headers=self.headers())
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        return data

    def open_sse(self):
        """模拟一个开着的浏览器页面：连上事件流并保持不断开。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.port}",
                                                 "x-nanocode-token": self.app.token})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        # 响应对象必须留着：这是流式响应，连接的生命周期跟着它走，丢掉就等于断开
        c.sse_response = r
        self.conns.append(c)
        self.assertTrue(wait_for(lambda: self.app.sse_clients >= 1))
        return c

    def start_task_and_wait_for_prompt(self):
        self.assertTrue(self.post("/api/chat", {"message": "帮我写个文件"})["ok"])
        self.assertTrue(wait_for(lambda: len(self.app.pending) == 1), "权限询问没有出现")
        self.assertTrue(self.session.agent.busy)

    def test_permission_wait_is_released_when_no_browser_connected(self):
        """#13：用户触发权限询问后关掉页面，不能让全局 busy 锁一直占着。"""
        self.app.disconnect_grace = 0.5
        self.start_task_and_wait_for_prompt()
        self.assertEqual(self.app.sse_clients, 0)
        self.assertTrue(wait_for(lambda: not self.app.busy, timeout=5), "没有浏览器连着，busy 却一直不释放")
        # 释放后别的请求可以进来
        self.assertTrue(self.post("/api/chat", {"message": "/help"})["ok"])

    def test_permission_wait_kept_while_a_browser_is_connected(self):
        """页面开着的时候不能误判：用户可能只是还没来得及点。"""
        self.app.disconnect_grace = 0.5
        self.open_sse()
        self.start_task_and_wait_for_prompt()
        time.sleep(1.5)  # 超过宽限期
        self.assertTrue(self.session.agent.busy)
        self.assertEqual(len(self.app.pending), 1)

    def test_closing_the_page_mid_prompt_releases_busy(self):
        """#13 的真实场景：页面开着触发了权限询问，然后关掉浏览器。服务端靠心跳发现断开。"""
        self.app.disconnect_grace = 0.5
        self.app.sse_ping_interval = 0.2
        sse = self.open_sse()
        self.start_task_and_wait_for_prompt()
        sse.sse_response.close()
        sse.close()
        self.conns.remove(sse)
        self.assertTrue(wait_for(lambda: not self.app.busy, timeout=6), "页面关了，busy 却一直不释放")

    def test_reconnect_within_grace_does_not_deny(self):
        """刷新页面（断开后很快重连）不能被当成"页面关了"：计数短暂归零但没超过宽限期。"""
        self.app.disconnect_grace = 1.5
        self.open_sse()
        self.start_task_and_wait_for_prompt()
        with self.app._sse_lock:
            self.app.sse_clients -= 1     # 模拟旧连接断开
        time.sleep(0.4)
        with self.app._sse_lock:
            self.app.sse_clients += 1     # 新页面重连
        time.sleep(1.8)                   # 越过原来的宽限期
        self.assertTrue(self.session.agent.busy)
        self.assertEqual(len(self.app.pending), 1)

    def test_cancel_still_releases_immediately(self):
        self.open_sse()
        self.start_task_and_wait_for_prompt()
        self.post("/api/cancel", {})
        self.assertTrue(wait_for(lambda: not self.app.busy, timeout=3))

    def test_second_request_is_queued_while_busy(self):
        """忙时不再拒绝（#5 缓解）：后来的请求收下排队，当前任务做完自动执行。"""
        self.open_sse()
        self.start_task_and_wait_for_prompt()
        r = self.post("/api/chat", {"message": "你好"})
        self.assertTrue(r["ok"])
        self.assertTrue(r["queued"])
        self.assertEqual(r["position"], 1)
        self.assertEqual(len(self.app.queue), 1)

    def test_permission_timeout_from_env(self):
        old = os.environ.get("NANOCODE_PERMISSION_TIMEOUT")
        os.environ["NANOCODE_PERMISSION_TIMEOUT"] = "42"
        try:
            self.assertEqual(WebApp(self.session).permission_timeout, 42.0)
            self.assertEqual(WebApp(self.session, permission_timeout=7).permission_timeout, 7)
        finally:
            if old is None:
                os.environ.pop("NANOCODE_PERMISSION_TIMEOUT", None)
            else:
                os.environ["NANOCODE_PERMISSION_TIMEOUT"] = old
        # WebApp 构造时会把自己挂到 permissions.ask_fn 上，恢复成当前服务器的那个
        self.session.permissions.ask_fn = self.app.ask


class RefreshBusyTest(unittest.TestCase):
    """#4：事件历史有上限，长任务的 user 事件会被挤掉，光重放事件会误判成空闲。"""

    def test_history_can_evict_the_running_turn_user_event(self):
        bus = EventBus()
        bus.emit("user", agent="main", text="长任务")
        for i in range(bus.history.maxlen + 100):
            bus.emit("tool_result", agent="main", id=str(i), name="read_file", content="x", is_error=False)
        self.assertFalse(any(e["type"] == "user" for e in bus.history))

    def test_page_load_syncs_busy_from_server_state(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        # 页面加载时必须以服务端 busy 为准；turn_end / command 触发的刷新不能（见 index.html 里的注释）
        self.assertIn("async function refreshState(sync = false)", html)
        self.assertIn("if (sync) { setBusy(!!s.busy);", html)
        self.assertIn("refreshState(true); refreshContext(); refreshSessions();", html)
        self.assertEqual(html.count("refreshState()"), 2)  # command、turn_end 两处仍是非同步刷新


if __name__ == "__main__":
    unittest.main()

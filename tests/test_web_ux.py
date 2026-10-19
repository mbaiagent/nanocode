"""Web 交互优化：排队、报错人话化、会话列表。"""

import http.client
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from helpers import Workspace

from nanocode.llm.base import LLMError, QuotaExhausted
from nanocode.messages import text_block
from nanocode.session import Options, Session
from nanocode.web.server import WebApp, friendly_error, make_server

STATIC = os.path.join(os.path.dirname(__file__), "..", "nanocode", "web", "static")


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class WebUxTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.session = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.session.agent.cancel.set()
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        self.ws.cleanup()

    def headers(self):
        return {"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token,
                "content-type": "application/json"}

    def test_server_bind_does_not_reverse_resolve_loopback(self):
        # A blocked reverse lookup can leave a sandbox process alive before its socket listens (#92).
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("unexpected reverse lookup")):
            server, _ = make_server(self.session, "127.0.0.1", 0)
        try:
            self.assertEqual(server.server_port, server.server_address[1])
        finally:
            server.server_close()

    def api(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers=self.headers())
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        return data

    def post(self, path, body):
        return self.api("POST", path, body)

    def get(self, path):
        return self.api("GET", path)

    # --- 排队 ---

    def test_busy_request_is_queued_not_rejected(self):
        self.session.agent.busy = True  # 模拟正在工作
        try:
            r = self.post("/api/chat", {"message": "第二条"})
            self.assertTrue(r["ok"])
            self.assertTrue(r["queued"])
            self.assertEqual(r["position"], 1)
            r = self.post("/api/chat", {"message": "第三条"})
            self.assertEqual(r["position"], 2)
            self.assertEqual(len(self.app.queue), 2)
        finally:
            self.session.agent.busy = False
            self.app.queue.clear()

    def test_mode_can_be_switched_while_busy(self):
        """#78：/mode 只改一个配置项，忙时必须能切。

        以前它和 /clear 一起被当成"会动 Agent 脚下状态"的命令 429 拒掉，而前端不显示
        这个错误 —— 下拉框已经变了、服务端没变，用户以为切换成功了。
        """
        self.session.agent.busy = True
        try:
            r = self.post("/api/chat", {"message": "/mode review"})
            self.assertTrue(r.get("ok"), r)
            self.assertEqual(self.session.permissions.mode, "review")
            # 要说清楚什么时候生效：正在跑的这一轮已经判过的工具不会回退重判
            self.assertIn("下一次工具调用", r.get("output", ""))
        finally:
            self.session.permissions.set_mode("default")
            self.session.agent.busy = False

    def test_clear_is_still_rejected_while_busy(self):
        """对照组（#50）：会动 Agent 脚下状态的命令忙时仍然要挡住。"""
        self.session.agent.busy = True
        try:
            r = self.post("/api/chat", {"message": "/clear"})
            self.assertFalse(r.get("ok"), r)
            self.assertEqual(r.get("code"), "busy")
        finally:
            self.session.agent.busy = False

    def test_mode_switch_failure_is_shown_in_the_ui(self):
        """#78：后端拒绝时前端必须显示出来，并把下拉框拨回服务端的真实状态。"""
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('$("#mode").onchange', html)
        self.assertIn("切换权限模式失败", html)
        self.assertNotIn('$("#mode").onchange = () => post(', html)   # 旧写法：丢弃返回值
        self.assertIn('$("#mode").value = serverMode = s.mode;', html)
        self.assertIn('$("#mode").value = serverMode;', html)

    def test_compact_does_not_start_a_turn(self):
        """#73：/compact 自己已经调过一次模型做总结，不能再把 None 当 prompt 起一轮。

        以前这里会把 prompt=None 传给 _run，往对话里塞一条 text=None 的消息
        （真实接口直接 400），还多扣一次每日预算。
        """
        self.session.agent.messages = [
            {"role": "user", "content": [text_block("第一轮")]},
            {"role": "assistant", "content": [text_block("第一轮回答")]},
            {"role": "user", "content": [text_block("第二轮")]},
            {"role": "assistant", "content": [text_block("第二轮回答")]},
        ]
        calls = []
        self.app._run = lambda p: calls.append(p)
        r = self.post("/api/chat", {"message": "/compact"})
        self.assertTrue(r["ok"])
        self.assertEqual(calls, [], "把 None 当 prompt 起了一轮")
        texts = [b.get("text") for m in self.session.agent.messages
                 for b in m["content"] if b.get("type") == "text"]
        self.assertNotIn(None, texts, "对话里留下了 text=None 的消息")

    def test_handoff_window_queues_instead_of_starting_a_second_agent(self):
        """#29：Agent.run() 一返回就把 agent.busy 清掉了，而 _run 还没处理完队列。

        这段窗口里如果只看 agent.busy，新请求会被当成"空闲"，绕开排队和每日预算，
        并发起第二个 Agent。这里把窗口固定下来（agent 不忙、但这一轮还没收尾），
        请求必须进队列。
        """
        self.app._turn_active = True          # 这一轮工作还没结束
        self.session.agent.busy = False       # 但 Agent 循环已经跑完了
        try:
            r = self.post("/api/chat", {"message": "补一句"})
            self.assertTrue(r["ok"])
            self.assertTrue(r["queued"], "窗口内的请求被当成新的一轮，会并发第二个 Agent")
            self.assertEqual(self.app.queue, ["补一句"])
        finally:
            self.app._turn_active = False
            self.app.queue.clear()

    def test_state_reports_busy_until_the_turn_finishes(self):
        """窗口期 agent.busy 已经是 False，/api/state 仍要报 busy，否则界面会闪一下空闲。"""
        self.app._turn_active = True
        self.session.agent.busy = False
        try:
            self.assertTrue(self.app.busy)
            self.assertTrue(self.get("/api/state")["busy"])
        finally:
            self.app._turn_active = False
        self.assertFalse(self.get("/api/state")["busy"])

    def test_state_never_gets_stuck_busy_after_a_turn(self):
        """空闲判定不能只加不减：一轮跑完后必须回到不忙。"""
        self.session.permissions.mode = "bypass"
        self.assertTrue(self.post("/api/chat", {"message": "hello"})["ok"])
        self.assertTrue(wait_for(lambda: not self.app.busy, timeout=10), "回合结束后一直显示忙")
        self.assertFalse(self.get("/api/state")["busy"])

    def test_queue_is_drained_in_order_after_task(self):
        """排队消息在当前任务结束后按顺序执行。"""
        self.session.permissions.mode = "bypass"  # mock 写文件不走权限询问
        done = []
        orig_send = self.session.send
        def spy(prompt):
            done.append(prompt)
            return orig_send(prompt)
        self.session.send = spy
        try:
            self.assertTrue(self.post("/api/chat", {"message": "第一条"})["ok"])
            self.assertTrue(wait_for(lambda: self.session.agent.busy, timeout=3))
            r = self.post("/api/chat", {"message": "第二条"})
            self.assertTrue(r.get("queued"))
            self.assertTrue(wait_for(lambda: not self.app.busy, timeout=15),
                            f"队列没有执行完：{done}")
            self.assertEqual(done, ["第一条", "第二条"])
        finally:
            self.session.send = orig_send

    # --- 报错人话化 ---

    def test_friendly_error_quota(self):
        msg = friendly_error(QuotaExhausted("HTTP 429：配额已用完，不再重试。", status=429))
        self.assertIn("配额用完了", msg)

    def test_friendly_error_503(self):
        msg = friendly_error(LLMError("HTTP 503: overloaded", status=503))
        self.assertIn("太忙了", msg)

    def test_friendly_error_429_rate_limit(self):
        msg = friendly_error(LLMError("HTTP 429: slow down", status=429))
        self.assertIn("太频繁", msg)

    def test_friendly_error_timeout(self):
        msg = friendly_error(TimeoutError("timed out"))
        self.assertIn("超时", msg)

    # --- 会话列表 ---

    def test_sessions_api_lists_and_switches(self):
        self.session.permissions.mode = "bypass"
        self.assertTrue(self.post("/api/chat", {"message": "hello"})["ok"])
        self.assertTrue(wait_for(lambda: not self.app.busy, timeout=10))
        first_id = self.session.session_id

        r = self.get("/api/sessions")
        self.assertTrue(r["ok"])
        self.assertEqual(r["current"], first_id)
        self.assertTrue(any(s["id"] == first_id for s in r["sessions"]))

        # 开新会话再切回去
        r = self.post("/api/session", {"action": "new"})
        self.assertTrue(r["ok"])
        new_id = self.session.session_id
        self.assertNotEqual(new_id, first_id)
        self.assertEqual(self.session.agent.messages, [])

        r = self.post("/api/session", {"action": "load", "id": first_id})
        self.assertTrue(r["ok"])
        self.assertEqual(self.session.session_id, first_id)
        self.assertTrue(len(self.session.agent.messages) > 0)

    def test_session_switch_rejected_while_busy(self):
        self.session.agent.busy = True
        try:
            r = self.post("/api/session", {"action": "new"})
            self.assertFalse(r["ok"])
        finally:
            self.session.agent.busy = False

    def test_session_load_rejects_path_traversal(self):
        r = self.post("/api/session", {"action": "load", "id": "../x"})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()

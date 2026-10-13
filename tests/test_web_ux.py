"""Web 交互优化：排队、报错人话化、会话列表。"""

import http.client
import json
import os
import threading
import time
import unittest

from helpers import Workspace

from nanocode.llm.base import LLMError, QuotaExhausted
from nanocode.session import Options, Session
from nanocode.web.server import WebApp, friendly_error, make_server


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
            self.assertTrue(wait_for(lambda: not self.session.agent.busy, timeout=15),
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
        self.assertTrue(wait_for(lambda: not self.session.agent.busy, timeout=10))
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

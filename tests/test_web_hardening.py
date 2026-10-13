"""Web API 加固：对应 issue #6（请求体无上限）和 #3（SSE 令牌放在 URL 里）。"""

import http.client
import json
import os
import socket
import threading
import time
import unittest

from helpers import Workspace

from nanocode.session import Options, Session
from nanocode.web import server as web_server
from nanocode.web.server import MAX_BODY, make_server

STATIC = os.path.join(os.path.dirname(__file__), "..", "nanocode", "web", "static")


class WebHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace()
        cls.session = Session(Options(provider="mock", cwd=cls.ws.dir, mock_delay=0), log=lambda m: None)
        cls.server, cls.app = make_server(cls.session, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.session.close()
        cls.ws.cleanup()

    # ------------------------------------------------------------------ 工具

    def host(self, **extra):
        return {"Host": f"127.0.0.1:{self.port}", **extra}

    def auth(self, **extra):
        return self.host(**{"x-nanocode-token": self.app.token, "content-type": "application/json", **extra})

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or self.host())
        r = conn.getresponse()
        data = r.read().decode("utf-8")
        conn.close()
        return r.status, data

    def raw_post(self, content_length: str, body: bytes = b""):
        """手写 HTTP 请求：http.client 不让我们随便写 Content-Length。返回状态行。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall((f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   f"x-nanocode-token: {self.app.token}\r\ncontent-type: application/json\r\n"
                   f"content-length: {content_length}\r\n\r\n").encode() + body)
        try:
            return s.recv(300).split(b"\r\n")[0].decode()
        finally:
            s.close()

    def open_events(self, query="", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events" + query, headers=headers or self.host())
        r = c.getresponse()
        return c, r

    # ------------------------------------------------------------------ #6 请求体上限

    def test_huge_content_length_is_rejected_without_reading(self):
        status = self.raw_post(str(1536 * 1024 * 1024))
        self.assertIn("413", status)

    def test_body_just_over_limit_is_rejected(self):
        self.assertIn("413", self.raw_post(str(MAX_BODY + 1)))

    def test_invalid_content_length_gets_400_not_a_dropped_connection(self):
        self.assertIn("400", self.raw_post("abc"))
        self.assertIn("400", self.raw_post("-5"))

    def test_normal_and_non_object_bodies_still_work(self):
        status, body = self.req("POST", "/api/chat", json.dumps({"message": "/help"}), self.auth())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        # 合法 JSON 但不是对象：以前会在 data.get 上崩溃，现在当作空请求
        status, body = self.req("POST", "/api/chat", json.dumps([1, 2]), self.auth())
        self.assertEqual(status, 200)

    def test_server_still_serves_after_a_rejected_body(self):
        self.raw_post(str(10 * MAX_BODY))
        self.assertEqual(self.req("GET", "/api/state", headers=self.auth())[0], 200)

    # ------------------------------------------------------------------ #3 一次性票据

    def test_ticket_requires_the_header_token(self):
        self.assertEqual(self.req("POST", "/api/ticket", "{}", self.host(**{"content-type": "application/json"}))[0], 403)
        status, body = self.req("POST", "/api/ticket", "{}", self.auth())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ticket"])

    def test_ticket_opens_the_event_stream_exactly_once(self):
        ticket = json.loads(self.req("POST", "/api/ticket", "{}", self.auth())[1])["ticket"]
        c, r = self.open_events(f"?ticket={ticket}")
        self.assertEqual(r.status, 200)
        r.close()
        c.close()
        c2, r2 = self.open_events(f"?ticket={ticket}")
        self.assertEqual(r2.status, 403)   # 用过的票据作废
        c2.close()

    def test_ticket_expires(self):
        ticket = self.app.issue_ticket()
        old = web_server.TICKET_TTL
        self.app._tickets[ticket] = time.time() - 1   # 直接把它改成已过期
        c, r = self.open_events(f"?ticket={ticket}")
        self.assertEqual(r.status, 403)
        c.close()
        self.assertEqual(web_server.TICKET_TTL, old)

    def test_ticket_only_works_for_the_event_stream(self):
        ticket = self.app.issue_ticket()
        self.assertEqual(self.req("GET", f"/api/state?ticket={ticket}")[0], 403)
        self.assertEqual(self.req("POST", f"/api/chat?ticket={ticket}", "{}",
                                  self.host(**{"content-type": "application/json"}))[0], 403)

    def test_long_lived_token_in_url_is_rejected_everywhere(self):
        t = self.app.token
        self.assertEqual(self.req("GET", f"/api/state?token={t}")[0], 403)
        c, r = self.open_events(f"?token={t}")
        self.assertEqual(r.status, 403)
        c.close()

    def test_unknown_ticket_is_rejected_and_tickets_are_capped(self):
        c, r = self.open_events("?ticket=nope")
        self.assertEqual(r.status, 403)
        c.close()
        self.app._tickets.clear()
        got = [self.app.issue_ticket() for _ in range(web_server.MAX_TICKETS + 5)]
        self.assertEqual(sum(t is not None for t in got), web_server.MAX_TICKETS)
        self.app._tickets.clear()

    def test_page_no_longer_puts_the_token_in_a_url(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("?token=", html)
        self.assertIn('"/api/events?ticket="', html)
        self.assertIn("es.onerror", html)   # 票据一次性，断线后必须自己重连


if __name__ == "__main__":
    unittest.main()

"""模型网关：按会话选 Key 和模型、计量、额度、限流、错误透传、分块编码，以及 nanocode 对 429 的识别。"""

import http.client
import json
import socket
import threading
import time
import unittest
from unittest import mock

import support  # noqa: F401

from nanocode.llm.base import quota_exhausted_message
from nanocode_server.models import Upstream
from nanocode_server.security.keys import internal_key
from nanocode_server.security.secretbox import SecretBox
from nanocode_server.security.tokens import Signer, web_token_digest
from nanocode_server.services.catalog import ModelCatalog
from nanocode_server.storage import MemoryRateLimiter, open_store
from nanocode_server.web.framework import make_server
from nanocode_server.web.gateway import GatewayApp
from support import SECRET, FakeServer, make_config

STREAM = [b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
          b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
          b'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":7}}\n\n',
          b"data: [DONE]\n\n"]


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.status = 200
        self.upstream = FakeServer(self.reply)
        self.cfg = make_config(allow_private_upstreams=True, daily_tokens=1000, requests_per_minute=100,
                               max_output_tokens=512, platform_base_url=self.upstream.url + "/v1",
                               platform_api_key="PLATFORM-KEY", platform_models=["big"])
        self.store = open_store(self.cfg)
        self.catalog = ModelCatalog(self.cfg, self.store, SecretBox(SECRET))
        self.app = GatewayApp(self.cfg, self.store, self.catalog, MemoryRateLimiter(100))
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        cred = self.catalog.add_credential("alice", "custom", "sk-ALICE-OWN-KEY", ["own-model"],
                                           base_url=self.upstream.url + "/v1")
        self.own = self.store.create_session("alice", "s", f"c:{cred.id}:own-model")
        self.platform = self.store.create_session("alice", "p", "p:big")
        self.signer = Signer(SECRET, "gateway")
        self.own_token = self.start_session(self.own)
        self.start_session(self.platform)

    def start_session(self, session, web_token="w" * 32):
        """把会话标成运行中（#53 起网关只认运行中的会话），返回本次沙箱的 web_token。"""
        self.store.update_session(session.id, status="running", endpoint="http://sandbox", web_token=web_token)
        return web_token

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()
        self.store.close()

    def reply(self, method, path, headers, body):
        if self.status != 200:
            return self.status, {"content-type": "application/json"}, b'{"error":{"message":"upstream boom"}}'
        if body and body.get("stream"):
            return 200, {"content-type": "text/event-stream"}, STREAM
        return 200, {"content-type": "application/json"}, json.dumps(
            {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}}).encode()

    def token(self, session, owner=None):
        s = self.store.get_session(session.id) or session
        return self.signer.sign({"u": owner or s.owner, "s": s.id,
                                 "w": web_token_digest(s.web_token)}, 600)

    def post(self, body, token=None, path="/v1/chat/completions", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"content-type": "application/json", **(headers or {})}
        if token:
            h["authorization"] = f"Bearer {token}"
        conn.request("POST", path, json.dumps(body), h)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def test_rejects_bad_tokens(self):
        self.assertEqual(self.post({"messages": []})[0], 401)
        self.assertEqual(self.post({"messages": []}, token="v1.x.y")[0], 401)
        self.assertEqual(self.post({"messages": []}, token=Signer(SECRET, "login").sign({"u": "alice", "s": self.own.id}, 60))[0], 401)
        self.assertEqual(self.post({"messages": []}, token=self.token(self.own, owner="mallory"))[0], 401)
        old = self.token(self.own)
        self.store.update_session(self.own.id, status="stopped", endpoint="")     # 会话停了
        self.assertEqual(self.post({"messages": []}, token=old)[0], 401)
        self.start_session(self.own, web_token="x" * 32)                          # 重启：web_token 轮换
        self.assertEqual(self.post({"messages": []}, token=old)[0], 401)          # 旧代次的令牌也拒绝
        self.store.update_session(self.own.id, status="deleted")
        self.assertEqual(self.post({"messages": []}, token=self.token(self.own))[0], 401)
        self.assertEqual(self.upstream.requests, [])

    def test_token_dies_with_the_sandbox(self):
        """#53：stop 之后旧令牌必须立刻失效，重启后也只有新沙箱的令牌能用。"""
        self.assertEqual(self.post({"messages": []}, self.token(self.own))[0], 200)
        old = self.token(self.own)
        self.store.update_session(self.own.id, status="stopped", endpoint="")
        self.assertEqual(self.post({"messages": []}, token=old)[0], 401)
        self.start_session(self.own, web_token="x" * 32)                          # 重启，换了 web_token
        self.assertEqual(self.post({"messages": []}, token=old)[0], 401)          # 泄漏的旧令牌不能跟着复活
        self.assertEqual(self.post({"messages": []}, self.token(self.own))[0], 200)

    def test_routes_by_session_and_meters(self):
        status, _, data = self.post({"model": "whatever-the-sandbox-says", "stream": True, "max_tokens": 99999,
                                     "messages": [{"role": "user", "content": "hi"}]}, self.token(self.own))
        self.assertEqual(status, 200)
        self.assertEqual(data, b"".join(STREAM))
        sent = self.upstream.requests[0]
        self.assertEqual(sent["headers"]["authorization"], "Bearer sk-ALICE-OWN-KEY")   # 用的是会话选的那把 Key
        self.assertEqual(sent["body"]["model"], "own-model")                             # 模型由会话决定
        self.assertEqual(sent["body"]["max_tokens"], 512)
        self.assertTrue(sent["body"]["stream_options"]["include_usage"])
        u = self.store.get_usage("alice")
        self.assertEqual((u["requests"], u["input_tokens"], u["output_tokens"], u["billable_tokens"]), (1, 120, 7, 0))

    def test_platform_model_quota(self):
        self.post({"messages": []}, self.token(self.platform))
        self.assertEqual(self.upstream.requests[-1]["headers"]["authorization"], "Bearer PLATFORM-KEY")
        self.store.add_usage("alice", 1, 990, 0, billable=True)
        status, headers, data = self.post({"messages": []}, self.token(self.platform))
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["retry-after"]), 0)
        self.assertIsNotNone(quota_exhausted_message(headers, data.decode()))   # nanocode 识别为额度用完，不重试
        # 自己的 Key 不受平台额度限制
        self.assertEqual(self.post({"messages": []}, self.token(self.own))[0], 200)

    def test_rate_limit_is_retryable(self):
        self.app.limiter = MemoryRateLimiter(1)
        self.assertEqual(self.post({"messages": []}, self.token(self.own))[0], 200)
        status, headers, data = self.post({"messages": []}, self.token(self.own))
        self.assertEqual(status, 429)
        self.assertIsNone(quota_exhausted_message(headers, data.decode()))       # 普通限流，nanocode 会退避重试

    def test_upstream_error_and_protocol_mismatch(self):
        self.status = 503
        status, _, data = self.post({"messages": []}, self.token(self.own))
        self.assertEqual(status, 503)
        self.assertIn(b"upstream boom", data)
        self.assertEqual(self.post({"messages": []}, self.token(self.own), path="/v1/messages")[0], 400)

    def test_probe_needs_internal_key(self):
        body = {"protocol": "openai", "base_url": self.upstream.url + "/v1", "api_key": "k", "model": "m"}
        self.assertEqual(self.post(body, token="nope", path="/internal/probe")[0], 401)
        status, _, data = self.post(body, token=internal_key(SECRET), path="/internal/probe")
        self.assertEqual((status, json.loads(data)["ok"]), (200, True))

    def test_probe_error_does_not_echo_upstream_body(self):
        self.status = 503
        body = {"protocol": "openai", "base_url": self.upstream.url + "/v1", "api_key": "k", "model": "m"}
        status, _, data = self.post(body, token=internal_key(SECRET), path="/internal/probe")
        result = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": False, "error": "HTTP 503"})
        self.assertNotIn("upstream boom", data.decode())

    def test_completion_blocks_link_local_before_connecting(self):
        upstream = Upstream("openai", "http://169.254.169.254/v1", "key", "model", billable=False)
        body = {"messages": [{"role": "user", "content": "test"}]}
        connected_to = []
        original_create_connection = socket.create_connection

        def loopback_only(address, *args, **kw):
            connected_to.append(address)
            if address[0] != "127.0.0.1":
                raise OSError("network access forbidden in test")
            return original_create_connection(address, *args, **kw)

        with mock.patch.object(self.catalog, "resolve", return_value=upstream), \
                mock.patch("socket.create_connection", side_effect=loopback_only):
            status, _, data = self.post(body, token=self.token(self.own))
        self.assertEqual(status, 502)
        self.assertIn("链路本地", data.decode())
        self.assertEqual([address[0] for address in connected_to], ["127.0.0.1"])
        self.assertEqual(self.upstream.requests, [])


class ChunkedKeepAliveTest(unittest.TestCase):
    """真实的模型 API 用 HTTP/1.1 分块编码并保持连接：网关必须正确解码并在流结束时立刻收尾记账。"""

    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.conns = []
        threading.Thread(target=self.serve, daemon=True).start()
        self.cfg = make_config(allow_private_upstreams=True)
        self.store = open_store(self.cfg)
        catalog = ModelCatalog(self.cfg, self.store, SecretBox(SECRET))
        cred = catalog.add_credential("alice", "custom", "sk-123456", ["m"],
                                      base_url=f"http://127.0.0.1:{self.listener.getsockname()[1]}/v1")
        self.session = self.store.create_session("alice", "s", f"c:{cred.id}:m")
        self.store.update_session(self.session.id, status="running", web_token="w" * 32)
        self.server = make_server(GatewayApp(self.cfg, self.store, catalog, MemoryRateLimiter(0)), "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.conns.append(conn)
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(65536)
            head, _, rest = buf.partition(b"\r\n\r\n")
            n = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
            while len(rest) < n:
                rest += conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n")
            for e in STREAM:
                conn.sendall(b"%x\r\n%s\r\n" % (len(e), e))
            conn.sendall(b"0\r\n\r\n")          # 流结束，但连接不关

    def tearDown(self):
        self.listener.close()
        for c in self.conns:
            c.close()
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def test_decodes_and_finishes_promptly(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        token = Signer(SECRET, "gateway").sign(
            {"u": "alice", "s": self.session.id, "w": web_token_digest("w" * 32)}, 600)
        conn.request("POST", "/v1/chat/completions", json.dumps({"stream": True, "messages": []}),
                     {"content-type": "application/json", "authorization": f"Bearer {token}"})
        started = time.time()
        data = conn.getresponse().read()
        conn.close()
        self.assertLess(time.time() - started, 5)
        self.assertEqual(data, b"".join(STREAM))
        deadline = time.time() + 2
        while self.store.get_usage("alice")["requests"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.store.get_usage("alice")["input_tokens"], 120)


class QuotaReservationTest(unittest.TestCase):
    """#54：每日额度的「检查 + 扣减」以前是 TOCTOU，并发的请求能合力超支。"""

    def setUp(self):
        self.upstream = FakeServer(self.reply)
        self.cfg = make_config(allow_private_upstreams=True, daily_tokens=5000, requests_per_minute=100,
                               max_output_tokens=100, platform_base_url=self.upstream.url + "/v1",
                               platform_api_key="PLATFORM-KEY", platform_models=["big"])
        self.store = open_store(self.cfg)
        self.catalog = ModelCatalog(self.cfg, self.store, SecretBox(SECRET))
        self.app = GatewayApp(self.cfg, self.store, self.catalog, MemoryRateLimiter(100))
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.session = self.store.create_session("alice", "p", "p:big")
        self.store.update_session(self.session.id, status="running", web_token="w" * 32)
        self.token = Signer(SECRET, "gateway").sign(
            {"u": "alice", "s": self.session.id, "w": web_token_digest("w" * 32)}, 600)
        self.body = {"messages": [{"role": "user", "content": "x" * 2000}]}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()
        self.store.close()

    def reply(self, method, path, headers, body):
        time.sleep(0.05)                      # 放大并发窗口：让请求在网关里重叠
        return 200, {"content-type": "application/json"}, json.dumps(
            {"choices": [{"message": {"content": "hi"}}],
             "usage": {"prompt_tokens": 1200, "completion_tokens": 100}}).encode()

    def test_concurrent_requests_cannot_exceed_daily_quota(self):
        statuses = []

        def call():
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("POST", "/v1/chat/completions", json.dumps(self.body),
                         {"content-type": "application/json", "authorization": f"Bearer {self.token}"})
            statuses.append(conn.getresponse().status)
            conn.close()

        threads = [threading.Thread(target=call) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        # 每次预留 = 输入预估（约 1015）+ 输出上限（100）≈ 1115：5000 的额度只放得下 4 个，
        # 其余必须 429。改成旧的「先读后放行」，20 个会全部通过、合力记 26000。
        self.assertEqual(statuses.count(200), 4)
        self.assertEqual(statuses.count(429), 16)
        self.assertEqual(len(self.upstream.requests), 4)
        used = self.store.get_usage("alice")
        self.assertLessEqual(used["billable_tokens"], 5000)      # 每日总额是硬上限
        self.assertEqual(used["input_tokens"], 4 * 1200)         # 真实用量照记，统计不失真


if __name__ == "__main__":
    unittest.main()

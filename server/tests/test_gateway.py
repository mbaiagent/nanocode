"""模型网关：令牌校验、强制模型、流式转发、计量、限额（并确认 nanocode 能把限额识别成"配额已用完"）。"""

import http.client
import json
import threading
import unittest

import support  # noqa: F401

from nanocode.llm.base import quota_exhausted_message
from nanocode_server import tokens
from nanocode_server.gateway import RateLimiter, UsageMeter, make_gateway_server
from support import SECRET, FakeServer, make_config

OPENAI_STREAM = [
    b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":7}}\n\n',
    b"data: [DONE]\n\n",
]


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeServer(self.upstream_reply)
        self.status = 200
        self.cfg = make_config(upstream_base_url=self.upstream.url + "/v1", upstream_api_key="REAL-KEY",
                               model="locked-model", daily_tokens=1000, requests_per_minute=100, max_output_tokens=512)
        self.server, self.gw = make_gateway_server(self.cfg)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.token = tokens.sign(SECRET, "alice@example.com", "s1", ttl=600)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()

    def upstream_reply(self, method, path, headers, body):
        if self.status != 200:
            return self.status, {"content-type": "application/json"}, b'{"error":{"message":"upstream boom"}}'
        if body and body.get("stream"):
            return 200, {"content-type": "text/event-stream"}, OPENAI_STREAM
        return 200, {"content-type": "application/json"}, json.dumps(
            {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}}).encode()

    def post(self, body, token=None, path="/v1/chat/completions"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"content-type": "application/json"}
        if token is not False:
            headers["authorization"] = f"Bearer {token or self.token}"
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        r = conn.getresponse()
        data = r.read()
        result = r.status, dict(r.getheaders()), data
        conn.close()
        return result

    def test_rejects_missing_or_bad_tokens(self):
        self.assertEqual(self.post({"messages": []}, token=False)[0], 401)
        self.assertEqual(self.post({"messages": []}, token="ncs1.x.y")[0], 401)
        self.assertEqual(self.post({"messages": []}, token=tokens.sign("z" * 40, "a", "s", 60))[0], 401)
        self.assertEqual(self.upstream.requests, [])         # 一个都没转发到上游

    def test_streams_through_and_meters_usage(self):
        status, headers, data = self.post({"model": "expensive-model", "stream": True, "max_tokens": 99999,
                                           "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertIn(b'"content":"hel"', data)
        self.assertIn(b"[DONE]", data)
        sent = self.upstream.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["authorization"], "Bearer REAL-KEY")   # 真实 Key 只在网关加上
        self.assertEqual(sent["body"]["model"], "locked-model")                 # 不让用户选更贵的模型
        self.assertEqual(sent["body"]["max_tokens"], 512)                       # 输出上限被压住
        self.assertTrue(sent["body"]["stream_options"]["include_usage"])        # 要求上游返回用量
        u = self.gw.usage.get("alice@example.com")
        self.assertEqual((u["requests"], u["input_tokens"], u["output_tokens"]), (1, 120, 7))

    def test_daily_quota_is_recognised_by_nanocode(self):
        self.gw.usage.add("alice@example.com", requests=1, input_tokens=990, output_tokens=20)
        status, headers, data = self.post({"messages": []})
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertEqual(self.upstream.requests, [])
        # nanocode 要能识别成"配额已用完"：不再重试，直接告诉用户
        self.assertIsNotNone(quota_exhausted_message(headers, data.decode()))
        # 别人不受影响
        other = tokens.sign(SECRET, "bob@example.com", "s2", ttl=600)
        self.assertEqual(self.post({"messages": []}, token=other)[0], 200)

    def test_rate_limit_is_a_plain_429_nanocode_retries(self):
        self.gw.limiter = RateLimiter(1)
        self.assertEqual(self.post({"messages": []})[0], 200)
        status, headers, data = self.post({"messages": []})
        self.assertEqual(status, 429)
        self.assertIsNone(quota_exhausted_message(headers, data.decode()))     # 不是配额问题，可以退避重试

    def test_upstream_errors_pass_through(self):
        self.status = 503
        status, _, data = self.post({"messages": []})
        self.assertEqual(status, 503)            # nanocode 对 5xx 会退避重试
        self.assertIn(b"upstream boom", data)

    def test_wrong_protocol_path(self):
        self.assertEqual(self.post({"messages": []}, path="/v1/messages")[0], 400)
        self.assertEqual(self.post({"messages": []}, path="/v1/other")[0], 404)

    def test_internal_usage_needs_secret(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/internal/usage?owner=alice@example.com", headers={"authorization": "Bearer nope"})
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/internal/usage?owner=alice@example.com", headers={"authorization": f"Bearer {SECRET}"})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read())["daily_limit"], 1000)
        conn.close()


class ChunkedKeepAliveUpstreamTest(unittest.TestCase):
    """真实的模型 API 用 HTTP/1.1 分块编码并保持连接。网关必须正确解码分块、在流结束时立刻收尾记账，
    而不是把分块长度行当数据转发出去、再一直等上游关连接（真实链路验证时发现的 bug）。"""

    def setUp(self):
        import socket
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.conns = []
        threading.Thread(target=self.serve, daemon=True).start()

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
            length = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
            while len(rest) < length:
                rest += conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n"
                         b"connection: keep-alive\r\n\r\n")
            for event in OPENAI_STREAM:
                conn.sendall(b"%x\r\n%s\r\n" % (len(event), event))
            conn.sendall(b"0\r\n\r\n")       # 流结束，但连接保持打开

    def tearDown(self):
        self.listener.close()
        for c in self.conns:
            c.close()

    def test_decodes_chunks_and_finishes_promptly(self):
        import time
        cfg = make_config(upstream_base_url=f"http://127.0.0.1:{self.port}/v1", upstream_api_key="K", model="m")
        server, gw = make_gateway_server(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/v1/chat/completions", body=json.dumps({"stream": True, "messages": []}),
                         headers={"content-type": "application/json",
                                  "authorization": "Bearer " + tokens.sign(SECRET, "alice", "s", 600)})
            started = time.time()
            data = conn.getresponse().read()       # 网关收尾后关连接，read() 才会返回
            conn.close()
            self.assertLess(time.time() - started, 5)
            self.assertEqual(data, b"".join(OPENAI_STREAM))    # 没有混进分块长度行
            deadline = time.time() + 2
            while gw.usage.get("alice")["requests"] == 0 and time.time() < deadline:
                time.sleep(0.05)
            u = gw.usage.get("alice")
            self.assertEqual((u["requests"], u["input_tokens"], u["output_tokens"]), (1, 120, 7))
        finally:
            server.shutdown()
            server.server_close()


class MeterTest(unittest.TestCase):
    def test_anthropic_stream(self):
        m = UsageMeter("anthropic")
        m.feed_sse_line(b'data: {"type":"message_start","message":{"usage":{"input_tokens":100,"cache_read_input_tokens":900,"output_tokens":1}}}')
        m.feed_sse_line(b'data: {"type":"message_delta","usage":{"output_tokens":42}}')
        self.assertEqual((m.input_tokens, m.output_tokens), (1000, 42))

    def test_ignores_garbage(self):
        m = UsageMeter("openai")
        for line in (b"event: x", b"data: [DONE]", b"data: {broken", b": ping"):
            m.feed_sse_line(line)
        self.assertEqual((m.input_tokens, m.output_tokens), (0, 0))


if __name__ == "__main__":
    unittest.main()

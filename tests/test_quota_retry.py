"""429 的两种含义：瞬时限流可以退避重试，配额用尽（如免费档每日限额）重试毫无意义。对应 issue #1。"""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import helpers  # noqa: F401  （把 NANOCODE_HOME 指到临时目录）

from nanocode.llm import base
from nanocode.llm.base import LLMError, QuotaExhausted, parse_reset, quota_exhausted_message
from nanocode.llm.openai_compat import OpenAICompatProvider

MSGS = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


class ErrorServer:
    """永远返回同一个错误响应，并记录收到了多少次请求。"""

    def __init__(self, status, body, headers=None):
        self.hits = 0
        outer = self
        payload = json.dumps(body).encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                outer.hits += 1
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def err_body(message="Rate limit exceeded"):
    return {"error": {"code": 429, "message": message, "metadata": {"error_type": "rate_limit_exceeded"}}}


class QuotaRetryTest(unittest.TestCase):
    def call(self, server):
        sleeps = []
        p = OpenAICompatProvider("m", api_key="k", base_url=server.url)
        with mock.patch.object(base.time, "sleep", lambda s: sleeps.append(s)):
            try:
                p.complete("", MSGS, [])
                raised = None
            except LLMError as e:
                raised = e
        return raised, sleeps

    def test_daily_quota_by_reset_header_is_not_retried(self):
        reset_ms = str(int((time.time() + 8 * 3600) * 1000))
        srv = ErrorServer(429, err_body(), {"X-RateLimit-Reset": reset_ms, "X-RateLimit-Remaining": "0"})
        self.addCleanup(srv.close)
        err, sleeps = self.call(srv)
        self.assertIsInstance(err, QuotaExhausted)
        self.assertEqual(err.status, 429)
        self.assertEqual((srv.hits, sleeps), (1, []))
        self.assertIn("配额已用完", str(err))
        self.assertIn("恢复", str(err))
        self.assertIn("小时", str(err))

    def test_quota_by_keyword_without_reset_header_is_not_retried(self):
        srv = ErrorServer(429, err_body("Rate limit exceeded: free-models-per-day"))
        self.addCleanup(srv.close)
        err, sleeps = self.call(srv)
        self.assertIsInstance(err, QuotaExhausted)
        self.assertEqual((srv.hits, sleeps), (1, []))
        self.assertIn("没有说明什么时候恢复", str(err))

    def test_transient_429_is_still_retried_with_backoff(self):
        reset_ms = str(int((time.time() + 30) * 1000))      # 30 秒后就恢复：普通的每分钟限流
        srv = ErrorServer(429, err_body(), {"X-RateLimit-Reset": reset_ms})
        self.addCleanup(srv.close)
        err, sleeps = self.call(srv)
        self.assertIsInstance(err, LLMError)
        self.assertNotIsInstance(err, QuotaExhausted)
        self.assertEqual((srv.hits, sleeps), (4, [2, 4, 8]))

    def test_429_without_any_hint_is_retried(self):
        srv = ErrorServer(429, err_body())
        self.addCleanup(srv.close)
        err, sleeps = self.call(srv)
        self.assertNotIsInstance(err, QuotaExhausted)
        self.assertEqual(srv.hits, 4)

    def test_5xx_is_still_retried(self):
        srv = ErrorServer(503, {"error": "overloaded"})
        self.addCleanup(srv.close)
        err, sleeps = self.call(srv)
        self.assertEqual((srv.hits, sleeps), (4, [2, 4, 8]))
        self.assertEqual(err.status, 503)

    def test_other_client_errors_are_not_retried(self):
        srv = ErrorServer(401, {"error": "bad key"})
        self.addCleanup(srv.close)
        err, sleeps = self.call(srv)
        self.assertEqual((srv.hits, sleeps), (1, []))


class ParseResetTest(unittest.TestCase):
    NOW = 1_760_000_000.0

    def test_formats(self):
        self.assertEqual(parse_reset({"X-RateLimit-Reset": "1760028800000"}, self.NOW), 1_760_028_800.0)   # 毫秒
        self.assertEqual(parse_reset({"X-RateLimit-Reset": "1760028800"}, self.NOW), 1_760_028_800.0)      # 秒
        self.assertEqual(parse_reset({"X-RateLimit-Reset": "90"}, self.NOW), self.NOW + 90)                # 相对秒
        self.assertEqual(parse_reset({"Retry-After": "30"}, self.NOW), self.NOW + 30)

    def test_garbage_and_missing(self):
        self.assertIsNone(parse_reset({}, self.NOW))
        self.assertIsNone(parse_reset(None, self.NOW))
        self.assertIsNone(parse_reset({"X-RateLimit-Reset": "soon"}, self.NOW))
        self.assertIsNone(parse_reset({"X-RateLimit-Reset": "-5"}, self.NOW))

    def test_threshold(self):
        near = {"X-RateLimit-Reset": "60"}
        far = {"X-RateLimit-Reset": "7200"}
        self.assertIsNone(quota_exhausted_message(near, "Rate limit exceeded", self.NOW))
        self.assertIn("配额已用完", quota_exhausted_message(far, "Rate limit exceeded", self.NOW))
        self.assertIn("配额已用完", quota_exhausted_message({}, "limit: PER-DAY exceeded", self.NOW))


if __name__ == "__main__":
    unittest.main()

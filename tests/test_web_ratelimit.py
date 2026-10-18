"""/api/chat 限流与预算：对应 issue #7（无限流时别人高频调用能把模型额度刷光）。"""

import datetime as dt
import http.client
import json
import os
import threading
import time
import unittest
from unittest import mock

from helpers import Workspace

from nanocode.llm import base
from nanocode.llm.base import QuotaExhausted
from nanocode.session import Options, Session
from nanocode.web import limits
from nanocode.web.limits import DailyCounter, Limits, TokenBucket, env_int
from nanocode.web.server import make_server


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class TokenBucketTest(unittest.TestCase):
    def test_burst_then_wait_then_refill(self):
        b = TokenBucket(rate_per_min=10, burst=3)
        self.assertEqual([b.take("a", now=0) for _ in range(3)], [0, 0, 0])
        wait = b.take("a", now=0)
        self.assertAlmostEqual(wait, 6.0)            # 每 6 秒补一个
        self.assertEqual(b.take("b", now=0), 0)      # 别的客户端不受影响
        self.assertEqual(b.take("a", now=6.01), 0)
        self.assertGreater(b.take("a", now=6.02), 0)

    def test_zero_disables(self):
        for b in (TokenBucket(0, 5), TokenBucket(10, 0)):
            self.assertTrue(all(b.take("a", now=0) == 0 for _ in range(100)))

    def test_key_cleanup(self):
        b = TokenBucket(60, 2)
        b.MAX_KEYS = 10
        for i in range(10):
            b.take(f"k{i}", now=0)
        b.take("new", now=100)                       # 旧桶早已补满，会被清掉
        self.assertLessEqual(len(b._b), 2)


class DailyCounterTest(unittest.TestCase):
    def test_limit_and_rollover(self):
        c = DailyCounter(2)
        self.assertEqual([c.take(), c.take(), c.take()], [True, True, False])
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        with mock.patch.object(limits.dt, "date", mock.Mock(today=mock.Mock(return_value=tomorrow))):
            self.assertTrue(c.take())

    def test_zero_unlimited(self):
        c = DailyCounter(0)
        self.assertTrue(all(c.take() for _ in range(1000)))

    def test_seconds_to_midnight(self):
        self.assertTrue(1 <= DailyCounter.seconds_to_midnight() <= 86401)

    def test_env_int(self):
        with mock.patch.dict(os.environ, {"X_A": "7", "X_B": "-3", "X_C": "abc", "X_D": "0"}):
            self.assertEqual(env_int("X_A", 1), 7)
            self.assertEqual(env_int("X_B", 1), 0)
            self.assertEqual(env_int("X_C", 1), 1)
            self.assertEqual(env_int("X_D", 1), 0)
            self.assertEqual(env_int("X_MISSING", 4), 4)

    def test_defaults(self):
        keys = ["NANOCODE_WEB_RATE_PER_MIN", "NANOCODE_WEB_RATE_BURST", "NANOCODE_WEB_MAX_QUEUE",
                "NANOCODE_WEB_DAILY_RUNS", "NANOCODE_TRUST_PROXY"]
        with mock.patch.dict(os.environ, {k: "" for k in keys}):
            lim = Limits()
        self.assertEqual((lim.rate_per_min, lim.burst, lim.max_queue, lim.daily_runs.limit, lim.trust_proxy),
                         (10, 5, 3, 200, False))


class DailyLLMCallsTest(unittest.TestCase):
    def setUp(self):
        base._daily_calls = None

    def tearDown(self):
        base._daily_calls = None

    def test_raises_quota_exhausted(self):
        with mock.patch.dict(os.environ, {"NANOCODE_WEB_DAILY_LLM_CALLS": "2"}):
            base.take_daily_llm_call()
            base.take_daily_llm_call()
            with self.assertRaises(QuotaExhausted):
                base.take_daily_llm_call()

    def test_default_unlimited(self):
        with mock.patch.dict(os.environ, {"NANOCODE_WEB_DAILY_LLM_CALLS": ""}):
            for _ in range(500):
                base.take_daily_llm_call()

    def test_counted_before_http_and_not_retried(self):
        """到上限时不发请求、不重试。"""
        from nanocode.llm.openai_compat import OpenAICompatProvider
        p = OpenAICompatProvider("m", api_key="k", base_url="http://127.0.0.1:9/v1", max_retries=3)
        with mock.patch.dict(os.environ, {"NANOCODE_WEB_DAILY_LLM_CALLS": "1"}):
            base.take_daily_llm_call()
            with mock.patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(QuotaExhausted):
                    p.complete("sys", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], [])
                urlopen.assert_not_called()


class WebRateLimitTest(unittest.TestCase):
    ENV = {"NANOCODE_WEB_RATE_PER_MIN": "10", "NANOCODE_WEB_RATE_BURST": "5", "NANOCODE_WEB_MAX_QUEUE": "3",
           "NANOCODE_WEB_DAILY_RUNS": "200", "NANOCODE_TRUST_PROXY": ""}

    def start(self, **env):
        self.ws = Workspace()
        with mock.patch.dict(os.environ, {**self.ENV, **env}):
            # mock 模型第一步会写文件，default 模式下停在权限询问，Agent 一直 busy
            self.session = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
            self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.app.disconnect_grace = 60
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.session.agent.cancel.set()
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        self.ws.cleanup()

    def post(self, path, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token,
             "content-type": "application/json", **(headers or {})}
        c.request("POST", path, body=json.dumps(body), headers=h)
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        return r.status, r.getheader("Retry-After"), data

    def test_burst_then_429(self):
        self.start(NANOCODE_WEB_RATE_BURST="2", NANOCODE_WEB_MAX_QUEUE="0")
        self.assertEqual(self.post("/api/chat", {"message": "写文件"})[0], 200)
        self.assertEqual(self.post("/api/chat", {"message": "再来"})[0], 200)
        status, retry, data = self.post("/api/chat", {"message": "第三次"})
        self.assertEqual(status, 429)
        self.assertEqual(data["code"], "rate_limited")
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])
        self.assertEqual(retry, str(data["retry_after"]))
        self.assertGreaterEqual(int(retry), 1)

    def test_memory_and_local_commands_are_free(self):
        self.start(NANOCODE_WEB_RATE_BURST="1")
        for _ in range(5):
            self.assertEqual(self.post("/api/chat", {"message": "#记住用中文"})[0], 200)
            self.assertEqual(self.post("/api/chat", {"message": "/help"})[0], 200)
        self.assertEqual(self.post("/api/chat", {"message": "写文件"})[0], 200)
        self.assertEqual(self.post("/api/chat", {"message": "再来"})[0], 429)

    def test_queue_full_and_cancel_clears_queue(self):
        self.start(NANOCODE_WEB_MAX_QUEUE="1")
        self.assertEqual(self.post("/api/chat", {"message": "帮我写个文件"})[0], 200)
        self.assertTrue(wait_for(lambda: len(self.app.pending) == 1), "权限询问没有出现")
        status, _, data = self.post("/api/chat", {"message": "排队一"})
        self.assertEqual((status, data.get("queued")), (200, True))
        status, retry, data = self.post("/api/chat", {"message": "排队二"})
        self.assertEqual((status, data["code"], retry), (429, "queue_full", "5"))
        self.assertEqual(len(self.app.queue), 1)
        self.post("/api/cancel", {})
        self.assertEqual(self.app.queue, [])
        self.assertTrue(wait_for(lambda: not self.app.busy), "中断后 busy 没有释放")

    def test_daily_runs(self):
        self.start(NANOCODE_WEB_DAILY_RUNS="1")
        self.assertEqual(self.post("/api/chat", {"message": "帮我写个文件"})[0], 200)
        self.assertTrue(wait_for(lambda: len(self.app.pending) == 1))
        self.assertEqual(self.post("/api/chat", {"message": "排队"})[2].get("queued"), True)
        # 出队时预算已用完：丢弃队列、报错、释放 busy
        self.post("/api/permission", {"id": next(iter(self.app.pending)), "decision": "no"})
        self.assertTrue(wait_for(lambda: not self.app.busy, 10), "busy 没有释放")
        self.assertEqual(self.app.queue, [])
        errors = [e for e in self.session.bus.history if e["type"] == "error"]
        self.assertTrue(any("预算" in e.get("message", "") for e in errors))
        status, retry, data = self.post("/api/chat", {"message": "新的一轮"})
        self.assertEqual((status, data["code"]), (429, "daily_budget"))
        self.assertGreaterEqual(int(retry), 1)

    def test_xff_ignored_unless_trusted(self):
        self.start(NANOCODE_WEB_RATE_BURST="1", NANOCODE_WEB_MAX_QUEUE="0")
        self.assertEqual(self.post("/api/chat", {"message": "a"}, {"X-Forwarded-For": "1.1.1.1"})[0], 200)
        self.assertEqual(self.post("/api/chat", {"message": "b"}, {"X-Forwarded-For": "2.2.2.2"})[0], 429)

    def test_xff_used_when_trusted(self):
        self.start(NANOCODE_WEB_RATE_BURST="1", NANOCODE_WEB_MAX_QUEUE="0", NANOCODE_TRUST_PROXY="1")
        self.assertEqual(self.post("/api/chat", {"message": "a"}, {"X-Forwarded-For": "1.1.1.1, 10.0.0.1"})[0], 200)
        self.assertEqual(self.post("/api/chat", {"message": "b"}, {"X-Forwarded-For": "2.2.2.2"})[0], 200)
        self.assertEqual(self.post("/api/chat", {"message": "c"}, {"X-Forwarded-For": "1.1.1.1"})[0], 429)

    def test_all_zero_disables(self):
        self.start(NANOCODE_WEB_RATE_BURST="0", NANOCODE_WEB_RATE_PER_MIN="0",
                   NANOCODE_WEB_MAX_QUEUE="0", NANOCODE_WEB_DAILY_RUNS="0")
        statuses = [self.post("/api/chat", {"message": f"m{i}"})[0] for i in range(12)]
        self.assertEqual(set(statuses), {200})


if __name__ == "__main__":
    unittest.main()

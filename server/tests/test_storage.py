"""存储和限流：SQLite 和内存总是测；配了 NCS_TEST_DATABASE_URL / NCS_TEST_REDIS_URL 再测 PostgreSQL / Redis。"""

import tempfile
import threading
import unittest
import uuid

import support  # noqa: F401

from nanocode_server.models import Credential
from nanocode_server.storage import Database, Store
from nanocode_server.storage.ratelimit import MemoryRateLimiter, RedisRateLimiter
from support import PG_URL, REDIS_URL


class StoreContract:
    """两种数据库都要满足的行为。子类提供 make_store()。"""

    def setUp(self):
        self.store = self.make_store()
        self.alice = f"alice-{uuid.uuid4().hex[:6]}"       # 共享的 PostgreSQL 里不和别的测试撞
        self.bob = f"bob-{uuid.uuid4().hex[:6]}"

    def tearDown(self):
        self.store.close()

    def test_sessions(self):
        s = self.store.create_session(self.alice, "第一个", "mock")
        self.store.create_session(self.bob, "别人的", "mock")
        self.assertEqual([x.id for x in self.store.list_sessions(self.alice)], [s.id])
        self.store.update_session(s.id, status="running", endpoint="http://x", web_token="t")
        got = self.store.get_session(s.id)
        self.assertEqual((got.status, got.endpoint, got.owner), ("running", "http://x", self.alice))
        self.assertIn(s.id, [x.id for x in self.store.active_sessions()])
        self.store.update_session(s.id, status="deleted")
        self.assertIsNone(self.store.get_session(s.id))
        self.assertEqual(self.store.list_sessions(self.alice), [])
        with self.assertRaises(ValueError):
            self.store.update_session(s.id, owner="mallory")      # 不能改所有者

    def test_claim_start_is_exclusive(self):
        """多个控制面副本同时访问一个已停止的会话：只有一个能抢到启动权。"""
        s = self.store.create_session(self.alice, "x", "mock")
        self.assertTrue(self.store.claim_start(s.id, "t1", now=1000))
        self.assertFalse(self.store.claim_start(s.id, "t2", now=1001))           # 别人正在启动
        self.assertEqual(self.store.get_session(s.id).web_token, "t1")
        self.assertTrue(self.store.claim_start(s.id, "t3", now=1000 + 121))      # 启动的人挂了，超时后可以重抢
        self.store.update_session(s.id, status="running")
        self.assertFalse(self.store.claim_start(s.id, "t4", now=5000))           # 已经在运行
        results = []
        self.store.update_session(s.id, status="stopped")
        threads = [threading.Thread(target=lambda: results.append(self.store.claim_start(s.id, "x"))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)

    def test_credentials_are_per_owner(self):
        c = self.store.add_credential(Credential("", self.alice, "deepseek", "我的", "openai", "https://x/v1",
                                                 ["a", "b"], "ciphertext", "…1234"))
        self.assertEqual(self.store.get_credential(c.id).models, ["a", "b"])
        self.assertEqual(self.store.list_credentials(self.bob), [])
        self.assertFalse(self.store.delete_credential(self.bob, c.id))     # 别人删不掉
        self.assertTrue(self.store.delete_credential(self.alice, c.id))
        self.assertIsNone(self.store.get_credential(c.id))

    def test_usage_accumulates_and_separates_billable(self):
        self.store.add_usage(self.alice, 1, 100, 10, billable=True)
        self.store.add_usage(self.alice, 1, 50, 5, billable=False)
        u = self.store.get_usage(self.alice)
        self.assertEqual((u["requests"], u["total_tokens"], u["billable_tokens"]), (2, 165, 110))
        self.assertEqual(self.store.get_usage(self.bob)["requests"], 0)

    def test_reserve_quota_is_atomic(self):
        """#54：检查 + 预留必须原子。并发的预留都读到同一个旧值，就会合力超额。"""
        day, limit = "2026-01-01", 100
        self.store.add_usage(self.alice, 0, 0, 0, billable=False, day=day)      # 先把当天的行建出来
        results = []
        barrier = threading.Barrier(10)

        def reserve():
            barrier.wait()
            results.append(self.store.reserve_quota(self.alice, 30, limit, day=day))

        threads = [threading.Thread(target=reserve) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results.count(True), 3)          # 100 只放得下 3 个 30
        self.assertEqual(self.store.get_usage(self.alice, day=day)["billable_tokens"], 90)

    def test_reserve_quota_rejects_when_full_and_settle_refunds(self):
        day = "2026-01-01"
        self.assertTrue(self.store.reserve_quota(self.alice, 60, 100, day=day))
        self.assertFalse(self.store.reserve_quota(self.alice, 60, 100, day=day))      # 120 > 100，放不下
        self.assertEqual(self.store.get_usage(self.alice, day=day)["billable_tokens"], 60)
        self.store.settle_usage(self.alice, 1, 35, 5, 40 - 60, day=day)                 # 实际只用 40：退 20
        u = self.store.get_usage(self.alice, day=day)
        self.assertEqual((u["requests"], u["input_tokens"], u["output_tokens"], u["billable_tokens"]),
                         (1, 35, 5, 40))

    def test_reserve_quota_zero_limit_means_unlimited(self):
        self.assertTrue(self.store.reserve_quota(self.alice, 10 ** 9, 0))


class SqliteStoreTest(StoreContract, unittest.TestCase):
    def make_store(self):
        return Store(Database.open("", tempfile.mkdtemp()))


@unittest.skipUnless(PG_URL, "设置 NCS_TEST_DATABASE_URL 才测 PostgreSQL")
class PostgresStoreTest(StoreContract, unittest.TestCase):
    def make_store(self):
        return Store(Database.open(PG_URL, ""))

    def test_backend(self):
        self.assertEqual(self.store.backend, "postgres")


class DatabaseUrlTest(unittest.TestCase):
    def test_unknown_scheme_refused(self):
        with self.assertRaises(ValueError):
            Database.open("mysql://x", "")


class RateLimitContract:
    def test_limits_per_key(self):
        rl = self.make(2)
        k = uuid.uuid4().hex
        self.assertTrue(rl.allow(k, now=1000))
        self.assertTrue(rl.allow(k, now=1001))
        self.assertFalse(rl.allow(k, now=1002))
        self.assertTrue(rl.allow("other-" + k, now=1002))   # 别人不受影响
        self.assertTrue(rl.allow(k, now=1100))              # 下一分钟恢复

    def test_zero_means_unlimited(self):
        rl = self.make(0)
        self.assertTrue(all(rl.allow("x") for _ in range(100)))


class MemoryRateLimitTest(RateLimitContract, unittest.TestCase):
    def make(self, n):
        return MemoryRateLimiter(n)


@unittest.skipUnless(REDIS_URL, "设置 NCS_TEST_REDIS_URL 才测 Redis")
class RedisRateLimitTest(RateLimitContract, unittest.TestCase):
    def make(self, n):
        return RedisRateLimiter(REDIS_URL, n, prefix=f"ncs-test-{uuid.uuid4().hex[:6]}:")


if __name__ == "__main__":
    unittest.main()

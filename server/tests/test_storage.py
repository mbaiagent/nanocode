"""存储和限流：SQLite 和内存总是测；配了 NCS_TEST_DATABASE_URL / NCS_TEST_REDIS_URL 再测 PostgreSQL / Redis。"""

import os
import tempfile
import threading
import unittest
import uuid

import support  # noqa: F401

from nanocode_server.models import Credential
from nanocode_server.storage import Database, Store
from nanocode_server.storage.ratelimit import MemoryRateLimiter, RedisRateLimiter
from nanocode_server.storage.sql import ConnectionPool, StorageBusy, SCHEMA_VERSION
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
        # 启动超时最慢是 K8s 的 120 秒：stale_after 必须比它大，否则启动途中会被重抢（#59）
        self.assertFalse(self.store.claim_start(s.id, "t2", now=1130))
        self.assertTrue(self.store.claim_start(s.id, "t3", now=1000 + 301))      # 启动的人挂了，超时后可以重抢
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

    def test_finish_start_needs_the_claim_token(self):
        """启动完成时必须还持着这次启动（#59）：换了令牌就是失去启动权，写不进去。"""
        s = self.store.create_session(self.alice, "x", "mock")
        self.assertTrue(self.store.claim_start(s.id, "t1", now=1000))
        self.assertTrue(self.store.finish_start(s.id, "t1", "http://sbx", 1100))
        self.assertEqual(self.store.get_session(s.id).endpoint, "http://sbx")
        # 被重抢（新令牌）之后，拿着旧令牌的副本不能再写 running
        self.store.update_session(s.id, status="stopped", endpoint="")
        self.assertTrue(self.store.claim_start(s.id, "t2", now=1200))
        self.assertFalse(self.store.finish_start(s.id, "t1", "http://old", 1300))
        self.assertEqual(self.store.get_session(s.id).endpoint, "")   # 旧令牌的写入被丢弃
        self.assertTrue(self.store.finish_start(s.id, "t2", "http://new", 1300))

    def test_start_cannot_resurrect_a_deleted_session(self):
        """启动期间被删除：完成时不能把 deleted 写回 running（#59 的复活 bug）。"""
        s = self.store.create_session(self.alice, "x", "mock")
        self.assertTrue(self.store.claim_start(s.id, "t1", now=1000))
        self.store.update_session(s.id, status="deleted", endpoint="", web_token="")
        self.assertFalse(self.store.finish_start(s.id, "t1", "http://sbx", 1100))
        self.assertFalse(self.store.fail_start(s.id, "t1", "boom"))
        self.assertFalse(self.store.stop_session(s.id))
        self.assertIsNone(self.store.get_session(s.id))          # 还是删掉的，列表里也没有

    def test_mark_dead_only_from_running(self):
        """mark_dead 不能把 stopped/deleted 覆盖掉（#59）。"""
        s = self.store.create_session(self.alice, "x", "mock")
        self.store.update_session(s.id, status="running", endpoint="http://x")
        self.assertTrue(self.store.mark_dead(s.id))
        self.assertEqual(self.store.get_session(s.id).status, "stopped")
        self.assertFalse(self.store.mark_dead(s.id))             # 已经不是 running 了
        self.store.update_session(s.id, status="running", endpoint="http://x")
        self.store.update_session(s.id, status="deleted", endpoint="", web_token="")
        self.assertFalse(self.store.mark_dead(s.id))
        self.assertIsNone(self.store.get_session(s.id))

    def test_update_session_cannot_revive_deleted(self):
        """通用更新也受终态保护：title/last_active 之类也不行（#59）。"""
        s = self.store.create_session(self.alice, "x", "mock")
        self.store.update_session(s.id, status="deleted", endpoint="", web_token="")
        self.store.update_session(s.id, title="改了名")
        self.assertIsNone(self.store.get_session(s.id))

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


class SchemaMigrationTest(unittest.TestCase):
    """#64：schema_version + 顺序迁移。旧库要能升级，比代码新的库要拒绝启动。"""

    def make_db(self):
        return Database.open("", tempfile.mkdtemp())

    def test_version_is_recorded_and_migrate_is_idempotent(self):
        db = self.make_db()
        self.addCleanup(db.close)
        store = Store(db)
        self.assertEqual(db.execute("SELECT version FROM schema_version WHERE id = 1")[0]["version"], SCHEMA_VERSION)
        store.db.migrate()                                   # 再跑一遍不能出错
        self.assertEqual(db.execute("SELECT version FROM schema_version WHERE id = 1")[0]["version"], SCHEMA_VERSION)
        store.create_session("alice", "x", "mock")           # 升级完照常能用

    def test_old_database_without_version_table_is_upgraded(self):
        """升级前的老库：表在、没有 schema_version。打开时要补上版本号而不是报错。"""
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "platform.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
            model_ref TEXT NOT NULL, status TEXT NOT NULL, endpoint TEXT NOT NULL DEFAULT '',
            web_token TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL,
            last_active DOUBLE PRECISION NOT NULL, started DOUBLE PRECISION NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '')""")
        conn.commit()
        conn.close()
        db = Database.open(f"sqlite:///{path}", "")
        self.addCleanup(db.close)
        store = Store(db)
        self.assertEqual(db.execute("SELECT version FROM schema_version WHERE id = 1")[0]["version"], SCHEMA_VERSION)
        self.assertIsNone(store.get_session("nope"))

    def test_newer_database_is_refused(self):
        """数据库是更新的代码写的：拒绝启动，别拿旧结构去写把数据弄坏（#64）。"""
        db = self.make_db()
        path = db.path
        Store(db)
        db.close()
        db = Database.open(f"sqlite:///{path}", "")
        db.execute("UPDATE schema_version SET version = 99 WHERE id = 1")
        db.close()
        with self.assertRaises(RuntimeError):
            Store(Database.open(f"sqlite:///{path}", ""))


class PgPlaceholderTest(unittest.TestCase):
    """#64：占位符转换不能动字符串字面量里的 ?。"""

    def test_question_mark_inside_literals_is_left_alone(self):
        from nanocode_server.storage.sql import _pg_placeholders
        self.assertEqual(_pg_placeholders("WHERE title LIKE '%?%' AND id = ?"),
                         "WHERE title LIKE '%?%' AND id = %s")
        self.assertEqual(_pg_placeholders("VALUES ('it''s ?', ?)"), "VALUES ('it''s ?', %s)")
        self.assertEqual(_pg_placeholders("SELECT '?'"), "SELECT '?'")
        self.assertEqual(_pg_placeholders("SELECT ?"), "SELECT %s")


class _FakeCursor:
    description = ["v"]
    rowcount = 1

    def fetchall(self):
        return [{"v": 1}]


class _FakeConn:
    """能演"用一下才坏"的假连接。"""

    def __init__(self, broken_on_use: bool = False):
        self.closed = self.broken = False
        self.broken_on_use = broken_on_use
        self.executed = 0

    def execute(self, sql, params=()):
        self.executed += 1
        if self.broken_on_use:
            self.broken = True
            raise OSError("connection reset by peer")
        return _FakeCursor()

    def close(self):
        self.closed = True


class ConnectionPoolTest(unittest.TestCase):
    """#64：坏连接必须 close 再放掉槽位；池超时必须明确报 StorageBusy（#60 映射成 503）。"""

    def test_reuses_connection_and_caps_size(self):
        conns = []

        def factory():
            c = _FakeConn()
            conns.append(c)
            return c

        pool = ConnectionPool(factory, size=2, timeout=0.2)
        with pool.connection() as c1:
            with pool.connection() as c2:
                self.assertIsNot(c1, c2)
                with self.assertRaises(StorageBusy):
                    with pool.connection():
                        pass                     # 已经 2 个了，等不到第三个
        with pool.connection() as c3:
            self.assertIs(c3, c1)                # 释放后被复用（LIFO），没有无限新建

    def test_broken_connection_is_closed_and_slot_released(self):
        conns = []

        def factory():
            c = _FakeConn(broken_on_use=not conns)     # 第一个是坏的
            conns.append(c)
            return c

        pool = ConnectionPool(factory, size=2, timeout=0.2)
        with self.assertRaises(OSError):
            with pool.connection() as c:
                c.execute("SELECT 1")
        self.assertTrue(conns[0].closed)          # 只减计数不 close 会泄漏 socket（#64）
        with pool.connection() as c:              # 槽位释放了，能建新连接并正常用
            self.assertEqual(c.execute("SELECT 1").fetchall(), [{"v": 1}])
        self.assertIs(c, conns[1])


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

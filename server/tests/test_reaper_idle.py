"""空闲时回收线程不碰数据库（#131）+ 启动时数据库连接退避重试（#132 的一部分）。

托管 Postgres（Neon 免费版）闲置约 5 分钟才缩到零。回收线程以前每 60 秒无条件查一次
active_sessions，计算节点永远醒着，免费额度半个月就用完。现在：启动时对账查一次，
之后只有「可能有运行中的沙箱」时才查，而且最早的回收截止时间没到也不查。
"""

import os
import time
import unittest
from unittest import mock

import support

from nanocode_server.config import Config
from nanocode_server.services.catalog import MOCK_REF
from nanocode_server.services.sessions import SessionManager
from nanocode_server.storage.sql import ConnectionPool, SqliteDatabase, Store, wait_for_database


class CountingDatabase(SqliteDatabase):
    """真的 SQLite，外加记下每一条 SQL：证明「空闲时零查询」要数得清楚。"""

    def __init__(self, path):
        super().__init__(path)
        self.queries: list[str] = []

    def execute(self, sql, params=()):
        self.queries.append(sql)
        return super().execute(sql, params)

    def update(self, sql, params=()):
        self.queries.append(sql)
        return super().update(sql, params)


_OPEN: list[CountingDatabase] = []


def make(cfg=None, db=None):
    cfg = cfg or support.make_config()
    if db is None:
        db = CountingDatabase(os.path.join(cfg.data_dir, "platform.db"))
        _OPEN.append(db)
    store = Store(db)
    launcher = mock.MagicMock()
    launcher.remote = False
    launcher.gateway_url_for_sandbox.return_value = "http://127.0.0.1:1"
    launcher.start.return_value = "http://127.0.0.1:2"
    launcher.is_alive.return_value = True
    catalog = mock.MagicMock()
    catalog.default_ref.return_value = MOCK_REF
    catalog.option.return_value = mock.MagicMock(ref=MOCK_REF, model="mock", protocol="openai")
    mgr = SessionManager(cfg, store, launcher, catalog, log=lambda m: None)
    mgr.sandbox_state = lambda s: {"busy": False}
    db.queries.clear()
    return mgr, store, db, launcher


class StopAfter:
    """假的 threading.Event：记下每次 wait 的间隔，第 n 次之后让 reap_forever 退出。"""

    def __init__(self, n):
        self.n, self.waits = n, []

    def wait(self, interval):
        self.waits.append(interval)
        return len(self.waits) > self.n


class IdleReaperTest(unittest.TestCase):
    def tearDown(self):
        while _OPEN:
            _OPEN.pop().close()

    def test_idle_platform_never_queries(self):
        mgr, _, db, _ = make()
        mgr.reconcile()                         # 启动对账：必须查库
        self.assertTrue(db.queries)
        db.queries.clear()
        for i in range(100):                    # 相当于空跑 100 分钟
            self.assertEqual(mgr.reap_tick(now=time.time() + 60 * i), [])
        mgr.reap_forever(StopAfter(50))
        self.assertEqual(db.queries, [])
        self.assertFalse(mgr.reap_pending)

    def test_reap_once_without_live_sandboxes_skips_db(self):
        mgr, _, db, _ = make()
        mgr.reconcile()
        db.queries.clear()
        self.assertEqual(mgr.reap_once(), [])
        self.assertEqual(db.queries, [])

    def test_running_sandbox_is_reaped_then_idle_again(self):
        mgr, store, db, launcher = make()
        mgr.reconcile()
        s = mgr.create("alice", "t")
        self.assertEqual(s.status, "running")
        self.assertTrue(mgr.reap_pending)
        now = time.time()

        db.queries.clear()
        self.assertEqual(mgr.reap_tick(now=now), [])       # 第一轮查一次，记下截止时间
        self.assertEqual(len(db.queries), 1)
        db.queries.clear()
        for i in range(1, 25):                               # 截止时间（30 分钟）之前：不查
            self.assertEqual(mgr.reap_tick(now=now + 60 * i), [])
        self.assertEqual(db.queries, [])

        later = now + mgr.cfg.idle_timeout + 5
        self.assertEqual(mgr.reap_tick(now=later), [s.id])  # 到点：查库、回收
        launcher.stop.assert_called_with(s.id)
        self.assertEqual(store.get_session(s.id).status, "stopped")
        self.assertFalse(mgr.reap_pending)

        db.queries.clear()
        for i in range(1, 50):                               # 回收完又回到零查询
            mgr.reap_tick(now=later + 60 * i)
        self.assertEqual(db.queries, [])

    def test_activity_pushes_deadline_without_missing_reap(self):
        mgr, store, db, _ = make()
        mgr.reconcile()
        s = mgr.create("bob", "t")
        now = time.time()
        mgr.reap_tick(now=now)
        # 用户中途有请求（touch）：缓存的截止时间偏早，到点查一次发现还没空闲，不回收，重新算
        store.update_session(s.id, last_active=now + 600)
        self.assertEqual(mgr.reap_tick(now=now + mgr.cfg.idle_timeout + 5), [])
        self.assertTrue(mgr.reap_pending)
        self.assertEqual(mgr.reap_tick(now=now + 600 + mgr.cfg.idle_timeout + 5), [s.id])

    def test_busy_agent_is_not_reaped(self):
        mgr, store, _, _ = make()
        mgr.reconcile()
        s = mgr.create("carol", "t")
        mgr.sandbox_state = lambda s: {"busy": True}
        now = time.time() + mgr.cfg.idle_timeout + 5
        self.assertEqual(mgr.reap_tick(now=now), [])
        self.assertEqual(store.get_session(s.id).status, "running")
        self.assertTrue(mgr.reap_pending)

    def test_restart_reconciles_once_then_reaps_existing_sandbox(self):
        cfg = support.make_config()
        mgr, store, db, _ = make(cfg)
        mgr.reconcile()
        s = mgr.create("dave", "t")
        store.update_session(s.id, last_active=time.time() - 7200)

        # 控制面重启：新的 SessionManager 不知道有沙箱在跑，靠启动对账从数据库里找回来
        mgr2, _, db2, launcher2 = make(cfg, db)
        self.assertFalse(mgr2.reap_pending)
        mgr2.reconcile()
        self.assertTrue(mgr2.reap_pending)
        self.assertEqual(mgr2.reap_tick(), [s.id])
        launcher2.stop.assert_called_with(s.id)
        db2.queries.clear()
        mgr2.reap_tick(now=time.time() + 3600)
        self.assertEqual(db2.queries, [])

    def test_restart_with_dead_sandbox_goes_idle(self):
        cfg = support.make_config()
        mgr, store, db, _ = make(cfg)
        mgr.reconcile()
        s = mgr.create("erin", "t")
        mgr2, _, _, launcher2 = make(cfg, db)
        launcher2.is_alive.return_value = False             # 重启后沙箱已经不在了
        mgr2.reconcile()
        self.assertEqual(store.get_session(s.id).status, "stopped")
        self.assertFalse(mgr2.reap_pending)

    def test_sandbox_started_during_query_keeps_marker(self):
        """查库期间有沙箱刚起好：这次查到的「没有」不能把标记清掉，否则它永远不会被回收。"""
        mgr, store, _, _ = make()
        mgr._mark_live()
        real = store.active_sessions

        def racing():
            rows = real()
            mgr._mark_live()                    # 查询返回之后、清标记之前，另一个线程起了沙箱
            return rows

        with mock.patch.object(store, "active_sessions", racing):
            mgr.reap_once()
        self.assertTrue(mgr.reap_pending)

    def test_session_running_elsewhere_marks_live(self):
        """别的副本起好的沙箱被本副本访问到：本副本也要开始回收它。"""
        mgr, store, _, _ = make()
        s = store.create_session("frank", "t", MOCK_REF)
        store.update_session(s.id, status="running", endpoint="http://127.0.0.1:3", started=time.time())
        self.assertFalse(mgr.reap_pending)
        mgr.ensure_running(s.id)
        self.assertTrue(mgr.reap_pending)

    def test_multi_replica_keeps_polling(self):
        mgr, _, db, _ = make(support.make_config(replicas=2))
        mgr.reconcile()
        db.queries.clear()
        mgr.reap_tick()
        mgr.reap_tick()
        self.assertEqual(len(db.queries), 2)

    def test_reap_interval_configurable(self):
        mgr, _, _, _ = make(support.make_config(reap_interval=900))
        ev = StopAfter(2)
        mgr.reap_forever(ev)
        self.assertEqual(ev.waits, [900, 900, 900])
        with mock.patch.dict(os.environ, {"NCS_REAP_INTERVAL": "300"}):
            self.assertEqual(Config.from_env().reap_interval, 300)
        cfg = support.make_config(auth="dev", reap_interval=0)
        with self.assertRaisesRegex(ValueError, "NCS_REAP_INTERVAL"):
            cfg.validate(launcher_needed=False)

    def test_reaper_thread_survives_errors(self):
        mgr, store, _, _ = make()
        mgr._mark_live()
        logs = []
        mgr.log = logs.append
        with mock.patch.object(store, "active_sessions", side_effect=RuntimeError("db down")):
            mgr.reap_forever(StopAfter(2), interval=0)
        self.assertEqual(len(logs), 2)
        self.assertTrue(mgr.reap_pending)       # 出错不清标记，下一轮再试


class StartupRetryTest(unittest.TestCase):
    def pool(self, failures):
        calls = []

        def connect():
            calls.append(1)
            if len(calls) <= failures:
                raise OSError("connection refused")
            return mock.MagicMock(closed=False, broken=False)

        return ConnectionPool(connect, 2), calls

    def test_retries_with_backoff_then_succeeds(self):
        pool, calls = self.pool(3)
        sleeps, logs = [], []
        wait_for_database(pool, timeout=30, sleep=sleeps.append, log=logs.append, clock=lambda: 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleeps, [0.5, 1, 2])
        self.assertEqual(len(logs), 3)
        self.assertIn("OSError", logs[0])

    def test_gives_up_after_timeout(self):
        pool, calls = self.pool(1000)
        t = [0.0]

        def sleep(s):
            t[0] += s

        with self.assertRaises(OSError):
            wait_for_database(pool, timeout=30, sleep=sleep, log=lambda m: None, clock=lambda: t[0])
        self.assertLessEqual(t[0], 30)
        self.assertGreater(len(calls), 3)

    def test_first_try_success_does_not_sleep(self):
        pool, calls = self.pool(0)
        sleeps = []
        wait_for_database(pool, sleep=sleeps.append, log=lambda m: None)
        self.assertEqual((len(calls), sleeps), (1, []))


if __name__ == "__main__":
    unittest.main()

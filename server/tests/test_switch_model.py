"""switch_model 的原子性（issue #61）。

换模型要「查 busy → stop 沙箱 → 改 DB → 用新模型重启」。这四步以前不持锁：
检查通过后新来的 /api/chat 会被 stop 杀掉当前回合；另一个请求在 stop 之后、
ensure_running 之前插进来，会拿**旧** model_ref 把沙箱拉起来，于是
「数据库说新模型、沙箱跑的还是旧模型」，而网关只认数据库，两边就打架了。
"""

import threading
import unittest
from unittest import mock

import support  # noqa: F401

from nanocode_server.models import Session
from nanocode_server.services.sessions import SessionError, SessionManager

SID = "0123456789abcdef"
OWNER = "u1"


class FakeStore:
    """够用的假存储：get_session 返回同一个对象，update_session 真的改它。"""

    def __init__(self, s: Session):
        self.s = s
        self.updates: list[dict] = []

    def get_session(self, sid):
        return self.s if sid == SID else None

    def update_session(self, sid, **fields):
        self.updates.append(fields)
        for k, v in fields.items():
            setattr(self.s, k, v)

    def active_sessions(self):
        return [self.s] if self.s.active else []

    def claim_start(self, sid, token):
        return True

    # #59 之后的守卫式写入：都真的改状态，并像真的存储一样返回是否写进去了
    def finish_start(self, sid, token, endpoint, now):
        self.updates.append({"status": "running"})
        self.s.status, self.s.endpoint, self.s.started = "running", endpoint, now
        return True

    def fail_start(self, sid, token, error):
        self.updates.append({"status": "failed", "error": error})
        self.s.status, self.s.error = "failed", error
        return True

    def stop_session(self, sid):
        self.updates.append({"status": "stopped"})
        self.s.status, self.s.endpoint = "stopped", ""
        return True

    def mark_dead(self, sid):
        return self.stop_session(sid)


def make_manager(s: Session):
    cfg = support.make_config()
    store = FakeStore(s)
    launcher = mock.MagicMock()
    catalog = mock.MagicMock()
    catalog.option.return_value = mock.MagicMock(ref="new-ref", model="new-model", protocol="openai")
    return SessionManager(cfg, store, launcher, catalog, log=lambda m: None), store, launcher


def session(**kw):
    base = dict(id=SID, owner=OWNER, title="t", model_ref="old-ref", status="running",
                endpoint="http://127.0.0.1:1", web_token="tok")
    base.update(kw)
    return Session(**base)


def lock_is_free(mgr) -> bool:
    """从**另一个线程**探测 per-session 锁有没有被占用。

    不能在本线程探测：锁是 RLock，同一线程 acquire(blocking=False) 永远成功，
    那样连"根本没持锁"都发现不了。
    """
    box = []

    def probe():
        lk = mgr._lock(SID)
        got = lk.acquire(blocking=False)
        box.append(got)
        if got:
            lk.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return box[0] if box else False


class SwitchModelTest(unittest.TestCase):
    def test_rejects_while_agent_is_busy(self):
        mgr, store, launcher = make_manager(session())
        mgr.sandbox_state = lambda s: {"busy": True, "queue": 0}
        with self.assertRaises(SessionError) as ctx:
            mgr.switch_model(OWNER, SID, "new-ref")
        self.assertEqual(ctx.exception.status, 409)
        launcher.stop.assert_not_called()                     # 忙的时候一步都不该做
        self.assertEqual(store.updates, [])

    def test_rejects_while_messages_are_queued(self):
        mgr, _, _ = make_manager(session())
        mgr.sandbox_state = lambda s: {"busy": False, "queue": 2}
        with self.assertRaises(SessionError) as ctx:
            mgr.switch_model(OWNER, SID, "new-ref")
        self.assertEqual(ctx.exception.status, 409)

    def test_busy_check_happens_inside_the_lock(self):
        """查 busy 必须在持锁状态下做，否则查完到 stop 之间又能插进来一个 chat。"""
        mgr, store, launcher = make_manager(session())
        seen = {}

        def sandbox_state(s):
            seen["free"] = lock_is_free(mgr)
            return {"busy": False, "queue": 0}

        mgr.sandbox_state = sandbox_state
        mgr.switch_model(OWNER, SID, "new-ref")
        self.assertEqual(seen, {"free": False})               # 查的时候锁是拿着的
        self.assertTrue(lock_is_free(mgr))                    # 用完必须释放
        launcher.stop.assert_called_once_with(SID)

    def test_whole_sequence_runs_under_the_session_lock(self):
        """stop 和 ensure_running 执行时，锁必须已经被 switch_model 持有。"""
        mgr, store, launcher = make_manager(session())
        mgr.sandbox_state = lambda s: {"busy": False, "queue": 0}
        seen = {}
        real_stop, real_ensure = mgr.stop, mgr.ensure_running

        def fake_stop(sid, **kw):
            seen["during_stop"] = lock_is_free(mgr)
            real_stop(sid, **kw)

        def fake_ensure(sid):
            seen["during_restart"] = lock_is_free(mgr)
            return real_ensure(sid)

        mgr.stop, mgr.ensure_running = fake_stop, fake_ensure
        mgr.switch_model(OWNER, SID, "new-ref")
        self.assertEqual(seen, {"during_stop": False, "during_restart": False})
        # 顺序：先写新模型，再用它重启（重启时 ensure_running 读到的必须是新值）
        idx_ref = [i for i, u in enumerate(store.updates) if u.get("model_ref")]
        idx_run = [i for i, u in enumerate(store.updates) if u.get("status") == "running"]
        self.assertEqual(len(idx_ref), 1)
        self.assertTrue(idx_run)
        self.assertLess(idx_ref[0], min(idx_run))
        self.assertEqual(store.s.model_ref, "new-ref")

    def test_stopped_session_switches_without_restarting(self):
        mgr, store, launcher = make_manager(session(status="stopped", endpoint=""))
        mgr.sandbox_state = lambda s: None                    # 没在跑，问不出状态
        mgr.ensure_running = lambda sid: self.fail("不该重启")
        out = mgr.switch_model(OWNER, SID, "new-ref")
        launcher.stop.assert_called_once_with(SID)
        self.assertEqual(out.model_ref, "new-ref")

    def test_lock_is_reentrant_so_stop_and_ensure_running_do_not_deadlock(self):
        mgr, _, _ = make_manager(session())
        lk = mgr._lock(SID)
        with lk:
            self.assertTrue(lk.acquire(blocking=False))       # RLock：同一线程能再拿
            lk.release()
        self.assertTrue(lock_is_free(mgr))

    def test_switch_model_uses_the_model_that_was_just_written(self):
        """回归 #61 的第二种失败：stop 之后、ensure_running 之前被别的请求抢先重启。"""
        mgr, store, launcher = make_manager(session())
        mgr.sandbox_state = lambda s: {"busy": False, "queue": 0}
        mgr.switch_model(OWNER, SID, "new-ref")
        # 整个序列持锁，所以 DB 里的 model_ref 在重启前就已经是新值
        self.assertLess(store.updates.index({"model_ref": "new-ref"}),
                        len(store.updates) - 1)


if __name__ == "__main__":
    unittest.main()

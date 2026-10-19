"""Web 面板多会话并行（#106、#5；方案见 docs/web-multisession-plan.md 一期）。

覆盖：并发上限（全局 / 每客户端 / 先来先得 / 可取消）、Session.spawn 的共享与隔离、
每个标签页各自的当前会话、并行运行、按会话取消、按会话的权限询问、按会话的事件流、
排队上限、/clear 换 id、/resume、淘汰、旧客户端兼容。
"""

import http.client
import json
import os
import threading
import time
import unittest

from helpers import Workspace

from nanocode.session import Options, Session
from nanocode.web.runtime import RunSlots
from nanocode.web.server import make_server


def wait_for(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class RunSlotsTest(unittest.TestCase):
    def test_global_cap_waits_until_release(self):
        slots = RunSlots(1, 0)
        self.assertTrue(slots.acquire("a", threading.Event()))
        got = []
        t = threading.Thread(target=lambda: got.append(slots.acquire("b", threading.Event())))
        t.start()
        time.sleep(0.3)
        self.assertEqual(got, [], "上限 1 时第二个不该拿到空位")
        slots.release("a")
        t.join(3)
        self.assertEqual(got, [True])
        self.assertEqual(slots.running, 1)

    def test_per_client_cap_does_not_block_other_clients(self):
        slots = RunSlots(3, 1)
        self.assertTrue(slots.acquire("a", threading.Event()))
        waiting = threading.Event()
        got = []
        t = threading.Thread(target=lambda: got.append(slots.acquire("a", threading.Event(),
                                                                     lambda pos: waiting.set())))
        t.start()
        self.assertTrue(waiting.wait(3), "同一客户端超过上限应当等待")
        # 排在后面的别的客户端不被前面卡住的等待者堵住
        self.assertTrue(slots.acquire("b", threading.Event()))
        slots.release("a")
        t.join(3)
        self.assertEqual(got, [True])

    def test_cancel_while_waiting_returns_false(self):
        slots = RunSlots(1, 0)
        slots.acquire("a", threading.Event())
        cancel = threading.Event()
        got = []
        t = threading.Thread(target=lambda: got.append(slots.acquire("b", cancel)))
        t.start()
        time.sleep(0.2)
        cancel.set()
        t.join(3)
        self.assertEqual(got, [False])
        self.assertEqual(slots.running, 1, "取消的等待者不能占名额")

    def test_waiters_are_served_in_order(self):
        slots = RunSlots(1, 0)
        slots.acquire("x", threading.Event())
        order = []
        threads = []
        for name in ("first", "second"):
            t = threading.Thread(target=lambda n=name: (slots.acquire(n, threading.Event()), order.append(n)))
            t.start()
            threads.append(t)
            time.sleep(0.15)
        slots.release("x")
        wait_for(lambda: order == ["first"], 3)
        slots.release("first")
        for t in threads:
            t.join(3)
        self.assertEqual(order, ["first", "second"])

    def test_zero_means_unlimited(self):
        slots = RunSlots(0, 0)
        for _ in range(20):
            self.assertTrue(slots.acquire("a", threading.Event()))


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.session = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)

    def tearDown(self):
        self.session.close()
        self.ws.cleanup()

    def test_spawn_shares_project_parts_but_not_conversation(self):
        child = self.session.spawn()
        self.assertIs(child.registry, self.session.registry)
        self.assertIs(child.provider, self.session.provider)
        self.assertIs(child.mcp_clients, self.session.mcp_clients)
        self.assertIsNot(child.bus, self.session.bus)
        self.assertIsNot(child.agent, self.session.agent)
        self.assertIsNot(child.permissions, self.session.permissions)
        # "总是允许"和权限模式互不影响
        child.permissions.session_allow.append("bash(ls)")
        child.permissions.set_mode("plan")
        self.assertEqual(self.session.permissions.session_allow, [])
        self.assertEqual(self.session.permissions.mode, "default")
        # /model 只换自己的
        child.set_model(model="other-model")
        self.assertNotEqual(self.session.provider.model, "other-model")

    def test_spawn_with_id_loads_without_writing_an_empty_file(self):
        self.session.agent.messages = [{"role": "user", "content": "hi"}]
        self.session.save()
        sid = self.session.session_id
        d = os.path.join(self.session.data_dir, "sessions")
        before = sorted(os.listdir(d))
        child = self.session.spawn(sid)
        self.assertEqual(child.session_id, sid)
        self.assertEqual(child.agent.messages, [{"role": "user", "content": "hi"}])
        self.assertEqual(sorted(os.listdir(d)), before)
        with self.assertRaises(OSError):
            self.session.spawn("no-such-session")

    def test_system_note_is_appended(self):
        child = self.session.spawn()
        child.system_note = lambda: "# 并行会话提醒"
        self.assertTrue(child.agent.system_prompt().endswith("# 并行会话提醒"))
        self.assertNotIn("# 并行会话提醒", self.session.agent.system_prompt())


class MultiSessionWebTest(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.ws = Workspace()
        self._old_env = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(self.ENV)
        self.session = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.app.disconnect_grace = 60
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.conns = []

    def tearDown(self):
        for rt in self.app.sessions.live():
            rt.cancel()
        for c in self.conns:
            c.sse_response.close()
            c.close()
        wait_for(lambda: not any(rt.busy for rt in self.app.sessions.live()), 5)
        self.server.shutdown()
        self.server.server_close()
        self.app.sessions.close()
        self.session.close()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.ws.cleanup()

    # ---- helpers ----
    def headers(self, sid=None):
        h = {"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token,
             "content-type": "application/json"}
        if sid:
            h["x-nanocode-session"] = sid
        return h

    def post(self, path, body=None, sid=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", path, body=json.dumps(body or {}), headers=self.headers(sid))
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        return r.status, data

    def get(self, path, sid=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers=self.headers(sid))
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        return r.status, data

    def open_sse(self, query=""):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/events" + query, headers=self.headers())
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        c.sse_response = r
        self.conns.append(c)
        return r

    def read_data(self, r, n):
        out = []
        while len(out) < n:
            line = r.fp.readline().decode().rstrip("\n")
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
        return out

    def new_session(self):
        status, r = self.post("/api/session", {"action": "new"}, sid=self.session.session_id)
        self.assertEqual(status, 200)
        self.assertTrue(r["ok"])
        return r["session_id"]

    def rt(self, sid):
        return self.app.sessions.get(sid)

    def start(self, sid, msg="帮我写个文件"):
        status, r = self.post("/api/chat", {"message": msg}, sid=sid)
        self.assertEqual(status, 200, r)
        self.assertTrue(r["ok"], r)
        return r

    def wait_prompt(self, sid):
        self.assertTrue(wait_for(lambda: len(self.rt(sid).pending) == 1), f"会话 {sid} 的权限询问没有出现")


class MultiSessionTest(MultiSessionWebTest):
    def test_new_session_in_one_tab_does_not_switch_others(self):
        """#106：一个标签页新建会话，不能把别的标签页（和默认会话）一起换掉。"""
        a = self.session.session_id
        b = self.new_session()
        self.assertNotEqual(a, b)
        self.assertEqual(self.get("/api/state")[1]["session_id"], a, "默认会话被换掉了")
        self.assertEqual(self.get("/api/state", sid=a)[1]["session_id"], a)
        self.assertEqual(self.get("/api/state", sid=b)[1]["session_id"], b)
        self.assertNotIn("session_switched", [e["type"] for e in self.session.bus.history])

    def test_two_sessions_run_in_parallel(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.start(b)
        self.wait_prompt(a)
        self.wait_prompt(b)
        self.assertTrue(self.rt(a).busy and self.rt(b).busy)

    def test_switching_while_running_is_allowed(self):
        a = self.session.session_id
        self.start(a)
        self.wait_prompt(a)
        status, r = self.post("/api/session", {"action": "new"}, sid=a)
        self.assertTrue(r["ok"])
        status, r = self.post("/api/session", {"action": "load", "id": a}, sid=r["session_id"])
        self.assertTrue(r["ok"])
        self.assertTrue(self.rt(a).busy, "切走再切回不能影响正在跑的会话")

    def test_cancel_only_stops_that_session(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.start(b)
        self.wait_prompt(a)
        self.wait_prompt(b)
        self.post("/api/cancel", {}, sid=a)
        self.assertTrue(wait_for(lambda: not self.rt(a).busy), "被取消的会话没停")
        self.assertTrue(self.rt(b).busy)
        self.assertEqual(len(self.rt(b).pending), 1)

    def test_cancel_all(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.start(b)
        self.wait_prompt(a)
        self.wait_prompt(b)
        self.post("/api/cancel", {"all": True}, sid=a)
        self.assertTrue(wait_for(lambda: not self.rt(a).busy and not self.rt(b).busy))

    def test_permission_answer_is_scoped_to_its_session(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.start(b)
        self.wait_prompt(a)
        self.wait_prompt(b)
        rid_b = next(iter(self.rt(b).pending))
        status, r = self.post("/api/permission", {"id": rid_b, "decision": "no"}, sid=a)
        self.assertEqual(status, 404, "拿着 A 的会话 id 不能回答 B 的询问")
        self.assertEqual(len(self.rt(b).pending), 1)
        status, r = self.post("/api/permission", {"id": rid_b, "decision": "no"}, sid=b)
        self.assertTrue(r["ok"])
        self.assertTrue(wait_for(lambda: not self.rt(b).busy))
        self.assertEqual(len(self.rt(a).pending), 1, "回答 B 不能影响 A")

    def test_legacy_permission_without_session_header_finds_any_session(self):
        b = self.new_session()
        self.start(b)
        self.wait_prompt(b)
        rid = next(iter(self.rt(b).pending))
        status, r = self.post("/api/permission", {"id": rid, "decision": "no"})
        self.assertTrue(r["ok"])
        self.assertTrue(wait_for(lambda: not self.rt(b).busy))

    def test_background_prompt_keeps_waiting_while_any_page_is_connected(self):
        """用户切去看别的会话时，后台会话的询问不能因为"没人看着它"被自动拒掉。"""
        self.app.disconnect_grace = 0.5
        a, b = self.session.session_id, self.new_session()
        self.open_sse(f"?sid={a}")              # 唯一的页面在看 A
        self.assertTrue(wait_for(lambda: self.app.sse_clients == 1))
        self.start(b)
        self.wait_prompt(b)
        time.sleep(1.5)
        self.assertEqual(len(self.rt(b).pending), 1, "有页面连着，后台会话的询问却被自动拒绝了")

    def test_mode_is_per_session(self):
        a, b = self.session.session_id, self.new_session()
        status, r = self.post("/api/chat", {"message": "/mode plan"}, sid=b)
        self.assertTrue(r["ok"])
        self.assertEqual(self.get("/api/state", sid=b)[1]["mode"], "plan")
        self.assertEqual(self.get("/api/state", sid=a)[1]["mode"], "default")

    def test_events_stream_is_per_session(self):
        a, b = self.session.session_id, self.new_session()
        r = self.open_sse(f"?sid={b}")
        self.rt(a).session.bus.emit("todos", todos=[{"content": "A", "status": "pending"}])
        self.rt(b).session.bus.emit("todos", todos=[{"content": "B", "status": "pending"}])
        events = self.read_data(r, 1)
        self.assertEqual(events[0]["todos"][0]["content"], "B")

    def test_status_broadcast_only_when_asked(self):
        b = self.new_session()
        r = self.open_sse("?status=1")
        first = self.read_data(r, 1)[0]
        self.assertEqual(first["type"], "sessions_status")
        self.assertNotIn("seq", first)
        self.start(b)
        self.wait_prompt(b)
        seen = []
        while not any(s["id"] == b and s["awaiting_permission"]
                      for e in seen if e["type"] == "sessions_status" for s in e["sessions"]):
            seen += self.read_data(r, 1)
        # 不要状态的旧连接收不到 sessions_status
        self.session.bus.emit("todos", todos=[])
        plain = self.open_sse()
        self.assertEqual(self.read_data(plain, 1)[0]["type"], "todos")

    def test_state_reports_parallel_sessions_and_agent_gets_a_note(self):
        a, b = self.session.session_id, self.new_session()
        self.start(b)
        self.wait_prompt(b)
        self.assertEqual(self.get("/api/state", sid=a)[1]["parallel"], 1)
        self.assertEqual(self.get("/api/state", sid=b)[1]["parallel"], 0)
        self.assertIn("并行会话提醒", self.rt(a).session.agent.system_prompt())
        self.assertNotIn("并行会话提醒", self.rt(b).session.agent.system_prompt())

    def test_unknown_and_invalid_session_ids(self):
        status, r = self.get("/api/state", sid="20000101-000000-000000")
        self.assertEqual((status, r["code"]), (404, "no_session"))
        status, r = self.get("/api/state", sid="../etc")
        self.assertEqual((status, r["code"]), (400, "bad_session"))

    def test_clear_returns_new_session_id(self):
        b = self.new_session()
        status, r = self.post("/api/chat", {"message": "/clear"}, sid=b)
        self.assertTrue(r["ok"])
        self.assertNotEqual(r["session_id"], b)
        self.assertIs(self.rt(r["session_id"]).session, self.app.sessions.get(r["session_id"]).session)
        self.assertNotIn(b, [rt.sid for rt in self.app.sessions.live()])

    def test_resume_command_points_to_session_list(self):
        a = self.session.session_id
        status, r = self.post("/api/chat", {"message": "/resume"}, sid=a)
        self.assertTrue(r["ok"])
        self.assertIn("会话列表", r["output"])
        self.assertEqual(r["session_id"], a)

    def test_sessions_list_marks_running_and_new(self):
        b = self.new_session()
        self.start(b)
        self.wait_prompt(b)
        status, r = self.get("/api/sessions", sid=b)
        item = next(s for s in r["sessions"] if s["id"] == b)
        self.assertTrue(item["running"])
        self.assertTrue(item["awaiting_permission"])
        self.assertEqual(r["current"], b)

    def test_idle_sessions_are_evicted_but_running_ones_stay(self):
        self.app.sessions.max_live = 2
        running = self.new_session()
        self.start(running)
        self.wait_prompt(running)
        idle = [self.new_session() for _ in range(3)]
        live = [rt.sid for rt in self.app.sessions.live()]
        self.assertIn(running, live)
        self.assertIn(self.session.session_id, live, "启动时的会话不能被淘汰")
        self.assertNotIn(idle[0], live)
        self.assertLessEqual(len(live), 3)


class ConcurrencyCapTest(MultiSessionWebTest):
    ENV = {"NANOCODE_WEB_MAX_RUNS": "1"}

    def test_global_cap_queues_until_a_slot_frees(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.wait_prompt(a)
        self.start(b)
        self.assertTrue(wait_for(lambda: self.rt(b).waiting_slot), "满了应当等空位")
        self.assertEqual(self.rt(b).pending, {})
        queued = [e for e in self.rt(b).session.bus.history if e["type"] == "queued"]
        self.assertEqual(queued[-1].get("reason"), "slots")
        self.post("/api/cancel", {}, sid=a)
        self.wait_prompt(b)

    def test_cancel_while_waiting_for_slot(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.wait_prompt(a)
        self.start(b)
        self.assertTrue(wait_for(lambda: self.rt(b).waiting_slot))
        self.post("/api/cancel", {}, sid=b)
        self.assertTrue(wait_for(lambda: not self.rt(b).busy), "等空位时取消没生效")
        self.assertTrue(self.rt(a).busy)


class PerClientCapTest(MultiSessionWebTest):
    ENV = {"NANOCODE_WEB_MAX_RUNS": "3", "NANOCODE_WEB_MAX_RUNS_PER_CLIENT": "1"}

    def test_same_client_is_limited(self):
        a, b = self.session.session_id, self.new_session()
        self.start(a)
        self.wait_prompt(a)
        self.start(b)
        self.assertTrue(wait_for(lambda: self.rt(b).waiting_slot), "同一客户端超过上限应当等空位")


class QueueCapTest(MultiSessionWebTest):
    ENV = {"NANOCODE_WEB_MAX_QUEUE": "1", "NANOCODE_WEB_MAX_QUEUE_TOTAL": "1", "NANOCODE_WEB_RATE_BURST": "0"}

    def test_total_queue_cap_across_sessions(self):
        a, b = self.session.session_id, self.new_session()
        for sid in (a, b):
            self.start(sid)
            self.wait_prompt(sid)
        self.assertTrue(self.start(a, "排一条")["queued"])
        status, r = self.post("/api/chat", {"message": "A 再排一条"}, sid=a)
        self.assertEqual((status, r["code"]), (429, "queue_full"))
        status, r = self.post("/api/chat", {"message": "B 也排一条"}, sid=b)
        self.assertEqual((status, r["code"]), (429, "queue_full"), "总排队上限没有生效")
        self.assertIn("总数", r["error"])


if __name__ == "__main__":
    unittest.main()

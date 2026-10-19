"""刷新 / 重启后恢复聊天记录：/api/messages 与"从 messages 折回的时间线"（#86）。

时间线本来只由事件流（llm_request / llm_response）实时画出来，而事件缓冲区在内存里
（events.py 的 EventHistory deque），进程一重启就空了。messages 是唯一落盘的东西，
所以这里校验两件事：
  1. /api/messages 把 messages、事件缓冲区的最新序号一起给前端；
  2. 事件补不出来的时候，能从 messages 折出最后一轮的步骤做兜底。
"""

import http.client
import json
import os
import threading
import time
import unittest

from helpers import Workspace

from nanocode.messages import (assistant_message, steps_of_turn, text_block, tool_result_block,
                               tool_use_block, user_message)
from nanocode.session import Options, Session
from nanocode.web.server import make_server


class TimelineFromMessagesTest(unittest.TestCase):
    def test_text_only_turn_is_one_finished_step(self):
        messages = [user_message("你好"), assistant_message([text_block("你好，我是 nanocode")])]
        steps = steps_of_turn(messages)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step"], 1)
        self.assertEqual(steps[0]["calls"], [])
        self.assertEqual(steps[0]["stop"], "end_turn")
        self.assertTrue(steps[0]["rebuilt"])      # 标明是折出来的，没有精确的 token / 耗时

    def test_tool_round_trip_is_two_steps(self):
        messages = [
            user_message("看看目录"),
            assistant_message([tool_use_block("t1", "bash", {"command": "ls"})]),
            {"role": "user", "content": [tool_result_block("t1", "a.py")]},
            assistant_message([text_block("只有一个文件")]),
        ]
        steps = steps_of_turn(messages)
        self.assertEqual([s["step"] for s in steps], [1, 2])
        self.assertEqual([s["calls"] for s in steps], [["bash"], []])
        self.assertEqual([s["stop"] for s in steps], ["tool_use", "end_turn"])

    def test_only_the_last_turn_is_rebuilt(self):
        """时间线的键是「agent+step」，跨轮会互相覆盖 —— 所以只还原最后一轮，和实时行为一致。"""
        messages = [
            user_message("第一轮"),
            assistant_message([text_block("回答一")]),
            user_message("第二轮"),
            assistant_message([tool_use_block("t2", "read_file", {"path": "a.py"})]),
        ]
        steps = steps_of_turn(messages)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["calls"], ["read_file"])

    def test_tool_result_user_message_does_not_start_a_turn(self):
        """工具结果是 user 角色，但它不是人说的话；把它当成轮次起点会少掉前面的步骤。"""
        messages = [
            user_message("读一下 a.py"),
            assistant_message([tool_use_block("t1", "read_file", {"path": "a.py"})]),
            {"role": "user", "content": [tool_result_block("t1", "print(1)")]},
            assistant_message([tool_use_block("t2", "edit_file", {"path": "a.py"})]),
        ]
        steps = steps_of_turn(messages)
        self.assertEqual([s["step"] for s in steps], [1, 2])

    def test_no_messages_means_no_steps(self):
        self.assertEqual(steps_of_turn([]), [])
        self.assertEqual(steps_of_turn([{"role": "user", "content": []}]), [])


class MessagesEndpointTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.session = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.session.agent.cancel.set()
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        self.ws.cleanup()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token})
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        return data

    def read_events(self, path, n, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "x-nanocode-token": self.app.token})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        events = []
        while len(events) < n:
            line = r.fp.readline().decode().rstrip("\n")
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        conn.close()
        return events

    def feed(self, delay=0.15, **event):
        def run():
            time.sleep(delay)
            self.session.bus.emit(**event)
        threading.Thread(target=run, daemon=True).start()

    # 服务重启后事件序号从 0 重来，浏览器手里还攥着上个进程的序号（#86）
    def test_reconnect_after_restart_is_not_filtered_forever(self):
        self.feed(type="todos", todos=[])
        events = self.read_events("/api/events?after=999999", 1)
        self.assertEqual(events[0]["type"], "todos")       # 序号比服务端还大 → 当作新连接，从头补发

    def test_reconnect_before_restart_still_skips_what_was_seen(self):
        self.session.bus.emit("todos", todos=[])
        seen = self.session.bus.history[-1]["seq"]
        self.feed(type="todos", todos=[{"content": "x", "status": "pending"}])
        events = self.read_events(f"/api/events?after={seen}", 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["todos"], [{"content": "x", "status": "pending"}])

    def test_returns_messages_last_seq_and_rebuilt_timeline(self):
        self.session.agent.messages = [
            user_message("看一下目录"),
            assistant_message([tool_use_block("t1", "bash", {"command": "ls"})]),
            {"role": "user", "content": [tool_result_block("t1", "a.py")]},
            assistant_message([text_block("只有一个文件 a.py")]),
        ]
        self.session.bus.emit("user", text="看一下目录")
        self.session.bus.emit("tool_call", id="t1", name="bash", input={"command": "ls"})
        last_seq = self.session.bus.history[-1]["seq"]

        data = self.get("/api/messages")
        self.assertTrue(data["ok"])
        self.assertEqual(data["messages"], self.session.agent.messages)
        self.assertEqual(data["last_seq"], last_seq)
        self.assertEqual([s["calls"] for s in data["timeline"]], [["bash"], []])

    def test_fresh_session_has_zero_seq_and_no_timeline(self):
        data = self.get("/api/messages")
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["last_seq"], 0)      # 前端据此知道"没有历史事件要补"
        self.assertEqual(data["timeline"], [])

    def test_requires_token(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/messages", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        r.read()
        c.close()

    def test_static_page_loads_history_before_connecting_events(self):
        """前端顺序不能反：先 loadHistory 拿到 messages 和 historySeq，再连 SSE 收新事件。"""
        with open(os.path.join(os.path.dirname(__file__), "..", "nanocode", "web", "static", "index.html"),
                  encoding="utf-8") as f:
            page = f.read()
        self.assertIn("let historyLoaded = false", page)   # 断线重连不能再拉一遍历史（会重复渲染）
        body = page[page.index("async function connectEvents"):]
        self.assertLess(body.index("await loadHistory()"), body.index("new EventSource"))


if __name__ == "__main__":
    unittest.main()

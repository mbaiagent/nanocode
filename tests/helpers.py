"""测试辅助：脚本化的假模型、临时工作目录、假的 SSE HTTP 服务器。"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 测试不能读写真实的 ~/.nanocode（个人配置、信任记录、会话记录）
os.environ["NANOCODE_HOME"] = tempfile.mkdtemp(prefix="nanocode-home-")

from nanocode.agent import Agent
from nanocode.events import EventBus
from nanocode.hooks import HookRunner
from nanocode.llm.base import Provider
from nanocode.messages import LLMResponse, Usage, text_block, tool_use_block
from nanocode.permissions import PermissionManager
from nanocode.tools import ToolRegistry, default_tools


class ScriptedProvider(Provider):
    """按顺序返回预先写好的回复，并记录每次收到的请求。"""

    name = "scripted"

    def __init__(self, replies):
        super().__init__("scripted")
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages, tools, max_tokens=4096, on_text=None):
        self.calls.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools})
        reply = self.replies.pop(0) if self.replies else [text_block("完成")]
        if callable(reply):
            reply = reply(messages, tools)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in reply) else "end_turn"
        return LLMResponse(reply, stop, Usage(10, 5))


def call(name, **input):
    call.n = getattr(call, "n", 0) + 1
    return tool_use_block(f"t{call.n}", name, input)


class Workspace:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="nanocode-test-")

    def write(self, rel, content):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def read(self, rel):
        with open(os.path.join(self.dir, rel), encoding="utf-8") as f:
            return f.read()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def py(code: str) -> str:
    """一条跨平台能跑的 shell 命令：Windows 上没有 `ls` / `sleep` / `python3`。

    只在同一份测试要在 Windows 上跑时才需要它（#45）。code 里请用单引号，
    外面这层是双引号 —— cmd 和 bash 都认双引号，认单引号的只有 bash。
    """
    return f'"{sys.executable}" -c "{code}"'


def make_agent(ws, replies, mode="bypass", hooks=None, ask=None, **kw):
    provider = ScriptedProvider(replies)
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    perms = PermissionManager(ws.dir, mode=mode, ask_fn=ask)
    agent = Agent(provider, ToolRegistry(default_tools()), perms, HookRunner(hooks, ws.dir), bus, ws.dir,
                  system_prompt=lambda: "测试系统提示词", **kw)
    return agent, provider, events


class FakeSSEServer:
    """一个返回固定 SSE 流的 HTTP 服务器，用来测试 provider 的协议解析。"""

    def __init__(self, sse_text, status=200):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["content-length"])
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "body": json.loads(self.rfile.read(n))})
                body = sse_text.encode()
                self.send_response(status)
                self.send_header("content-type", "text/event-stream" if status == 200 else "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

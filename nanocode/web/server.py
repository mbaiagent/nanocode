"""本地 Web 界面：聊天 + Agent 内部状态调试面板。

只用标准库 http.server 实现。Web 界面同样只是事件总线的订阅者：
浏览器通过 SSE（/api/events）实时接收事件，看到的内容和 CLI 完全一样，
只是多了"透视"能力 —— 可以看到每一步发给模型的完整 messages、系统提示词、工具定义。

接口：
    GET  /                  聊天 + 调试面板
    GET  /learn             交互式架构学习页
    GET  /api/state         会话概况
    GET  /api/context       完整上下文：system、messages、tools（就是下一次要发给模型的东西）
    GET  /api/events        SSE 事件流（连接时先补发历史事件）
    POST /api/chat          {"message": "..."} 发送消息（也支持 /命令 和 # 记忆）
    POST /api/permission    {"id": "...", "decision": "yes|always|no"}
    POST /api/cancel        中断当前任务
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..session import Session

STATIC = os.path.join(os.path.dirname(__file__), "static")


class WebApp:
    def __init__(self, session: Session):
        self.session = session
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()
        session.permissions.ask_fn = self.ask

    def ask(self, name: str, input: dict, target: str) -> str:
        """权限询问：发事件给浏览器，然后阻塞等待 /api/permission 的回答。"""
        rid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "answer": "no"}
        self.pending[rid] = slot
        self.session.bus.emit("permission_request", id=rid, name=name, input=input, target=target)
        while not slot["event"].wait(0.2):
            if self.session.agent.cancel.is_set():
                break
        self.pending.pop(rid, None)
        self.session.bus.emit("permission_answer", id=rid, decision=slot["answer"])
        return slot["answer"]

    def chat(self, message: str) -> dict:
        s = self.session
        with self.lock:
            if s.agent.busy:
                return {"ok": False, "error": "Agent 正在工作，请等它完成或先中断"}
            message = message.strip()
            if message.startswith("#"):
                return {"ok": True, "output": s.add_memory(message.lstrip("#"))}
            prompt = message
            if message.startswith("/"):
                output, prompt = s.handle_command(message)
                s.bus.emit("command", input=message, output=output)
                if not prompt:
                    return {"ok": True, "output": output}
            s.agent.busy = True  # 先占住，避免两个请求同时启动
        threading.Thread(target=self._run, args=(prompt,), daemon=True).start()
        return {"ok": True}

    def _run(self, prompt: str) -> None:
        try:
            self.session.send(prompt)
        except Exception as e:  # noqa: BLE001
            self.session.bus.emit("error", agent="main", message=f"{type(e).__name__}: {e}")
            self.session.agent.busy = False


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 安静一点
            pass

        def _json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, name):
            path = os.path.join(STATIC, name)
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            s = app.session
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._file("index.html")
            if path in ("/learn", "/learn.html"):
                return self._file("learn.html")
            if path == "/api/state":
                return self._json(s.info())
            if path == "/api/context":
                return self._json({"system": s.agent.system_prompt(), "messages": s.agent.messages,
                                   "tools": s.registry.schemas(), "permissions": {
                                       "mode": s.permissions.mode, "allow": s.permissions.allow + s.permissions.session_allow,
                                       "deny": s.permissions.deny}})
            if path == "/api/events":
                return self._sse()
            self.send_error(404)

        def do_POST(self):
            path = self.path.split("?")[0]
            data = self._body()
            if path == "/api/chat":
                return self._json(app.chat(str(data.get("message", ""))))
            if path == "/api/permission":
                slot = app.pending.get(data.get("id", ""))
                if not slot:
                    return self._json({"ok": False, "error": "请求不存在或已过期"}, 404)
                slot["answer"] = data.get("decision") if data.get("decision") in ("yes", "always", "no") else "no"
                slot["event"].set()
                return self._json({"ok": True})
            if path == "/api/cancel":
                app.session.agent.cancel.set()
                return self._json({"ok": True})
            self.send_error(404)

        def _sse(self):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            unsubscribe = app.session.bus.subscribe(q.put)
            try:
                for e in list(app.session.bus.history):  # 补发历史，刷新页面不丢状态
                    self._send_event(e)
                while True:
                    try:
                        e = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._send_event(e)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                unsubscribe()

        def _send_event(self, e):
            data = json.dumps(e, ensure_ascii=False, default=str)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = WebApp(session)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    print(f"nanocode Web 界面：http://{host}:{port}   （学习页：http://{host}:{port}/learn）")
    print(f"模型 {session.provider_name}/{session.provider.model} · 工作目录 {session.cwd} · Ctrl-C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        session.close()

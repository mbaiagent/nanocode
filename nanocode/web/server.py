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

安全：所有请求校验 Host / Origin，/api/* 需要令牌（页面加载时注入），POST 只接受 JSON。
详见 Handler._guard 的说明。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..session import Session

STATIC = os.path.join(os.path.dirname(__file__), "static")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class WebApp:
    def __init__(self, session: Session, permission_timeout: float | None = None, disconnect_grace: float = 30):
        self.session = session
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(24)
        if permission_timeout is None:
            permission_timeout = float(os.environ.get("NANOCODE_PERMISSION_TIMEOUT") or 600)
        self.permission_timeout = permission_timeout
        # 权限询问期间如果一个浏览器都没连着（页面关了），宽限这么多秒后按拒绝处理
        self.disconnect_grace = disconnect_grace
        self.sse_clients = 0
        self._sse_lock = threading.Lock()
        # 事件流空闲时每隔这么多秒发一次心跳；服务端也是靠写心跳才发现浏览器已断开的
        self.sse_ping_interval = 15
        self.allowed_hosts: set[str] = set(LOCAL_HOSTS)
        self.allowed_origins: set[str] = set()
        session.permissions.ask_fn = self.ask

    def configure(self, host: str, port: int) -> None:
        """绑定端口后调用：确定哪些 Host / Origin 是"自己人"。"""
        hosts = set(LOCAL_HOSTS)
        if host not in ("0.0.0.0", "::", ""):
            hosts.add(host.lower())
        self.allowed_hosts = hosts
        self.allowed_origins = {f"http://{h}:{port}" for h in hosts} | {f"http://[{h}]:{port}" for h in hosts if ":" in h}

    def ask(self, name: str, input: dict, target: str) -> str:
        """权限询问：发事件给浏览器，然后阻塞等待 /api/permission 的回答。

        按拒绝处理的三种情况，Agent 都不会永远卡住（等待期间 Agent 占着全局 busy 锁，别的请求都会被拒）：
          - 用户点了中断
          - 超过 permission_timeout（默认 10 分钟，环境变量 NANOCODE_PERMISSION_TIMEOUT 可改）
          - 没有任何浏览器连着（页面关了）超过 disconnect_grace 秒。刷新页面会很快重连，不受影响。
        """
        rid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "answer": "no"}
        self.pending[rid] = slot
        self.session.bus.emit("permission_request", id=rid, name=name, input=input, target=target)
        deadline = time.time() + self.permission_timeout
        no_client_since = None
        while not slot["event"].wait(0.2):
            now = time.time()
            if self.session.agent.cancel.is_set() or now > deadline:
                break
            if self.sse_clients == 0:
                no_client_since = no_client_since or now
                if now - no_client_since > self.disconnect_grace:
                    break
            else:
                no_client_since = None
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
                body = f.read().replace(b"__NANOCODE_TOKEN__", app.token.encode())
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _reject(self, status: int, reason: str) -> None:
            self._json({"ok": False, "error": reason}, status)

        def _guard(self, path: str, method: str) -> bool:
            """所有请求先过这道检查，返回 False 表示已经拒绝。

            本地 Web 服务的三种典型攻击和对应防线：
              1. DNS 重绑定：恶意域名解析到 127.0.0.1，浏览器认为是"同源"，能读到页面里的令牌。
                 → 校验 Host 头必须是本机地址。所有请求都要校验，包括首页。
              2. 跨站请求伪造（CSRF）：恶意网页直接 POST 到 127.0.0.1。浏览器对 text/plain 这类
                 "简单请求"不做预检，请求会照常发出。
                 → POST 只接受 application/json（跨站发 JSON 必须先预检，而我们不响应预检）；
                   Origin 头存在时必须是本服务自己。
              3. 其他网页或本机程序猜接口。→ /api/* 必须带上启动时随机生成的令牌。
            """
            host = (self.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in app.allowed_hosts:
                self._reject(403, "Host 不被允许（防 DNS 重绑定）")
                return False
            origin = self.headers.get("origin")
            if origin and origin.rstrip("/") not in app.allowed_origins:
                self._reject(403, "Origin 不被允许（防跨站请求）")
                return False
            if not path.startswith("/api/"):
                return True
            if method == "POST" and (self.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                self._reject(415, "只接受 application/json")
                return False
            query = parse_qs(urlparse(self.path).query)
            token = self.headers.get("x-nanocode-token") or (query.get("token") or [""])[0]
            if not hmac.compare_digest(token, app.token):
                self._reject(403, "缺少或错误的令牌")
                return False
            return True

        def do_GET(self):
            s = app.session
            path = self.path.split("?")[0]
            if not self._guard(path, "GET"):
                return
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
            if not self._guard(path, "POST"):
                return
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
            with app._sse_lock:
                app.sse_clients += 1
            try:
                for e in list(app.session.bus.history):  # 补发历史，刷新页面不丢状态
                    self._send_event(e)
                while True:
                    try:
                        e = q.get(timeout=app.sse_ping_interval)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._send_event(e)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                unsubscribe()
                with app._sse_lock:
                    app.sse_clients -= 1

        def _send_event(self, e):
            data = json.dumps(e, ensure_ascii=False, default=str)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


def make_server(session: Session, host: str = "127.0.0.1", port: int = 8765) -> tuple[ThreadingHTTPServer, WebApp]:
    app = WebApp(session)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.configure(host, server.server_address[1])
    return server, app


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765) -> None:
    server, app = make_server(session, host, port)
    port = server.server_address[1]
    if host in ("0.0.0.0", "::"):
        print("警告：服务监听在所有网卡上，局域网内的人只要拿到页面就能操作 Agent。除非你清楚后果，请用默认的 127.0.0.1。")
    print(f"nanocode Web 界面：http://{host}:{port}   （学习页：http://{host}:{port}/learn）")
    print(f"模型 {session.provider_name}/{session.provider.model} · 工作目录 {session.cwd} · Ctrl-C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        session.close()

"""一个很小的 HTTP 框架（基于标准库 http.server）：路由、请求、响应、流式输出。

控制面和网关都建在它上面，业务代码只写"这个路径做什么"：

    app = App()

    @app.route("GET", "/api/sessions/{sid}")
    def show(req):
        return Response.json({"id": req.params["sid"]})

路径参数写成 {名字}（匹配一段）或 {名字:path}（匹配剩下的所有内容）。
处理函数抛 HttpError 会变成 JSON 错误响应；返回的 Response 可以是一次性的字节，也可以是流（事件流）。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlparse

SECURITY_HEADERS = [("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer")]


class HttpError(Exception):
    def __init__(self, status: int, message: str, headers: list | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers or []


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: object                      # http.client.HTTPMessage：不区分大小写的 get
    body: bytes
    client_ip: str
    raw_query: str = ""
    params: dict = field(default_factory=dict)
    user: str | None = None              # 认证中间件填写

    @property
    def cookies(self) -> dict:
        c = SimpleCookie()
        try:
            c.load(self.headers.get("cookie") or "")
        except Exception:  # noqa: BLE001 —— 格式错误的 Cookie 当作没有
            return {}
        return {k: m.value for k, m in c.items()}

    @property
    def is_json(self) -> bool:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower() == "application/json"

    def json(self) -> dict:
        if not self.is_json:
            raise HttpError(415, "只接受 application/json")
        try:
            data = json.loads(self.body or b"{}")
        except ValueError:
            raise HttpError(400, "请求体不是合法的 JSON") from None
        if not isinstance(data, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        return data

    def arg(self, name: str, default: str = "") -> str:
        return (self.query.get(name) or [default])[0]


@dataclass
class Response:
    status: int = 200
    body: bytes | Iterable[bytes] = b""
    headers: list = field(default_factory=list)
    stream: bool = False                 # True：body 是迭代器，逐块写出并刷新（事件流）

    @classmethod
    def json(cls, data, status: int = 200, headers: list | None = None) -> "Response":
        return cls(status, json.dumps(data, ensure_ascii=False).encode(),
                   [("content-type", "application/json; charset=utf-8"), *(headers or [])])

    @classmethod
    def error(cls, status: int, message: str, headers: list | None = None) -> "Response":
        return cls.json({"ok": False, "error": message}, status, headers)

    @classmethod
    def html(cls, body: bytes, status: int = 200) -> "Response":
        return cls(status, body, [("content-type", "text/html; charset=utf-8"), ("cache-control", "no-store")])

    @classmethod
    def redirect(cls, location: str, headers: list | None = None) -> "Response":
        return cls(302, b"", [("location", location), *(headers or [])])

    @classmethod
    def file(cls, path: str, cache: str = "no-cache") -> "Response":
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            raise HttpError(404, "文件不存在") from None
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        return cls(200, data, [("content-type", ctype), ("cache-control", cache)])


Handler = Callable[[Request], Response]


class App:
    max_body = 1024 * 1024

    def __init__(self):
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def route(self, method: str, pattern: str):
        regex = "^" + re.sub(r"\{(\w+)(?::path)\}", r"(?P<\1>.*)",
                             re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)) + "$"

        def decorator(fn: Handler) -> Handler:
            self._routes.append((method, re.compile(regex), fn))
            return fn
        return decorator

    def dispatch(self, req: Request) -> Response:
        allowed = False
        for method, regex, fn in self._routes:
            m = regex.match(req.path)
            if not m:
                continue
            allowed = True
            if method == req.method:
                req.params = m.groupdict()
                return fn(req)
        raise HttpError(405 if allowed else 404, "方法不允许" if allowed else "没有这个地址")

    def handle(self, req: Request) -> Response:
        """子类可以覆盖：在 dispatch 前后加认证之类的逻辑。"""
        return self.dispatch(req)

    def __call__(self, req: Request) -> Response:
        try:
            return self.handle(req)
        except HttpError as e:
            return Response.error(e.status, str(e), e.headers)


def static_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class _Server(ThreadingHTTPServer):
    # 标准库默认的监听队列只有 5：一下子来几十个连接（比如几十个用户同时点"新建会话"），
    # 队列满了内核会丢弃 SYN，客户端要等 1 秒重试，或者直接被重置。压测时就是这样发现的。
    request_queue_size = 256


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class RequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"          # 写完就关连接：流式响应不需要分块编码
        server_version = "nanocode-server"

        def log_message(self, *args):
            pass

        def _serve(self, method: str) -> None:
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > app.max_body:
                self.close_connection = True
                return self._write(Response.error(413, "请求体过大"))
            body = self.rfile.read(n) if n else b""
            req = Request(method, u.path, parse_qs(u.query), self.headers, body, self.client_address[0], u.query)
            self._write(app(req))

        def _write(self, resp: Response) -> None:
            try:
                self.send_response(resp.status)
                for k, v in [*SECURITY_HEADERS, *resp.headers]:
                    self.send_header(k, v)
                if not resp.stream:
                    self.send_header("content-length", str(len(resp.body)))
                self.end_headers()
                if not resp.stream:
                    self.wfile.write(resp.body)
                    return
                for chunk in resp.body:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                           # 对方断开了
            finally:
                close = getattr(resp.body, "close", None)
                if resp.stream and close:
                    close()                    # 让流的生成器执行 finally，关掉上游连接

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

    server = _Server((host, port), RequestHandler)
    server.daemon_threads = True
    return server


__all__ = ["App", "Request", "Response", "HttpError", "make_server", "static_dir"]

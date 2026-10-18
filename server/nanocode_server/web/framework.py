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
import socket
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlparse

SECURITY_HEADERS = [("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer")]

# 读请求阶段的 socket 超时和整个请求（请求行 + 头 + 体）的总时限（#55）。
# 沙箱被网络策略允许访问网关：一个租户开几千个只发半个请求的连接，就能把
# ThreadingHTTPServer 的线程占光，所有用户的模型请求一起失败。socket 超时挡
# 「连上不发数据」，总时限挡「每次只发几个字节」的慢速客户端。响应阶段会解除
# 超时：模型流式响应 / SSE 可能几分钟没有一个字节，慢消费者不能误杀。
REQUEST_TIMEOUT = 15
READ_DEADLINE = 60


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
    rid: str = ""                        # 请求编号：出错时客户端和服务端日志能对上（#60）

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
        except Exception:
            # 业务代码没接住的异常（Docker/K8s 出错、解密失败、连接池忙……）：
            # 必须回一个 5xx JSON，不能直接把连接掐了（#60）。否则客户端看到的是
            # RemoteDisconnected，分不清是平台故障还是自己网络断了，也无法重试。
            print(f"[nanocode] 请求 {req.rid} {req.method} {req.path} 未处理异常：", file=sys.stderr)
            traceback.print_exc()
            return Response.error(500, f"服务器内部错误（请求号 {req.rid or '-'}），请稍后重试；"
                                       "反复出现请把请求号告诉管理员")


def static_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class _Server(ThreadingHTTPServer):
    # 标准库默认的监听队列只有 5：一下子来几十个连接（比如几十个用户同时点"新建会话"），
    # 队列满了内核会丢弃 SYN，客户端要等 1 秒重试，或者直接被重置。压测时就是这样发现的。
    request_queue_size = 256

    def __init__(self, *args, max_connections: int = 256, **kwargs):
        super().__init__(*args, **kwargs)
        # 并发连接上限（#55）：ThreadingHTTPServer 每个连接起一个线程、没有上限，
        # 半打开的连接 + 16MB 请求体就能耗尽线程和内存。放不下的连接直接回 503，
        # 不起线程；线程结束（无论正常还是异常）都把名额还回去。
        self._slots = threading.BoundedSemaphore(max_connections) if max_connections > 0 else None

    def process_request(self, request, client_address):
        if self._slots is not None and not self._slots.acquire(blocking=False):
            return self._reject(request)
        try:
            super().process_request(request, client_address)
        except Exception:                        # 起线程失败（fd/线程耗尽）也要还名额
            if self._slots is not None:
                self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self._slots is not None:
                self._slots.release()

    @staticmethod
    def _reject(request):
        try:
            request.settimeout(5)                # 别让回 503 本身阻塞住 accept 循环
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\ncontent-length: 0\r\n"
                            b"retry-after: 1\r\nconnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            request.close()


def make_server(app: App, host: str, port: int, max_connections: int = 256) -> ThreadingHTTPServer:
    class RequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"          # 写完就关连接：流式响应不需要分块编码
        server_version = "nanocode-server"
        timeout = REQUEST_TIMEOUT              # 读请求的阶段：空闲/龟速连接不能占住线程（#55）
        read_deadline = READ_DEADLINE          # 整个请求的总时限（头 + 体）

        def log_message(self, *args):
            pass

        def handle_one_request(self):
            # 从读请求行开始计时：总时限要覆盖慢速地把头和体一点点吐出来的客户端，
            # socket timeout 只限制单次 recv，挡不住每次只发一个字节的攻击。
            self._deadline = time.monotonic() + self.read_deadline
            super().handle_one_request()

        def _serve(self, method: str) -> None:
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > app.max_body:
                self.close_connection = True
                return self._write(Response.error(413, "请求体过大"))
            body = self._read_body(n)
            if body is None:                   # 请求体发得太慢，总时限用完：断掉
                self.close_connection = True
                return self._write(Response.error(408, "请求体发送太慢，连接已关闭"))
            req = Request(method, u.path, parse_qs(u.query), self.headers, body, self.client_address[0], u.query,
                          rid=uuid.uuid4().hex[:8])
            # 响应阶段解除 socket 超时：流式响应 / SSE 可以几分钟没有一个字节，
            # 慢消费者不该被误杀（连接数由 _Server 的信号量兜底）。
            self.connection.settimeout(None)
            self._write(app(req))

        def _read_body(self, n: int) -> bytes | None:
            """按声明的长度读请求体；超时或对方断开返回 None。

            不能一口气 rfile.read(n)：那会阻塞到收满 n 字节，每秒只发几个字节的客户端
            就能永远占住一个线程（#55）。也不能用 select 等 socket 可读：请求体很可能
            在读请求头时已经被 rfile 的缓冲吞进去了，socket 上反而没有数据。read1 每次
            最多做一次 recv（被 socket 超时兜底），间隙里检查总时限，两种情况都对。
            """
            if n <= 0:
                return b""
            chunks, got = [], 0
            while got < n:
                if self._deadline - time.monotonic() <= 0:
                    return None
                try:
                    chunk = self.rfile.read1(min(n - got, 65536))
                except (socket.timeout, OSError):
                    return None                     # 读得太慢或对方断开
                if not chunk:
                    return None
                chunks.append(chunk)
                got += len(chunk)
            return b"".join(chunks)

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
                try:
                    for chunk in resp.body:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    # 对方断开了。Windows 上报的是 ConnectionAbortedError（WinError 10053/10054），
                    # 不在 Linux 常见的 BrokenPipe / ConnectionReset 里：压测时每个断连刷一条
                    # traceback，真正的错误反而被淹没（#70）。
                    pass
                except Exception:
                    # 流中途出错（比如记账时代理挂了）：连接上已经写出去一部分，变不成 5xx 了，
                    # 至少留下现场，别只靠 socketserver 那句没上下文的 traceback（#60）。
                    traceback.print_exc()
            except OSError:
                pass                           # 写响应头时对方就已经断开（同上，#70）
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

    server = _Server((host, port), RequestHandler, max_connections=max_connections)
    server.daemon_threads = True
    return server


__all__ = ["App", "Request", "Response", "HttpError", "make_server", "static_dir"]

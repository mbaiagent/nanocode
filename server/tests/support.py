"""测试辅助：import 路径、临时配置、可编程的假 HTTP 服务器、真实的平台实例。

可选组件的测试由环境变量打开，没有就跳过：
    NCS_TEST_DATABASE_URL=postgresql://...   在 PostgreSQL 上再跑一遍存储测试
    NCS_TEST_REDIS_URL=redis://...           Redis 限流测试
    NCS_TEST_DOCKER=1                        真的启动 Docker 沙箱（需要已构建 nanocode-sandbox 镜像）
"""

import http.client
import json
import os
import socketserver
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(SERVER_DIR)
for p in (SERVER_DIR, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NANOCODE_HOME", tempfile.mkdtemp(prefix="ncs-test-home-"))

from nanocode_server.config import Config  # noqa: E402

SECRET = "s" * 48
PG_URL = os.environ.get("NCS_TEST_DATABASE_URL", "")
REDIS_URL = os.environ.get("NCS_TEST_REDIS_URL", "")
DOCKER = os.environ.get("NCS_TEST_DOCKER") == "1"


class LocalHTTPServer(ThreadingHTTPServer):
    """测试用的本机 HTTP 服务器：绑定时不做反向 DNS。

    标准库 HTTPServer.server_bind() 会调 socket.getfqdn() 反查绑定地址；GitHub 的 macOS
    runner 上这一步能卡几十秒（#92，生产代码在 #97 已修）。测试服务器用不到 server_name。
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def make_config(**kw) -> Config:
    cfg = Config(data_dir=tempfile.mkdtemp(prefix="ncs-test-"), secret_key=SECRET, port=0, gateway_port=0,
                 launcher="local")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class FakeServer:
    """可编程的假 HTTP 服务器：handler(method, path, headers, body) -> (状态, 响应头, 响应体或分块列表)。"""

    def __init__(self, handler):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _handle(self, method):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                outer.requests.append({"method": method, "path": self.path, "headers": dict(self.headers), "body": body})
                status, headers, payload = handler(method, self.path, self.headers, body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                for chunk in (payload if isinstance(payload, list) else [payload]):
                    self.wfile.write(chunk)
                    self.wfile.flush()

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = LocalHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Client:
    """以某个用户的身份访问控制面（开发模式登录，或代理模式的请求头）。"""

    def __init__(self, port: int, user: str | None = None, proxy_secret: str = "", auth_header: str = "X-Auth-Request-Email"):
        self.port = port
        self.user = user
        self.proxy_secret = proxy_secret
        self.auth_header = auth_header
        self.cookie = ""
        if user and not proxy_secret:
            status, data, _ = self.request("POST", "/api/login", {"user": user})
            assert status == 200, data

    def request(self, method, path, body=None, headers=None, raw=False):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h["content-type"] = "application/json"
        if self.cookie:
            h["cookie"] = self.cookie
        if self.proxy_secret:
            h["X-NCS-Proxy-Secret"] = self.proxy_secret
            if self.user:
                h[self.auth_header] = self.user
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        headers_out = {k.lower(): v for k, v in r.getheaders()}     # 响应头名不区分大小写，统一成小写
        conn.close()
        if headers_out.get("set-cookie", "").startswith("ncs_login="):
            self.cookie = headers_out["set-cookie"].split(";", 1)[0]
        if raw:
            return r.status, data, headers_out
        try:
            return r.status, json.loads(data), headers_out
        except ValueError:
            return r.status, data.decode("utf-8", "replace"), headers_out

    def events_until(self, sid, stop_type, timeout=30, last_event_id=None, approve=True):
        """读事件流直到出现 stop_type；遇到权限确认就经控制面提交"允许"。"""
        h = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["cookie"] = self.cookie
        if self.proxy_secret:
            h.update({"X-NCS-Proxy-Secret": self.proxy_secret, self.auth_header: self.user})
        if last_event_id is not None:
            h["Last-Event-ID"] = str(last_event_id)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/s/{sid}/api/events", headers=h)
        r = conn.getresponse()
        assert r.status == 200, r.status
        events = []
        try:
            while True:
                line = r.readline()
                if not line:
                    break
                if not line.startswith(b"data: "):
                    continue
                e = json.loads(line[6:])
                events.append(e)
                if e["type"] == "permission_request" and approve:
                    self.request("POST", f"/s/{sid}/api/permission", {"id": e["id"], "decision": "yes"})
                if e["type"] == stop_type:
                    break
        finally:
            conn.close()
        return events

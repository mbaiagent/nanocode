"""测试辅助：把 server/ 和仓库根目录加进 import 路径，并准备临时目录、假的上游服务器。"""

import json
import os
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

SECRET = "s" * 40


def make_config(**kw) -> Config:
    cfg = Config(data_dir=tempfile.mkdtemp(prefix="ncs-test-"), token_secret=SECRET, port=0, gateway_port=0)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class FakeServer:
    """一个可编程的假 HTTP 服务器：handler(method, path, headers, body) -> (状态, 响应头, 响应体 bytes 或 bytes 列表)。"""

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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

"""web_fetch / web_search 遵守 HTTP(S)_PROXY / NO_PROXY（平台沙箱的出站代理，server/nanocode_server/egress.py）。

- 一般情况：先在本地解析并检查地址（拦内网），再经代理连接；
- NANOCODE_EGRESS_PROXY=1（平台沙箱，没有 DNS）：域名不在本地解析，交给代理；IP 字面量仍在本地拦；
- https 走 CONNECT 隧道，证书仍按域名校验；http 发绝对地址；NO_PROXY 里的直连。
"""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from helpers import LocalHTTPServer, Workspace

from nanocode.tools import ToolContext
from nanocode.tools.web import WebFetch, proxy_for
from nanocode.tools.web_search import WebSearch
from test_web_fetch import Handler

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
              "ALL_PROXY", "all_proxy", "NANOCODE_EGRESS_PROXY", "REQUEST_METHOD")


class FakeProxy:
    """最小的正向代理：记下请求行；绝对地址的 GET 直接回一段文字；CONNECT 按 tunnel_to 转到本机端口，没设就回 403。"""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.lines: list[str] = []
        self.tunnel_to = None
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    return
                buf += chunk
            line = buf.split(b"\r\n", 1)[0].decode()
            self.lines.append(line)
            if line.startswith("CONNECT"):
                if not self.tunnel_to:
                    c.sendall(b"HTTP/1.1 403 Destination is a private or internal address\r\nContent-Length: 0\r\n\r\n")
                    return
                up = socket.create_connection(("127.0.0.1", self.tunnel_to), timeout=5)
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                t = threading.Thread(target=self._copy, args=(up, c), daemon=True)
                t.start()
                self._copy(c, up)
                t.join(5)
                up.close()
                return
            body = f"via proxy: {line}".encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: "
                      + str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            pass
        finally:
            c.close()

    @staticmethod
    def _copy(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            try:
                b.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def no_dns(host):
    raise AssertionError(f"不该在本地解析 {host}")


class ProxyEnvTest(unittest.TestCase):
    def setUp(self):
        self.proxy = FakeProxy()
        self.addCleanup(self.proxy.close)
        url = f"http://127.0.0.1:{self.proxy.port}"
        env = {k: "" for k in PROXY_VARS}
        env.update(HTTP_PROXY=url, HTTPS_PROXY=url, NO_PROXY="localhost,direct.test")
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in PROXY_VARS:
            if not os.environ.get(k):
                os.environ.pop(k, None)
        self.ctx = ToolContext(cwd=Workspace().dir)

    def test_proxy_selection_follows_no_proxy(self):
        self.assertEqual(proxy_for("https", "example.com"), ("127.0.0.1", self.proxy.port))
        self.assertIsNone(proxy_for("https", "direct.test"))
        self.assertIsNone(proxy_for("ftp", "example.com"))

    def test_plain_http_goes_through_the_proxy_with_an_absolute_url(self):
        tool = WebFetch(resolver=lambda h: ["93.184.216.34"])
        r = tool.execute({"url": "http://site.test:8080/a?b=1"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertIn("via proxy: GET http://site.test:8080/a?b=1 HTTP/1.1", r.content)

    def test_without_the_platform_flag_private_addresses_are_still_refused_locally(self):
        tool = WebFetch(resolver=lambda h: ["10.0.0.7"])
        r = tool.execute({"url": "http://site.test/"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("内网", r.content)
        self.assertEqual(self.proxy.lines, [])

    def test_platform_sandbox_leaves_dns_to_the_proxy(self):
        os.environ["NANOCODE_EGRESS_PROXY"] = "1"
        r = WebFetch(resolver=no_dns).execute({"url": "http://site.test/x"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertIn("GET http://site.test/x", r.content)

    def test_ip_literals_are_refused_locally_even_with_the_platform_proxy(self):
        os.environ["NANOCODE_EGRESS_PROXY"] = "1"
        for url in ("http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://2130706433/",
                    "https://[::1]/"):
            with self.subTest(url=url):
                r = WebFetch(resolver=no_dns).execute({"url": url}, self.ctx)
                self.assertTrue(r.is_error)
                self.assertIn("内网", r.content)
        self.assertEqual(self.proxy.lines, [])

    def test_https_uses_connect_and_reports_the_proxy_refusal(self):
        os.environ["NANOCODE_EGRESS_PROXY"] = "1"
        r = WebFetch(resolver=no_dns).execute({"url": "https://internal.test/"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("403", r.content)
        self.assertEqual(self.proxy.lines, ["CONNECT internal.test:443 HTTP/1.1"])

    def test_no_proxy_hosts_connect_directly(self):
        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tool = WebFetch(resolver=lambda h: ["127.0.0.1"], allow_ips={"127.0.0.1"})
        r = tool.execute({"url": f"http://direct.test:{server.server_address[1]}/text"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertEqual(self.proxy.lines, [])

    def test_web_search_goes_through_the_proxy(self):
        os.environ["NANOCODE_EGRESS_PROXY"] = "1"
        tool = WebSearch(provider="searxng", base_url="http://searx.test", allow_http=True, resolver=no_dns)
        with self.assertRaises(Exception):
            tool.run({"query": "hello"}, self.ctx)          # 假代理回的不是 JSON：这里只看请求有没有经过代理
        self.assertEqual(len(self.proxy.lines), 1)
        self.assertTrue(self.proxy.lines[0].startswith("GET http://searx.test/search?"), self.proxy.lines)


@unittest.skipUnless(shutil.which("openssl"), "没有 openssl，无法生成测试证书")
class ProxyTlsTest(unittest.TestCase):
    """CONNECT 隧道里的 TLS 仍按**域名**校验证书：代理换不了内容。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="nanocode-tls-")
        cert, key = os.path.join(cls.tmp, "c.pem"), os.path.join(cls.tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
                        "-subj", "/CN=tunnel.test", "-addext", "subjectAltName=DNS:tunnel.test"], check=True,
                       capture_output=True)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        cls.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = server_ctx.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client_ctx = ssl.create_default_context(cafile=cert)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        self.proxy = FakeProxy()
        self.proxy.tunnel_to = self.server.server_address[1]
        self.addCleanup(self.proxy.close)
        url = f"http://127.0.0.1:{self.proxy.port}"
        patcher = mock.patch.dict(os.environ, {"HTTPS_PROXY": url, "https_proxy": url, "NANOCODE_EGRESS_PROXY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)
        self.ctx = ToolContext(cwd=Workspace().dir)

    def test_https_through_the_tunnel_is_verified_against_the_hostname(self):
        r = WebFetch(resolver=no_dns, ssl_context=self.client_ctx).execute({"url": "https://tunnel.test/text"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertIn("你好，世界", r.content)
        self.assertEqual(self.proxy.lines, ["CONNECT tunnel.test:443 HTTP/1.1"])

    def test_wrong_hostname_through_the_tunnel_is_rejected(self):
        r = WebFetch(resolver=no_dns, ssl_context=self.client_ctx).execute({"url": "https://other.test/text"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("证书", r.content)


if __name__ == "__main__":
    unittest.main()

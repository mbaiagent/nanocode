"""反向代理部署：显式信任对外的 Host / Origin，其余照旧拒绝。对应 issue #2。"""

import http.client
import io
import json
import os
import threading
import unittest
from contextlib import redirect_stderr
from unittest import mock

from helpers import Workspace

from nanocode import cli
from nanocode.session import Options, Session
from nanocode.web.server import (host_from_header, make_server, normalize_host, normalize_origin,
                                 public_settings, split_list)

PUB = "pub.example.com"


class PublicHostServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace()
        cls.session = Session(Options(provider="mock", cwd=cls.ws.dir, mock_delay=0), log=lambda m: None)
        cls.servers = []

    @classmethod
    def tearDownClass(cls):
        for srv in cls.servers:
            srv.shutdown()
            srv.server_close()
        cls.session.close()
        cls.ws.cleanup()

    def start(self, **kw):
        srv, app = make_server(self.session, "127.0.0.1", 0, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        return srv.server_address[1], app

    def req(self, port, method, path, headers, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def chat(self, port, app, host, origin=None):
        h = {"Host": host, "x-nanocode-token": app.token, "content-type": "application/json"}
        if origin:
            h["Origin"] = origin
        return self.req(port, "POST", "/api/chat", h, json.dumps({"message": "/help"}))

    # ------------------------------------------------------------------ 默认行为不变

    def test_default_still_rejects_public_host_and_origin(self):
        port, app = self.start()
        self.assertEqual(self.req(port, "GET", "/", {"Host": PUB}), 403)
        self.assertEqual(self.chat(port, app, f"127.0.0.1:{port}", f"https://{PUB}"), 403)
        self.assertEqual(self.req(port, "GET", "/", {"Host": f"127.0.0.1:{port}"}), 200)

    # ------------------------------------------------------------------ 配置了公网地址

    def test_public_host_allows_host_and_both_schemes(self):
        port, app = self.start(public_hosts=[PUB])
        self.assertEqual(self.req(port, "GET", "/", {"Host": PUB}), 200)
        self.assertEqual(self.req(port, "GET", "/", {"Host": f"{PUB}:8443"}), 200)     # 带端口的 Host
        self.assertEqual(self.chat(port, app, PUB, f"https://{PUB}"), 200)
        self.assertEqual(self.chat(port, app, PUB, f"http://{PUB}"), 200)
        self.assertEqual(self.chat(port, app, PUB), 200)                              # 没有 Origin（同源 GET 之外的调用）

    def test_public_host_does_not_open_the_door_to_others(self):
        port, app = self.start(public_hosts=[PUB])
        self.assertEqual(self.req(port, "GET", "/", {"Host": "evil.example.com"}), 403)        # DNS 重绑定
        self.assertEqual(self.req(port, "GET", "/", {"Host": f"x.{PUB}"}), 403)                # 不是通配
        self.assertEqual(self.chat(port, app, PUB, "https://evil.example.com"), 403)           # 跨站
        self.assertEqual(self.chat(port, app, PUB, f"https://{PUB}:8443"), 403)                # 未配置的端口

    def test_public_origin_for_non_default_port(self):
        port, app = self.start(public_hosts=[PUB], public_origins=[f"https://{PUB}:8443/"])
        self.assertEqual(self.chat(port, app, f"{PUB}:8443", f"https://{PUB}:8443"), 200)

    def test_public_origin_alone_does_not_trust_the_host(self):
        port, app = self.start(public_origins=[f"https://{PUB}"])
        self.assertEqual(self.req(port, "GET", "/", {"Host": PUB}), 403)

    def test_ipv6_public_host(self):
        port, app = self.start(public_hosts=["2001:db8::1"])
        self.assertEqual(self.req(port, "GET", "/", {"Host": "[2001:db8::1]:8443"}), 200)
        self.assertEqual(self.chat(port, app, "[2001:db8::1]", "https://[2001:db8::1]"), 200)

    def test_token_is_still_required(self):
        port, app = self.start(public_hosts=[PUB])
        h = {"Host": PUB, "content-type": "application/json"}
        self.assertEqual(self.req(port, "POST", "/api/chat", h, "{}"), 403)

    def test_invalid_config_fails_before_binding(self):
        with self.assertRaises(ValueError):
            make_server(self.session, "127.0.0.1", 0, public_hosts=["*.example.com"])


class NormalizeTest(unittest.TestCase):
    def test_normalize_host(self):
        self.assertEqual(normalize_host("https://Pub.Example.com:8443"), PUB)
        self.assertEqual(normalize_host("pub.example.com:443"), PUB)
        self.assertEqual(normalize_host("  1.2.3.4  "), "1.2.3.4")
        self.assertEqual(normalize_host("[::1]"), "::1")
        self.assertEqual(normalize_host("2001:db8::1"), "2001:db8::1")
        for bad in ("*", "*.example.com", "", "a b", "https://", "a/b"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_host(bad)

    def test_host_from_header(self):
        self.assertEqual(host_from_header("[::1]:8765"), "::1")
        self.assertEqual(host_from_header("[2001:db8::1]"), "2001:db8::1")      # 默认端口，没有端口
        self.assertEqual(host_from_header("[2001:DB8::1]:8443"), "2001:db8::1")
        self.assertEqual(host_from_header("Example.com:8443"), "example.com")
        self.assertEqual(host_from_header("example.com"), "example.com")
        self.assertEqual(host_from_header("127.0.0.1:8765"), "127.0.0.1")
        self.assertEqual(host_from_header(""), "")

    def test_normalize_origin(self):
        self.assertEqual(normalize_origin("HTTPS://Pub.Example.com:8443/"), "https://pub.example.com:8443")
        for bad in ("pub.example.com", "https://pub.example.com/path", "ftp://x", "https://*.example.com", ""):
            with self.assertRaises(ValueError, msg=bad):
                normalize_origin(bad)

    def test_split_and_env_merge(self):
        self.assertEqual(split_list(["a, b", "c", ""]), ["a", "b", "c"])
        with mock.patch.dict(os.environ, {"NANOCODE_PUBLIC_HOST": "x.com,y.com", "NANOCODE_PUBLIC_ORIGIN": "https://x.com"}):
            self.assertEqual(public_settings(["z.com"], ["https://z.com"]),
                             (["z.com", "x.com", "y.com"], ["https://z.com", "https://x.com"]))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NANOCODE_PUBLIC_HOST", None)
            os.environ.pop("NANOCODE_PUBLIC_ORIGIN", None)
            self.assertEqual(public_settings(None, None), ([], []))


class CliTest(unittest.TestCase):
    def test_invalid_public_host_exits_with_config_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--web", "--public-host", "*"])
        self.assertEqual(code, 2)
        self.assertIn("配置错误", err.getvalue())

    def test_invalid_public_origin_exits_with_config_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--web", "--public-origin", "not-an-origin"])
        self.assertEqual(code, 2)
        self.assertIn("配置错误", err.getvalue())


if __name__ == "__main__":
    unittest.main()

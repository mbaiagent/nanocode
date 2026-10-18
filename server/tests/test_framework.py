"""HTTP 框架的连接防护（#55）：慢客户端占不住线程，并发连接有上限、超限 503。

原来控制面和网关共用没有 socket 超时、没有连接上限的 ThreadingHTTPServer：
一个沙箱里的 Agent 开几千个只发半个请求的连接，就能把网关的线程和内存占光。
"""

import http.client
import socket
import threading
import time
import unittest
from unittest import mock

import support  # noqa: F401

from nanocode_server.web import framework
from nanocode_server.web.framework import App, Response, make_server


def serve(app, **kw):
    server = make_server(app, "127.0.0.1", 0, **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SlowClientTest(unittest.TestCase):
    """连上不发数据、或者一个字节一个字节地磨蹭，都不能长期占住一个线程。"""

    def server_with(self, timeout, deadline):
        # 读类属性是在 make_server 里绑定到 handler 上的，先 patch 模块常量再造 server
        with mock.patch.object(framework, "REQUEST_TIMEOUT", timeout), \
                mock.patch.object(framework, "READ_DEADLINE", deadline):
            return serve(App())

    def test_idle_connection_is_closed(self):
        server = self.server_with(0.3, 2.0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        try:
            started = time.time()
            self.assertEqual(conn.recv(1024), b"")          # 一直不发请求行：对端主动关掉
            self.assertLess(time.time() - started, 5)
        finally:
            conn.close()

    def test_half_sent_body_times_out(self):
        server = self.server_with(0.5, 1.0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        try:
            # 声明了 100 字节的请求体却只发 10 字节：总时限用完就断，不能一直等下去
            conn.sendall(b"POST /x HTTP/1.0\r\ncontent-length: 100\r\n\r\n" + b"x" * 10)
            self.assertIn(b" 408 ", conn.recv(1024))
        finally:
            conn.close()

    def test_normal_request_still_works(self):
        app = App()
        app.route("GET", "/ok")(lambda req: Response.json({"ok": True}))
        server = serve(app)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("GET", "/ok")
        self.assertEqual(conn.getresponse().read(), b'{"ok": true}')
        conn.close()


class ConcurrencyLimitTest(unittest.TestCase):
    """并发连接数有上限：放不下的连接直接 503，不起线程。"""

    def test_over_limit_connections_get_503(self):
        app = App()
        active, lock, release = [0], threading.Lock(), threading.Event()

        @app.route("GET", "/hold")
        def hold(req):
            with lock:
                active[0] += 1
            release.wait(10)
            return Response.json({"ok": True})

        server = serve(app, max_connections=2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        results = []

        def call():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/hold")
            results.append(conn.getresponse().status)
            conn.close()

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                if active[0] >= 2:
                    break
            time.sleep(0.02)
        self.assertEqual(active[0], 2)                     # 两个名额都被占住

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/hold")
        self.assertEqual(conn.getresponse().status, 503)   # 第三个：直接拒绝，不起线程
        conn.close()

        release.set()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [200, 200])

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)   # 名额还回来了
        conn.request("GET", "/hold")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()


if __name__ == "__main__":
    unittest.main()

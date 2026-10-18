"""HTTP 框架的连接防护：慢客户端占不住线程（#55）、并发连接超限 503（#55）、
客户端半途断开不刷 traceback（#70）。

原来控制面和网关共用没有 socket 超时、没有连接上限的 ThreadingHTTPServer：
一个沙箱里的 Agent 开几千个只发半个请求的连接，就能把网关的线程和内存占光。
"""

import http.client
import socket
import threading
import time
import types
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


class ClientDisconnectTest(unittest.TestCase):
    """客户端半途断开时不能刷 traceback（#70）。

    Windows 上报的是 ConnectionAbortedError（WinError 10053/10054），不在 Linux
    常见的 BrokenPipeError / ConnectionResetError 里：压测时每个断连一条 traceback，
    真正的错误会被淹没。断开就是断开，静默收尾即可。

    这里不给真 socket 发数据—— macOS 上写进一个已被对端关闭的 socket 往往先被内核
    缓冲下来、并不报错，端到端测不出来。直接把 wfile 换成会抛异常的假对象，
    确定性地覆盖"Windows 报的那个错"。
    """

    def _write(self, resp, exc):
        """拿 handler 类的 _write 直接跑一遍，wfile 换成会抛 exc 的假对象。"""
        server = make_server(App(), "127.0.0.1", 0)
        self.addCleanup(server.server_close)

        class _Aborted:
            def write(self, data):
                raise exc

            def flush(self):
                raise exc

        handler = types.SimpleNamespace(send_response=lambda *a: None, send_header=lambda *a: None,
                                        end_headers=lambda: None, wfile=_Aborted())
        with mock.patch("traceback.print_exc") as printed:
            server.RequestHandlerClass._write(handler, resp)
        return printed

    def test_aborted_plain_response_is_swallowed(self):
        # Windows 上客户端（压测脚本）跑完就断开，服务端的 write 抛 WinError 10053
        resp = Response(200, b"x" * 4096, [("content-type", "text/plain")])
        printed = self._write(resp, ConnectionAbortedError(10053, "已建立的连接被中止"))
        self.assertFalse(printed.called)

    def test_aborted_stream_is_swallowed(self):
        def chunks():
            yield b"data: 1\n\n"
            yield b"data: 2\n\n"

        resp = Response(200, chunks(), [("content-type", "text/event-stream")], stream=True)
        printed = self._write(resp, ConnectionAbortedError(10053, "已建立的连接被中止"))
        self.assertFalse(printed.called)

    def test_real_error_in_stream_still_reported(self):
        """断连要静默，但真正的错误（比如记账写库失败）不能跟着一起被吞掉（#60）。"""
        def failing():
            yield b"data: 1\n\n"
            raise RuntimeError("usage 记账失败")

        resp = Response(200, failing(), [("content-type", "text/event-stream")], stream=True)
        printed = self._write(resp, RuntimeError("unreachable"))
        self.assertTrue(printed.called)


if __name__ == "__main__":
    unittest.main()

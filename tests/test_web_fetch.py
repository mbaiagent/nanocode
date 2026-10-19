"""web_fetch 工具（#19）：能取到网页，并且不能被用来打内网、绕过确认或撑爆上下文。"""

import gzip
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler

from helpers import LocalHTTPServer, Workspace, call, make_agent

from nanocode.messages import text_block
from nanocode.permissions import PermissionManager
from nanocode.tools import ToolContext, default_tools
from nanocode.tools.web import WebFetch, html_to_text

PAGE = """<!doctype html><html><head><title>北京天气</title><style>body{color:red}</style>
<script>var secret = "SCRIPT-BODY";</script></head>
<body><h1>今日天气</h1><p>晴，<b>12</b>°C</p><ul><li>风力 2 级</li><li>空气质量 良</li></ul>
<a href="/more">更多</a> <a href="https://example.org/x">外链</a><!-- 注释 -->
<noscript>NOSCRIPT-BODY</noscript></body></html>"""


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def reply(self, body, ctype="text/html; charset=utf-8", status=200, headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Handler.seen.append({"path": self.path, "headers": dict(self.headers)})
        route = self.path.split("?")[0]
        if route == "/page":
            self.reply(PAGE)
        elif route == "/json":
            self.reply(json.dumps({"temp": 12, "城市": "北京"}, ensure_ascii=False), "application/json; charset=utf-8")
        elif route == "/text":
            self.reply("你好，世界", "text/plain; charset=utf-8")
        elif route == "/gbk":
            self.reply("你好，世界".encode("gbk"), "text/plain; charset=gbk")
        elif route == "/gz":
            self.reply(gzip.compress("压缩内容".encode()), "text/plain; charset=utf-8", headers={"content-encoding": "gzip"})
        elif route == "/redir":
            self.reply("", status=302, headers={"location": "/page"})
        elif route == "/loop":
            self.reply("", status=302, headers={"location": "/loop"})
        elif route == "/to-metadata":
            self.reply("", status=302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        elif route == "/to-file":
            self.reply("", status=302, headers={"location": "file:///etc/passwd"})
        elif route == "/big":
            self.reply("x" * 3_000_000, "text/plain")
        elif route == "/bin":
            self.reply(b"\x89PNG\r\n", "image/png")
        elif route == "/missing":
            self.reply("没有这个页面", status=404)
        elif route == "/slow":
            threading.Event().wait(3)
            self.reply("late")
        else:
            self.reply("?", status=500)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.ws = Workspace()
        self.ctx = ToolContext(cwd=self.ws.dir)
        Handler.seen.clear()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def fetch(self, path_or_url, tool=None):
        tool = tool or WebFetch(allow_ips={"127.0.0.1"})
        url = path_or_url if "://" in path_or_url else self.url(path_or_url)
        return tool.execute({"url": url}, self.ctx)


class FetchTest(Base):
    def test_html_becomes_readable_text(self):
        r = self.fetch("/page")
        self.assertFalse(r.is_error, r.content)
        for want in ("北京天气", "今日天气", "晴，12°C", "风力 2 级", "空气质量 良"):
            self.assertIn(want, r.content)
        for junk in ("SCRIPT-BODY", "NOSCRIPT-BODY", "color:red", "<p>", "注释"):
            self.assertNotIn(junk, r.content)
        self.assertIn(self.url("/more"), r.content)                    # 相对链接补全，模型才能接着点
        self.assertIn("https://example.org/x", r.content)

    def test_result_says_where_it_came_from_and_that_it_is_only_data(self):
        r = self.fetch("/page")
        head = r.content.split("\n\n", 1)[0]
        self.assertIn(self.url("/page"), head)
        self.assertIn("200", head)
        self.assertIn("数据", head)

    def test_json_and_plain_text_pass_through(self):
        self.assertIn('"城市": "北京"', self.fetch("/json").content)
        self.assertIn("你好，世界", self.fetch("/text").content)

    def test_charset_is_honoured(self):
        self.assertIn("你好，世界", self.fetch("/gbk").content)

    def test_gzip_is_decoded(self):
        self.assertIn("压缩内容", self.fetch("/gz").content)

    def test_query_string_is_kept_and_host_header_is_correct(self):
        self.fetch("/text?a=1&b=2")
        self.assertEqual(Handler.seen[0]["path"], "/text?a=1&b=2")
        self.assertEqual(Handler.seen[0]["headers"]["Host"], f"127.0.0.1:{self.port}")
        self.assertIn("nanocode", Handler.seen[0]["headers"]["User-Agent"])

    def test_redirects_are_followed_and_reported(self):
        r = self.fetch("/redir")
        self.assertIn("今日天气", r.content)
        self.assertIn(self.url("/page"), r.content.split("\n\n", 1)[0])      # 报的是最终地址

    def test_redirect_loop_stops(self):
        r = self.fetch("/loop")
        self.assertTrue(r.is_error)
        self.assertIn("重定向", r.content)
        self.assertLessEqual(len(Handler.seen), 10)

    def test_http_errors_are_reported_to_the_model(self):
        r = self.fetch("/missing")
        self.assertTrue(r.is_error)
        self.assertIn("404", r.content)

    def test_binary_content_is_refused(self):
        r = self.fetch("/bin")
        self.assertTrue(r.is_error)
        self.assertIn("image/png", r.content)

    def test_huge_pages_are_cut_off(self):
        r = self.fetch("/big")
        self.assertFalse(r.is_error)
        self.assertLess(len(r.content), 30000)
        self.assertIn("截断", r.content)

    def test_slow_server_times_out(self):
        r = self.fetch("/slow", WebFetch(allow_ips={"127.0.0.1"}, timeout=0.5))
        self.assertTrue(r.is_error)
        self.assertIn("超时", r.content)

    def test_connection_failure_is_a_readable_error(self):
        r = WebFetch(allow_ips={"127.0.0.1"}).execute({"url": "http://127.0.0.1:1/"}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("无法连接", r.content)

    def test_cancel_stops_the_fetch(self):
        self.ctx.cancel.set()
        r = self.fetch("/page")
        self.assertTrue(r.is_error)


class GuardTest(Base):
    """联网工具把请求发出去的地方是用户的机器：模型被提示词注入后，不能借它去碰内网、云元数据或本机服务。"""

    def blocked(self, url, tool=None):
        r = (tool or WebFetch()).execute({"url": url}, self.ctx)
        self.assertTrue(r.is_error, url)
        self.assertEqual(Handler.seen, [], f"{url} 不该真的发出请求")
        return r.content

    def test_private_and_local_addresses_are_refused(self):
        for url in [f"http://127.0.0.1:{self.port}/page", "http://localhost/", "http://[::1]/", "http://10.0.0.5/",
                    "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/",
                    "http://100.64.0.1/", "http://[fe80::1]/", "http://[fd00::1]/"]:
            self.assertIn("内网", self.blocked(url), url)

    def test_alternative_spellings_of_loopback_are_refused(self):
        for url in ["http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://127.1/", "http://[::ffff:127.0.0.1]/"]:
            # 要因为"是本机地址"被拒，而不是碰巧 DNS 查不到（macOS 的 getaddrinfo 不认这些写法，#92）
            self.assertIn("内网", self.blocked(url))

    def test_numeric_ipv4_spellings_do_not_hit_dns(self):
        from unittest import mock

        from nanocode.tools.web import _resolve
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该查 DNS")):
            for host in ["2130706433", "0x7f.0.0.1", "0177.0.0.1", "127.1", "10.0.0.5"]:
                ip = "10.0.0.5" if host == "10.0.0.5" else "127.0.0.1"
                self.assertEqual(_resolve(host), [ip], host)

    def test_only_http_and_https(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "gopher://example.com/", "//example.com/x", "example.com"]:
            self.assertIn("http", self.blocked(url))

    def test_credentials_in_url_are_refused(self):
        self.assertIn("用户名", self.blocked("http://user:pw@example.com/"))

    def test_one_private_address_among_many_is_enough_to_refuse(self):
        """DNS 返回多个地址时，只要有一个是内网就拒绝，不能赌连到哪一个。"""
        tool = WebFetch(resolver=lambda host: ["93.184.216.34", "10.0.0.7"])
        self.assertIn("内网", self.blocked("http://mixed.example/", tool))

    def test_redirect_to_a_private_address_is_refused(self):
        tool = WebFetch(allow_ips={"127.0.0.1"})
        r = tool.execute({"url": self.url("/to-metadata")}, self.ctx)
        self.assertTrue(r.is_error)
        self.assertIn("内网", r.content)
        self.assertEqual([s["path"] for s in Handler.seen], ["/to-metadata"])      # 第二跳没有发出

    def test_redirect_to_a_file_url_is_refused(self):
        r = self.fetch("/to-file")
        self.assertTrue(r.is_error)

    def test_connection_is_pinned_to_the_address_that_was_checked(self):
        """DNS 重绑定：检查时解析一次，连接就用这个地址，不能让连接时再解析出另一个（内网）地址。"""
        calls = []

        def resolver(host):
            calls.append(host)
            return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.1"]
        tool = WebFetch(resolver=resolver, allow_ips={"127.0.0.1"})
        r = tool.execute({"url": f"http://pinned.test:{self.port}/text"}, self.ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertEqual(calls, ["pinned.test"])
        self.assertEqual(Handler.seen[0]["headers"]["Host"], f"pinned.test:{self.port}")

    def test_the_default_tool_has_no_exemptions(self):
        tool = next(t for t in default_tools() if t.name == "web_fetch")
        self.assertEqual(tool.allow_ips, set())


class HttpsTest(unittest.TestCase):
    """HTTPS 走另一条连接路径：地址固定成检查过的 IP，但证书必须按**域名**校验。"""

    @classmethod
    def setUpClass(cls):
        import shutil
        import ssl
        import subprocess
        import tempfile
        if not shutil.which("openssl"):
            raise unittest.SkipTest("没有 openssl，无法生成测试证书")
        cls.tmp = tempfile.mkdtemp(prefix="nanocode-tls-")
        cert, key = os.path.join(cls.tmp, "c.pem"), os.path.join(cls.tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
                        "-subj", "/CN=pinned.test", "-addext", "subjectAltName=DNS:pinned.test"], check=True, capture_output=True)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        cls.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = server_ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client_ctx = ssl.create_default_context(cafile=cert)
        cls.untrusting_ctx = ssl.create_default_context()          # 不认识这张自签证书

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, host, ctx):
        tool = WebFetch(resolver=lambda h: ["127.0.0.1"], allow_ips={"127.0.0.1"}, ssl_context=ctx)
        return tool.execute({"url": f"https://{host}:{self.port}/text"}, ToolContext(cwd=Workspace().dir))

    def test_https_works_and_is_verified_against_the_hostname(self):
        r = self.fetch("pinned.test", self.client_ctx)
        self.assertFalse(r.is_error, r.content)
        self.assertIn("你好，世界", r.content)

    def test_certificate_for_another_hostname_is_rejected(self):
        r = self.fetch("other.test", self.client_ctx)
        self.assertTrue(r.is_error)
        self.assertIn("证书", r.content)

    def test_untrusted_certificate_is_rejected(self):
        r = self.fetch("pinned.test", self.untrusting_ctx)
        self.assertTrue(r.is_error)
        self.assertIn("证书", r.content)


class PermissionTest(Base):
    def setUp(self):
        super().setUp()
        self.tool = WebFetch()

    def check(self, mode="default", allow=(), url="https://wttr.in/Beijing?format=3"):
        pm = PermissionManager(self.ws.dir, mode=mode, allow=list(allow))
        return pm.check(self.tool, {"url": url}).behavior

    def test_asks_by_default_because_the_url_can_carry_data_out(self):
        self.assertFalse(self.tool.is_read_only)
        self.assertEqual(self.check(), "ask")

    def test_an_allow_rule_can_open_a_site(self):
        self.assertEqual(self.check(allow=["web_fetch(https://wttr.in/*)"]), "allow")
        self.assertEqual(self.check(allow=["web_fetch(https://wttr.in/*)"], url="https://evil.example/?https://wttr.in/x"), "ask")
        self.assertEqual(self.check(allow=["web_fetch"], url="https://anything.example/"), "allow")

    def test_review_mode_still_asks_and_plan_mode_refuses(self):
        self.assertEqual(self.check(mode="review"), "ask")
        self.assertEqual(self.check(mode="plan"), "deny")
        self.assertEqual(self.check(mode="bypass"), "allow")

    def test_non_interactive_denial_tells_how_to_allow(self):
        pm = PermissionManager(self.ws.dir)
        d = pm.resolve(self.tool, {"url": "https://wttr.in/Beijing"})
        self.assertEqual(d.behavior, "deny")
        self.assertIn("allow", d.reason)

    def test_deny_rule_wins(self):
        pm = PermissionManager(self.ws.dir, mode="bypass", deny=["web_fetch(*evil*)"])
        self.assertEqual(pm.check(self.tool, {"url": "https://evil.example/"}).behavior, "deny")


class AgentIntegrationTest(Base):
    def test_fetching_does_not_count_as_changing_the_project(self):
        """联网不会改动项目，不该触发"改完要验证"的闸门，也不该让终审汇总出现改动。"""
        agent, _, events = make_agent(self.ws, [[call("web_fetch", url=self.url("/text"))], [text_block("天气晴")]])
        agent.tools.register(WebFetch(allow_ips={"127.0.0.1"}))
        agent.run("查一下")
        self.assertFalse(agent._dirty)
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertIn("你好，世界", results[0]["content"])


class HtmlToTextTest(unittest.TestCase):
    def test_structure_and_entities(self):
        text = html_to_text("<h1>标题</h1><p>a &amp; b &lt;c&gt;</p><p>第二段</p><br>行<div>块</div>", "http://x/")
        self.assertEqual([l for l in text.splitlines() if l.strip()], ["标题", "a & b <c>", "第二段", "行", "块"])

    def test_broken_html_does_not_crash(self):
        self.assertIn("ok", html_to_text("<p>ok<div><b>unclosed <a href=", "http://x/"))

    def test_javascript_links_are_not_listed(self):
        self.assertNotIn("javascript", html_to_text('<a href="javascript:alert(1)">点我</a>', "http://x/"))


if __name__ == "__main__":
    unittest.main()

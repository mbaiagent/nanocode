"""web_search：provider 适配、请求形状与安全边界。

不联网：所有 provider 都指向本机的假搜索服务。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from helpers import LocalHTTPServer
from nanocode.tools import ToolContext, default_tools
from nanocode.tools.base import ToolError
from nanocode.tools.web_search import Brave, DuckDuckGo, SearchResult, Serper, Tavily, WebSearch

DDG_HTML = """<!doctype html><html><body>
<div class="result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&amp;rut=abc">Example Docs</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Official <b>documentation</b> for the API.</a>
</div>
<div class="result">
  <a class="result__a" href="https://example.org/post">Second result</a>
  <a class="result__snippet" href="https://example.org/post">A blog post.</a>
</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body=b""):
        Handler.seen.append({"method": self.command, "path": self.path,
                             "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

    def do_GET(self):
        self._record()
        route = self.path.split("?")[0]
        if route == "/searxng/search":
            self.reply(json.dumps({"results": [
                {"title": "SearX result", "url": "https://example.net/a", "content": "from searxng"}]}))
        elif route == "/brave":
            self.reply(json.dumps({"web": {"results": [
                {"title": "Brave result", "url": "https://example.net/b", "description": "from brave"}]}}))
        else:
            self.reply("?", status=404)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        self._record(body)
        route = self.path.split("?")[0]
        if route == "/html":
            self.reply(DDG_HTML, "text/html; charset=utf-8")
        elif route == "/tavily":
            self.reply(json.dumps({"results": [
                {"title": "Tavily result", "url": "https://example.net/t", "content": "from tavily"}]}))
        elif route == "/serper":
            self.reply(json.dumps({"organic": [
                {"title": "Serper result", "link": "https://example.net/s", "snippet": "from serper"}]}))
        elif route == "/deepseek/v1/search":
            self.reply(json.dumps({"data": [
                {"title": "DeepSeek result", "url": "https://example.net/d", "summary": "from deepseek"}]}))
        else:
            self.reply("?", status=404)


class WebSearchTest(unittest.TestCase):
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
        self.ctx = ToolContext(cwd=tempfile.mkdtemp(prefix="nc-search-"))
        Handler.seen.clear()
        self.base = f"http://127.0.0.1:{self.port}"

    def tool(self, provider, path="", **kw):
        return WebSearch(provider=provider, base_url=self.base + path, allow_http=True,
                         allow_ips={"127.0.0.1"}, **kw)

    def test_duckduckgo_is_keyless_default(self):
        tool = WebSearch(allow_http=True, allow_ips={"127.0.0.1"}, base_url=self.base + "/html")
        self.assertTrue(tool.available)
        out = tool.run({"query": "python asyncio"}, self.ctx)
        self.assertIn("Example Docs", out)
        self.assertIn("https://example.com/docs", out)       # uddg 参数已还原
        self.assertIn("Official documentation", out)
        self.assertIn("Second result", out)
        seen = Handler.seen[-1]
        self.assertEqual((seen["method"], seen["path"]), ("POST", "/html"))
        self.assertIn("q=python+asyncio", seen["body"].decode())
        self.assertIn("application/x-www-form-urlencoded", seen["headers"]["content-type"])

    def test_searxng_uses_json_and_base_url(self):
        out = self.tool("searxng", "/searxng").run({"query": "hello"}, self.ctx)
        self.assertIn("SearX result", out)
        self.assertIn("from searxng", out)
        seen = Handler.seen[-1]
        self.assertEqual(seen["method"], "GET")
        self.assertIn("/searxng/search?", seen["path"])
        self.assertIn("format=json", seen["path"])

    def test_tavily_request_and_parse(self):
        out = self.tool("tavily", "/tavily", api_key="tvly-key").run({"query": "x", "count": 3}, self.ctx)
        self.assertIn("Tavily result", out)
        seen = Handler.seen[-1]
        body = json.loads(seen["body"])
        self.assertEqual((body["query"], body["max_results"], body["api_key"]), ("x", 3, "tvly-key"))

    def test_brave_uses_subscription_header(self):
        out = self.tool("brave", "/brave", api_key="brave-key").run({"query": "x"}, self.ctx)
        self.assertIn("Brave result", out)
        seen = Handler.seen[-1]
        self.assertEqual(seen["headers"]["x-subscription-token"], "brave-key")
        self.assertIn("q=x", seen["path"])

    def test_serper_uses_api_key_header(self):
        out = self.tool("serper", "/serper", api_key="serper-key").run({"query": "x"}, self.ctx)
        self.assertIn("Serper result", out)
        seen = Handler.seen[-1]
        self.assertEqual(seen["headers"]["x-api-key"], "serper-key")
        self.assertEqual(json.loads(seen["body"])["num"], 5)

    def test_deepseek_compat_keeps_old_shape(self):
        out = self.tool("deepseek", "/deepseek/v1/search", api_key="ds-key").run({"query": "x"}, self.ctx)
        self.assertIn("DeepSeek result", out)
        seen = Handler.seen[-1]
        self.assertEqual(seen["headers"]["authorization"], "Bearer ds-key")
        self.assertEqual(json.loads(seen["body"]), {"query": "x", "count": 5})

    def test_https_is_required_by_default(self):
        tool = WebSearch(provider="searxng", base_url="http://127.0.0.1:9", allow_ips={"127.0.0.1"})
        with self.assertRaises(ToolError) as e:
            tool.run({"query": "x"}, self.ctx)
        self.assertIn("https", str(e.exception))

    def test_private_search_endpoint_is_rejected(self):
        tool = WebSearch(provider="searxng", base_url="https://search.internal", api_key="",
                         resolver=lambda host: ["10.0.0.5"])
        with self.assertRaises(ToolError) as e:
            tool.run({"query": "x"}, self.ctx)
        self.assertIn("内网", str(e.exception))

    def test_missing_key_hides_tool_from_registry(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("NANOCODE_SEARCH_", "NCS_SEARCH_"))}
        env.update({"NANOCODE_SEARCH_PROVIDER": "tavily", "NANOCODE_SEARCH_API_KEY": ""})
        with mock.patch.dict(os.environ, env, clear=True):
            names = [t.name for t in default_tools()]
        self.assertNotIn("web_search", names)

    def test_unknown_provider_is_unavailable(self):
        tool = WebSearch(provider="nope")
        self.assertFalse(tool.available)
        with self.assertRaises(ToolError) as e:
            tool.run({"query": "x"}, self.ctx)
        self.assertIn("未知的搜索 provider", str(e.exception))

    def test_result_parsers(self):
        self.assertEqual(DuckDuckGo().parse(DDG_HTML)[0].url, "https://example.com/docs")
        self.assertEqual(Tavily(api_key="k").parse('{"results":[{"title":"t","url":"https://a","content":"c"}]}')[0],
                         SearchResult("t", "https://a", "c"))
        brave = Brave(api_key="k").parse('{"web":{"results":[{"title":"t","url":"https://a","description":"d"}]}}')
        self.assertEqual(brave[0].url, "https://a")
        serper = Serper(api_key="k").parse('{"organic":[{"title":"t","link":"https://a","snippet":"s"}]}')
        self.assertEqual(serper[0].title, "t")

    def test_non_http_result_urls_are_dropped(self):
        # 结果里的 javascript:/data: 不能进入工具输出（模型可能把它当链接展示）
        tool = self.tool("tavily", "/tavily", api_key="k")
        bad = json.dumps({"results": [{"title": "x", "url": "javascript:alert(1)", "content": ""},
                                      {"title": "ok", "url": "https://ok.example", "content": ""}]})
        with mock.patch.object(tool.provider, "parse", return_value=[
            SearchResult("x", "javascript:alert(1)", ""), SearchResult("ok", "https://ok.example", "")]):
            out = tool.run({"query": "q"}, self.ctx)
        self.assertNotIn("javascript:", out)
        self.assertIn("https://ok.example", out)
        del bad


if __name__ == "__main__":
    unittest.main()

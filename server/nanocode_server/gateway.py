"""模型网关：沙箱调用模型的唯一出口，真实的模型 API Key 只在这里。

沙箱里的 nanocode 把它当成一个普通的 OpenAI 兼容（或 Anthropic）接口：
    POST /v1/chat/completions     OpenAI 兼容协议（沙箱 base_url = http://网关/v1）
    POST /v1/messages             Anthropic 协议（沙箱 base_url = http://网关）
    GET  /healthz                 健康检查
    GET  /internal/usage?owner=   查某人今天的用量（控制面用，需要 Authorization: Bearer <NCS_TOKEN_SECRET>）

每个请求：
  1. 验证网关令牌（tokens.py），得到用户；
  2. 限流：每人每分钟请求数；限额：每人每天 token 数。超了返回 429：
       - 每日限额用完：消息里带 "daily quota"，并给出 Retry-After（到 UTC 零点）。nanocode 识别成
         "配额已用完"（llm/base.py 的 QuotaExhausted），直接告诉用户，不会反复重试；
       - 每分钟请求太多：普通 429，nanocode 会退避重试；
  3. 改写请求：模型强制为配置的模型（防止用户选更贵的），输出上限压到配置值，OpenAI 协议要求返回用量；
  4. 带上真实 Key 转发给上游，流式响应原样转回去，同时从流里读出 token 用量记账。

记账在请求结束后进行，所以最后一个请求可能让当天用量略微超过上限，这是有意的取舍：
不需要在请求前预估 token，也不会因为预估不准误拒请求。
"""

from __future__ import annotations

import collections
import datetime
import hmac
import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import tokens
from .config import Config
from .store import UsageStore

MAX_BODY = 8 * 1024 * 1024     # 长对话的请求体可能有几 MB
ROUTES = {"/v1/chat/completions": "openai", "/chat/completions": "openai", "/v1/messages": "anthropic"}


def seconds_until_utc_midnight(now: float | None = None) -> int:
    now = time.time() if now is None else now
    d = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    nxt = (d + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int(nxt.timestamp() - now))


class RateLimiter:
    """每人每分钟的请求数（滑动窗口，内存里）。网关多副本时每个副本各算各的，总量放宽到 N 倍。"""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, owner: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time() if now is None else now
        with self.lock:
            q = self.hits[owner]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


class UsageMeter:
    """从上游响应里读出 token 用量。流式和非流式、两种协议都支持。"""

    def __init__(self, protocol: str):
        self.protocol = protocol
        self.input_tokens = 0
        self.output_tokens = 0

    def feed_sse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            data = json.loads(payload)
        except ValueError:
            return
        self.feed_json(data)

    def feed_json(self, data) -> None:
        if not isinstance(data, dict):
            return
        if self.protocol == "openai":
            u = data.get("usage") or {}
            if u:
                self.input_tokens = u.get("prompt_tokens") or self.input_tokens
                self.output_tokens = u.get("completion_tokens") or self.output_tokens
            return
        # Anthropic：message_start 里有输入用量，message_delta 里有输出用量；非流式响应顶层就有 usage
        u = (data.get("message") or {}).get("usage") or data.get("usage") or {}
        cached = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        if u.get("input_tokens") is not None:
            self.input_tokens = (u.get("input_tokens") or 0) + cached
        if u.get("output_tokens") is not None:
            self.output_tokens = u.get("output_tokens") or 0


class Gateway:
    def __init__(self, cfg: Config, usage: UsageStore | None = None):
        self.cfg = cfg
        self.usage = usage or UsageStore(os.path.join(cfg.data_dir, "gateway.db"))
        self.limiter = RateLimiter(cfg.requests_per_minute)
        up = urlparse(cfg.upstream_base_url)
        self.up_scheme, self.up_host, self.up_port = up.scheme, up.hostname, up.port
        self.up_path = up.path.rstrip("/")

    # ------------------------------------------------------------------ 规则

    def check_limits(self, owner: str) -> tuple[int, dict, dict] | None:
        """超限时返回 (状态码, 响应体, 额外响应头)；没超返回 None。"""
        if self.cfg.daily_tokens > 0:
            used = self.usage.get(owner)["total_tokens"]
            if used >= self.cfg.daily_tokens:
                wait = seconds_until_utc_midnight()
                msg = (f"daily quota exceeded for {owner}: used {used} of {self.cfg.daily_tokens} tokens today; "
                       f"resets at 00:00 UTC. 今日额度已用完，UTC 零点恢复，需要更多额度请联系管理员。")
                return 429, {"error": {"message": msg, "type": "quota_exceeded"}}, {"Retry-After": str(wait)}
        if not self.limiter.allow(owner):
            return 429, {"error": {"message": "too many requests per minute, slow down. 请求太频繁，请稍后重试。",
                                   "type": "rate_limited"}}, {"Retry-After": "10"}
        return None

    def rewrite(self, protocol: str, body: dict) -> dict:
        body = dict(body)
        if self.cfg.model:
            body["model"] = self.cfg.model
        if self.cfg.max_output_tokens:
            key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            body[key] = min(int(body.get(key) or self.cfg.max_output_tokens), self.cfg.max_output_tokens)
        if protocol == "openai" and body.get("stream"):
            body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}   # 才能计量
        return body

    def upstream_path(self, protocol: str) -> str:
        return self.up_path + ("/chat/completions" if protocol == "openai" else "/v1/messages")

    def upstream_headers(self, protocol: str, incoming) -> dict:
        h = {"content-type": "application/json", "accept": incoming.get("accept") or "application/json"}
        if protocol == "openai":
            h["authorization"] = f"Bearer {self.cfg.upstream_api_key}"
        else:
            h["x-api-key"] = self.cfg.upstream_api_key
            h["anthropic-version"] = incoming.get("anthropic-version") or "2023-06-01"
            if incoming.get("anthropic-beta"):
                h["anthropic-beta"] = incoming["anthropic-beta"]
        return h

    def connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.up_scheme == "https" else http.client.HTTPConnection
        return cls(self.up_host, self.up_port, timeout=300)


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"     # 流式响应写完就关连接，不用分块编码

        def log_message(self, *args):
            pass

        def _json(self, status: int, data: dict, headers: dict | None = None) -> None:
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _token(self) -> str:
            auth = self.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return (self.headers.get("x-api-key") or "").strip()

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._json(200, {"ok": True})
            if u.path == "/internal/usage":
                if not hmac.compare_digest(self._token().encode(), gw.cfg.token_secret.encode()):
                    return self._json(401, {"error": {"message": "unauthorized"}})
                owner = (parse_qs(u.query).get("owner") or [""])[0]
                return self._json(200, {**gw.usage.get(owner), "daily_limit": gw.cfg.daily_tokens})
            self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            protocol = ROUTES.get(urlparse(self.path).path)
            if not protocol:
                return self._json(404, {"error": {"message": "not found"}})
            if protocol != gw.cfg.upstream_protocol:
                return self._json(400, {"error": {"message": f"this gateway speaks {gw.cfg.upstream_protocol}, not {protocol}"}})
            claims = tokens.verify(gw.cfg.token_secret, self._token())
            if not claims:
                return self._json(401, {"error": {"message": "invalid or expired gateway token", "type": "auth"}})
            owner = claims["u"]
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._json(413, {"error": {"message": "request body too large"}})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": {"message": "invalid JSON"}})
            if not isinstance(body, dict):
                return self._json(400, {"error": {"message": "invalid request"}})
            limited = gw.check_limits(owner)
            if limited:
                return self._json(*limited)
            body = gw.rewrite(protocol, body)
            self._forward(protocol, owner, body)

        def _forward(self, protocol: str, owner: str, body: dict) -> None:
            meter = UsageMeter(protocol)
            conn = gw.connect()
            try:
                conn.request("POST", gw.upstream_path(protocol), body=json.dumps(body).encode(),
                             headers=gw.upstream_headers(protocol, self.headers))
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                return self._json(502, {"error": {"message": f"upstream unreachable: {type(e).__name__}"}})
            try:
                self.send_response(resp.status)
                for k in ("content-type", "retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests",
                          "x-ratelimit-reset-tokens", "anthropic-ratelimit-requests-reset"):
                    if resp.getheader(k):
                        self.send_header(k, resp.getheader(k))
                self.end_headers()
                ctype = resp.getheader("content-type") or ""
                if "text/event-stream" in ctype:
                    while True:
                        line = resp.readline()   # 不能读 resp.fp：那会绕过 HTTP/1.1 的分块解码
                        if not line:
                            break
                        meter.feed_sse_line(line.strip())
                        self.wfile.write(line)
                        if line in (b"\n", b"\r\n"):
                            self.wfile.flush()
                    self.wfile.flush()
                else:
                    data = resp.read()
                    self.wfile.write(data)
                    if resp.status == 200:
                        try:
                            meter.feed_json(json.loads(data))
                        except ValueError:
                            pass
            except (BrokenPipeError, ConnectionResetError):
                pass          # 沙箱那头断开了（用户中断）；已经产生的用量照样记账
            finally:
                conn.close()
                gw.usage.add(owner, requests=1, input_tokens=meter.input_tokens, output_tokens=meter.output_tokens)

    return Handler


def make_gateway_server(cfg: Config, usage: UsageStore | None = None) -> tuple[ThreadingHTTPServer, Gateway]:
    gw = Gateway(cfg, usage)
    server = ThreadingHTTPServer((cfg.gateway_host, cfg.gateway_port), make_handler(gw))
    server.daemon_threads = True
    return server, gw


__all__ = ["Gateway", "make_gateway_server", "RateLimiter", "UsageMeter", "seconds_until_utc_midnight"]

"""模型网关：沙箱调用模型的唯一出口，所有真实的 API Key 都只在这里使用。

    POST /v1/chat/completions   OpenAI 兼容协议（沙箱 base_url = http://网关/v1）
    POST /v1/messages           Anthropic 协议（沙箱 base_url = http://网关）
    GET  /healthz
    POST /internal/probe        控制面用：测试一个还没保存的 Key（需要内部密钥）

每个模型请求：
  1. 验证网关令牌，得到用户和会话；从数据库查这个会话选的模型，解析出上游地址和 Key；
  2. 限流（每人每分钟）；平台付费的模型再按「输入预估 + 输出上限」预留每日额度。超了返回 429：
       额度用完的消息里带 "daily quota" 和 Retry-After，nanocode 会识别成"配额已用完"，直接告诉用户；
       每分钟请求太多是普通 429，nanocode 会退避重试；
  3. 改写请求：模型强制为会话选的模型，输出上限压到配置值，OpenAI 协议要求流里带上用量；
  4. 带上真实 Key 转发，流式响应原样转回去，同时读出 token 用量记账。

额度是「预留 + 只退不多补」：请求前按输入规模预留（一条带 WHERE 的 UPDATE，并发下原子），
请求结束后按实际用量把多预留的退回去。以前是请求前读一次、结束后再记账（TOCTOU），
并发的请求都读到同一个旧值、全部放行，合力可以把每日额度超支几倍（#54）。
"""

from __future__ import annotations

import hmac
import json
import time

from ..config import Config
from ..models import Upstream
from ..security.keys import internal_key
from ..security.tokens import Signer, web_token_digest
from ..services.catalog import CatalogError, ModelCatalog
from ..services.upstream import UpstreamError, UsageMeter, open_request, probe
from ..storage import RateLimiter, Store
from .framework import App, HttpError, Request, Response

ROUTES = {"/v1/chat/completions": "openai", "/chat/completions": "openai", "/v1/messages": "anthropic"}
PASS_HEADERS = ("retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
                "anthropic-ratelimit-requests-reset")


def seconds_until_utc_midnight(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(1, 86400 - int(now) % 86400)


def estimate_input_tokens(body: dict) -> int:
    """粗算一次请求的输入 token 数：约 2 字符 1 token。

    英文约 4 字符 1 token、中文约 1.5 字符 1 token，取 2 两边都保守（宁可多预留）。
    只用于额度预留（#54）：预估偏大最多让临近额度的请求被拒，不会让平台超支。
    """
    text = json.dumps(body.get("messages") or [], ensure_ascii=False)
    if body.get("system"):                     # Anthropic 协议的系统提示单独一个字段
        text += json.dumps(body["system"], ensure_ascii=False)
    return max(1, (len(text) + 1) // 2)


class GatewayApp(App):
    max_body = 16 * 1024 * 1024       # 长对话的请求体可能有好几 MB

    def __init__(self, cfg: Config, store: Store, catalog: ModelCatalog, limiter: RateLimiter):
        super().__init__()
        self.cfg = cfg
        self.store = store
        self.catalog = catalog
        self.limiter = limiter
        self.signer = Signer(cfg.secret_key, "gateway")
        self.route("GET", "/healthz")(lambda req: Response.json({"ok": True}))
        self.route("POST", "/internal/probe")(self.probe)
        for path in ROUTES:
            self.route("POST", path)(self.complete)

    # ------------------------------------------------------------------ 模型请求

    def _caller(self, req: Request) -> tuple[str, Upstream]:
        auth = req.headers.get("authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else (req.headers.get("x-api-key") or "")
        claims = self.signer.verify(token)
        if not claims:
            raise HttpError(401, "invalid or expired gateway token")
        session = self.store.get_session(str(claims.get("s", "")))
        if session is None or session.owner != claims.get("u"):
            raise HttpError(401, "session no longer exists")
        # 沙箱停了令牌就必须失效：以前只查「存在 + owner」，stop 之后旧令牌在有效期内
        # （默认 12 小时）还能调用网关、按当前会话的模型消耗平台额度或用户自己的 Key（#53）。
        # starting 是启动中的瞬态，还没有沙箱能发出模型请求，一律按没运行处理。
        if session.status != "running":
            raise HttpError(401, "session is not running")
        # 代次校验：令牌签发时带的是当次沙箱 web_token 的摘要，stop / 重启会轮换
        # web_token。泄漏的旧令牌即使会话又跑起来了也对不上，直接拒绝。
        if not hmac.compare_digest(str(claims.get("w", "")), web_token_digest(session.web_token)):
            raise HttpError(401, "gateway token is stale")
        try:
            return session.owner, self.catalog.resolve(session.owner, session.model_ref)
        except CatalogError as e:
            raise HttpError(400, str(e)) from None

    def _reserve_quota(self, owner: str, up: Upstream, body: dict) -> int:
        """平台付费模型：请求前按「输入预估 + 输出上限」预留每日额度，返回预留量。

        检查 + 预留是一条带 WHERE 的 UPDATE（Store.reserve_quota），数据库行锁保证
        并发下只有真正放得下的请求能占到额度；请求结束后只退不多补，所以每日总额
        是硬上限，不会再被并发请求合力超支（#54）。
        """
        if not (up.billable and self.cfg.daily_tokens > 0):
            return 0
        need = estimate_input_tokens(body) + (self.cfg.max_output_tokens or 0)
        if not self.store.reserve_quota(owner, need, self.cfg.daily_tokens):
            raise HttpError(429, "daily quota exceeded: 今日平台模型额度已用完，UTC 零点恢复；"
                                "也可以在设置里添加自己的 Key。",
                            [("retry-after", str(seconds_until_utc_midnight()))])
        return need

    def _refund(self, owner: str, reserved: int) -> None:
        if reserved:                             # 没发到上游的请求，把预留的额度还回去
            self.store.settle_usage(owner, 0, 0, 0, -reserved)

    def _rewrite(self, up: Upstream, body: dict) -> dict:
        body = {**body, "model": up.model}
        cap = self.cfg.max_output_tokens
        if cap:
            key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            body[key] = min(int(body.get(key) or cap), cap)
        if up.protocol == "openai" and body.get("stream"):
            body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
        return body

    def complete(self, req: Request) -> Response:
        owner, up = self._caller(req)
        if ROUTES[req.path] != up.protocol:
            raise HttpError(400, f"this session's model speaks {up.protocol}")
        body = self._rewrite(up, req.json())
        if not self.limiter.allow(owner):
            raise HttpError(429, "too many requests per minute. 请求太频繁，请稍后重试。", [("retry-after", "10")])
        reserved = self._reserve_quota(owner, up, body)
        try:
            # 平台模型的地址是管理员配置的（可能是内网的自建推理服务），用户自己填的地址才做公网检查
            conn, resp = open_request(up, body, req.headers, self.cfg.allow_private_upstreams or up.billable)
        except UpstreamError as e:
            self._refund(owner, reserved)
            raise HttpError(502, str(e)) from None
        meter = UsageMeter(up.protocol)
        headers = [("content-type", resp.getheader("content-type") or "application/json")]
        headers += [(h, resp.getheader(h)) for h in PASS_HEADERS if resp.getheader(h)]

        def record():
            actual = meter.input_tokens + meter.output_tokens if up.billable else 0
            # 只退不多补：实际用量没超过预估就把差额退回去；超过了也按预留量计——
            # 这样「预留」才是真正的计入，每日总额不会超（#54）。input/output 始终记
            # 真实值，用户在页面上看到的用量统计不失真。
            self.store.settle_usage(owner, 1, meter.input_tokens, meter.output_tokens, min(actual, reserved) - reserved)

        if "text/event-stream" not in (resp.getheader("content-type") or ""):
            try:
                data = resp.read()
            finally:
                conn.close()
            if resp.status == 200:
                try:
                    meter.feed_json(json.loads(data))
                except ValueError:
                    pass
            record()
            return Response(resp.status, data, headers)

        def stream():
            try:
                while True:
                    line = resp.readline()        # 不能读 resp.fp：那会绕过 HTTP/1.1 的分块解码
                    if not line:
                        return
                    meter.feed_sse_line(line)
                    yield line
            finally:                              # 正常结束、沙箱中途断开，都要关连接、记账
                conn.close()
                record()
        return Response(resp.status, stream(), headers, stream=True)

    # ------------------------------------------------------------------ 内部接口

    def probe(self, req: Request) -> Response:
        auth = (req.headers.get("authorization") or "")[7:]
        if not hmac.compare_digest(auth.encode(), internal_key(self.cfg.secret_key).encode()):
            raise HttpError(401, "unauthorized")
        d = req.json()
        up = Upstream(str(d.get("protocol", "")), str(d.get("base_url", "")), str(d.get("api_key", "")),
                      str(d.get("model", "")), billable=False)
        if up.protocol not in ("openai", "anthropic"):
            raise HttpError(400, "bad protocol")
        return Response.json(probe(up, self.cfg.allow_private_upstreams))


__all__ = ["GatewayApp", "seconds_until_utc_midnight"]

"""把浏览器的请求转发给会话的沙箱（就是 nanocode 自己的 Web 界面和接口）。

  - Host 改成 localhost、去掉 Origin / Cookie / 身份头，加上沙箱的 Web 令牌（沙箱只认这个）；
  - 事件流（SSE）逐行转发，浏览器重连时带的 Last-Event-ID 原样转给沙箱，只补发没收到的事件；
  - 连不上沙箱，或者沙箱拒绝了我们生成的令牌（地址已经被别的沙箱复用），都说明沙箱不在了。
"""

from __future__ import annotations

import http.client
from urllib.parse import urlparse

from ..models import Session
from .framework import Request, Response

# x-nanocode-session：沙箱里的面板支持多会话（#106），页面靠这个头指明操作哪个会话
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "last-event-id", "x-nanocode-session")


class SandboxGone(Exception):
    pass


def forward(s: Session, req: Request, path: str) -> Response:
    ep = urlparse(s.endpoint)
    # 远程沙箱的地址带路径前缀（http://代理/sbx/<id>），本机 Docker / 子进程没有
    target = ep.path.rstrip("/") + path + (f"?{req.raw_query}" if req.raw_query else "")
    is_events = path == "/api/events"
    headers = {k: req.headers[k] for k in FORWARD_REQUEST_HEADERS if req.headers.get(k)}
    headers.update({"Host": "localhost", "x-nanocode-token": s.web_token})
    conn = http.client.HTTPConnection(ep.hostname, ep.port, timeout=120 if is_events else 60)
    try:
        conn.request(req.method, target, body=req.body if req.method == "POST" else None, headers=headers)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise SandboxGone(f"{type(e).__name__}") from None
    if resp.status == 403:
        conn.close()
        raise SandboxGone("令牌被拒绝")
    ctype = resp.getheader("content-type") or "application/octet-stream"
    if "text/event-stream" not in ctype:
        try:
            body = resp.read()
        finally:
            conn.close()
        # 只回传内容类型和缓存策略。CSP / X-Frame-Options 故意不用沙箱给的（沙箱不可信），
        # 由控制面统一加在所有 /s/ 响应上（control.py 的 SANDBOX_PANEL_HEADERS，#113）
        extra = [("cache-control", resp.getheader("cache-control"))] if resp.getheader("cache-control") else []
        return Response(resp.status, body, [("content-type", ctype), *extra])

    def lines():
        try:
            while True:
                line = resp.readline()        # 不能读 resp.fp：那会绕过 HTTP/1.1 的分块解码
                if not line:
                    return
                yield line
        finally:
            conn.close()
    return Response(resp.status, lines(), [("content-type", ctype), ("cache-control", "no-cache"),
                                          ("x-accel-buffering", "no")], stream=True)


__all__ = ["forward", "SandboxGone"]

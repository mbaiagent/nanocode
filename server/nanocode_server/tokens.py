"""网关令牌：控制面签发给每个沙箱，沙箱拿它调用模型网关。

沙箱里绝不能有真实的模型 API Key（Agent 执行一句 `env` 就能读到，被提示词注入后就能发出去）。
沙箱拿到的是这个令牌：
  - 只能用来访问模型网关，网关按令牌里的用户限流、限额、计量；
  - 有过期时间（也是沙箱的最长存活时间），过期后沙箱重启时重新签发；
  - 用 HMAC 签名，网关不需要查数据库就能验证，可以多副本部署。

格式：ncs1.<base64(JSON 载荷)>.<base64(HMAC-SHA256 签名)>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

PREFIX = "ncs1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, owner: str, session: str, ttl: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = _b64(json.dumps({"u": owner, "s": session, "exp": int(now + ttl)}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{PREFIX}.{payload}".encode(), hashlib.sha256).digest())
    return f"{PREFIX}.{payload}.{sig}"


def verify(secret: str, token: str, now: float | None = None) -> dict | None:
    """签名正确且没过期，返回 {"u": 用户, "s": 会话, "exp": 过期时间}；否则返回 None。"""
    now = time.time() if now is None else now
    try:
        prefix, payload, sig = token.split(".")
    except ValueError:
        return None
    if prefix != PREFIX:
        return None
    expected = _b64(hmac.new(secret.encode(), f"{PREFIX}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < now or not data.get("u"):
        return None
    return data


__all__ = ["sign", "verify"]

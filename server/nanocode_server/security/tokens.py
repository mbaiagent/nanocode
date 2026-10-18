"""签名令牌：一段 JSON + HMAC 签名 + 过期时间。验证不需要查数据库，多副本都能验证。

两种用途，密钥不同、互不通用：
    gateway   控制面签发给沙箱，沙箱拿它调用模型网关（载荷：用户、会话、沙箱代次）
    login     开发模式的登录 Cookie（载荷：用户）

格式：v1.<base64(JSON)>.<base64(HMAC-SHA256)>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .keys import derive

VERSION = "v1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def web_token_digest(web_token: str) -> str:
    """沙箱 Web 令牌的摘要，用于网关令牌的「代次」校验。

    网关令牌本身不带沙箱的 web_token（那是沙箱 Web 面板的密钥，不能泄漏），
    只带摘要：stop / 重启会轮换 web_token，网关比对当前会话的摘要就能认出
    旧令牌，泄漏的令牌不会在会话重启后继续有效（#53）。
    """
    return hashlib.sha256(web_token.encode()).hexdigest()[:16]


class Signer:
    def __init__(self, master: str, purpose: str):
        self.key = derive(master, f"token/{purpose}")

    def _sig(self, payload: str) -> str:
        return _b64(hmac.new(self.key, f"{VERSION}.{payload}".encode(), hashlib.sha256).digest())

    def sign(self, claims: dict, ttl: int, now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = _b64(json.dumps({**claims, "exp": int(now + ttl)}, separators=(",", ":")).encode())
        return f"{VERSION}.{payload}.{self._sig(payload)}"

    def verify(self, token: str, now: float | None = None) -> dict | None:
        """签名正确且没过期返回载荷，否则返回 None。"""
        try:
            version, payload, sig = token.split(".")
        except (ValueError, AttributeError):
            return None
        if version != VERSION or not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            claims = json.loads(_unb64(payload))
        except ValueError:
            return None
        now = time.time() if now is None else now
        if not isinstance(claims, dict) or claims.get("exp", 0) < now:
            return None
        return claims


__all__ = ["Signer", "web_token_digest"]

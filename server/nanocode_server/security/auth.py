"""用户身份。两种方式，由 NCS_AUTH 选择：

    dev     登录页输入任意用户名，签发一个 HttpOnly 的签名 Cookie。只允许在只监听本机时使用，
            用来在一台电脑上模拟多个用户（不同浏览器 / 无痕窗口登录不同的名字）。
    proxy   生产部署：控制面前面是认证代理（oauth2-proxy 等，对接企业 SSO），代理把用户身份放进
            请求头，并带上共享密钥 X-NCS-Proxy-Secret 证明请求确实经过了它。

开发模式另有一道检查：请求的 Host 必须是本机地址。否则恶意网页可以把自己的域名解析到
127.0.0.1（DNS 重绑定），在你的浏览器里冒充你驱动 Agent。
"""

from __future__ import annotations

import hmac
import re

from ..config import LOOPBACK, Config
from .tokens import Signer

COOKIE = "ncs_login"
PROXY_SECRET_HEADER = "X-NCS-Proxy-Secret"
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@+\-]{0,63}$")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def normalize_user(name: str) -> str:
    name = (name or "").strip().lower()
    if not USERNAME.match(name):
        raise AuthError("用户名只能包含字母、数字和 . _ @ + -，最长 64 个字符", 400)
    return name


def host_name(host_header: str) -> str:
    """Host 头里不带端口的主机名：[::1]:8700 → ::1，localhost:8700 → localhost。"""
    v = (host_header or "").strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


class Authenticator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.signer = Signer(cfg.secret_key, "login")

    @property
    def mode(self) -> str:
        return self.cfg.auth

    def check_host(self, host_header: str) -> None:
        if self.mode == "dev" and host_name(host_header) not in LOOPBACK:
            raise AuthError("开发模式只接受 Host 为本机地址的请求（防 DNS 重绑定）", 403)

    def identify(self, headers, cookies: dict) -> str | None:
        """返回已登录的用户名；没登录返回 None。代理模式下请求没经过代理直接抛 AuthError。"""
        if self.mode == "proxy":
            given = headers.get(PROXY_SECRET_HEADER) or ""
            if not hmac.compare_digest(given.encode(), self.cfg.proxy_secret.encode()):
                raise AuthError("请求没有经过认证代理")
            raw = headers.get(self.cfg.auth_header) or ""
            return normalize_user(raw) if raw else None
        claims = self.signer.verify(cookies.get(COOKIE, ""))
        return claims["u"] if claims else None

    def login_cookie(self, user: str) -> str:
        if self.mode != "dev":
            raise AuthError("只有开发模式可以直接登录", 404)
        token = self.signer.sign({"u": normalize_user(user)}, self.cfg.login_ttl)
        secure = "; Secure" if self.cfg.public_url.startswith("https://") else ""
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={self.cfg.login_ttl}{secure}"

    @staticmethod
    def logout_cookie() -> str:
        return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


__all__ = ["Authenticator", "AuthError", "normalize_user", "host_name", "COOKIE", "PROXY_SECRET_HEADER"]

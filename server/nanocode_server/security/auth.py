"""用户身份。三种方式，由 NCS_AUTH 选择（必须显式设置）：

    github  生产：GitHub OAuth 登录（见 github.py / accounts.py）。登录会话保存在服务端，
            Cookie 只是一个随机令牌；https 下 Cookie 名带 __Host- 前缀（必须 Secure、Path=/、不带 Domain）。
    proxy   控制面前面是认证代理（oauth2-proxy 等，对接企业 SSO），代理把用户身份放进
            请求头，并带上共享密钥 X-NCS-Proxy-Secret 证明请求确实经过了它。
    dev     登录页输入任意用户名，签发一个 HttpOnly 的签名 Cookie。只允许在只监听本机时使用，
            用来在一台电脑上模拟多个用户（不同浏览器 / 无痕窗口登录不同的名字）。

开发模式另有一道检查：请求的 Host 必须是本机地址。否则恶意网页可以把自己的域名解析到
127.0.0.1（DNS 重绑定），在你的浏览器里冒充你驱动 Agent。

identify() 返回用户 id（也就是其他表里的 owner）。
"""

from __future__ import annotations

import hmac
import re

from ..config import LOOPBACK, Config
from ..models import User
from .accounts import Accounts
from .tokens import Signer

COOKIE = "ncs_login"
PROXY_SECRET_HEADER = "X-NCS-Proxy-Secret"
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@+\-]{0,63}$")
OAUTH_TTL = 600


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
    def __init__(self, cfg: Config, accounts: Accounts | None = None):
        self.cfg = cfg
        self.accounts = accounts
        self.signer = Signer(cfg.secret_key, "login")
        self.oauth_signer = Signer(cfg.secret_key, "oauth-state")
        prefix = "__Host-" if cfg.https else ""
        self.session_cookie = f"{prefix}ncs_sid"
        self.oauth_cookie = f"{prefix}ncs_oauth"
        self._known: set[str] = set()       # dev / proxy：已经建过账号的 id，不用每个请求查库

    @property
    def mode(self) -> str:
        return self.cfg.auth

    def check_host(self, host_header: str) -> None:
        if self.mode == "dev" and host_name(host_header) not in LOOPBACK:
            raise AuthError("开发模式只接受 Host 为本机地址的请求（防 DNS 重绑定）", 403)

    def _ensure(self, uid: str, provider: str) -> str:
        if self.accounts and uid not in self._known:
            self.accounts.ensure_user(uid, provider)
            self._known.add(uid)
        return uid

    def identify(self, headers, cookies: dict) -> str | None:
        """返回已登录的用户 id；没登录返回 None。代理模式下请求没经过代理直接抛 AuthError。"""
        if self.mode == "github":
            user = self.accounts.resolve(cookies.get(self.session_cookie, "")) if self.accounts else None
            return user.id if user else None
        if self.mode == "proxy":
            given = headers.get(PROXY_SECRET_HEADER) or ""
            if not hmac.compare_digest(given.encode(), self.cfg.proxy_secret.encode()):
                raise AuthError("请求没有经过认证代理")
            raw = headers.get(self.cfg.auth_header) or ""
            return self._ensure(normalize_user(raw), "proxy") if raw else None
        claims = self.signer.verify(cookies.get(COOKIE, ""))
        return claims["u"] if claims else None

    # ------------------------------------------------------------------ Cookie

    def _cookie(self, name: str, value: str, max_age: int) -> str:
        secure = "; Secure" if self.cfg.https else ""
        return f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def login_cookie(self, user: str) -> str:
        """开发模式：签名 Cookie。"""
        if self.mode != "dev":
            raise AuthError("只有开发模式可以直接登录", 404)
        name = normalize_user(user)
        self._ensure(name, "dev")
        return self._cookie(COOKIE, self.signer.sign({"u": name}, self.cfg.login_ttl), self.cfg.login_ttl)

    def session_cookie_for(self, token: str) -> str:
        return self._cookie(self.session_cookie, token, self.cfg.session_ttl)

    def logout_cookies(self) -> list[str]:
        return [self._cookie(COOKIE, "", 0), self._cookie(self.session_cookie, "", 0)]

    # OAuth 的 state、PKCE verifier 和邀请码放在一个短命的签名 Cookie 里：不占数据库，
    # 任何副本都能验证；回调时 state 必须和 Cookie 里的一致（防 CSRF / 登录劫持）。
    def oauth_cookie_for(self, state: str, verifier: str, invite: str) -> str:
        token = self.oauth_signer.sign({"s": state, "v": verifier, "i": invite[:64]}, OAUTH_TTL)
        return self._cookie(self.oauth_cookie, token, OAUTH_TTL)

    def read_oauth_cookie(self, cookies: dict) -> dict | None:
        return self.oauth_signer.verify(cookies.get(self.oauth_cookie, ""))

    def clear_oauth_cookie(self) -> str:
        return self._cookie(self.oauth_cookie, "", 0)

    def current_user(self, uid: str) -> User | None:
        return self.accounts.store.get_user(uid) if self.accounts else None


__all__ = ["Authenticator", "AuthError", "normalize_user", "host_name", "COOKIE", "PROXY_SECRET_HEADER"]

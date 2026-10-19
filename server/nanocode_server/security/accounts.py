"""账号：登录会话（服务端保存，不用 JWT）、邀请码、GitHub 首次登录的注册规则。

登录会话
    Cookie 里是 32 字节随机令牌，数据库里只存它的 SHA-256：库泄露也拿不到能用的令牌。
    服务端保存的好处是能立即失效：退出、停用账号都直接作废。有绝对有效期和闲置有效期。

注册规则（NCS_AUTH=github）
    已经绑定过的 GitHub 账号（按数字 id，不按 login——login 可以改名）直接登录；
    第一次登录要有效的邀请码，除非 login 在引导管理员名单 NCS_ADMIN_GITHUB_LOGINS 里。
    名单里也可以写 id:<数字 id>，比 login 更稳。按 login 引导时，如果这个 login 已经被
    另一个 GitHub 账号用过（原主人改名后被别人注册），不给管理员。
"""

from __future__ import annotations

import hashlib
import secrets
import time

from ..config import Config
from ..models import Invite, User
from ..storage import Store

GITHUB = "github"


class AccountError(Exception):
    def __init__(self, message: str, code: str = "denied"):
        super().__init__(message)
        self.code = code


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_invite(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


class Accounts:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------ 登录会话

    def start_session(self, user: User, ip: str, user_agent: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        self.store.create_auth_session(token_hash(token), user.id, now + self.cfg.session_ttl, ip, user_agent, now)
        self.store.update_user(user.id, last_login=now)
        return token

    def resolve(self, token: str, now: float | None = None) -> User | None:
        if not token or len(token) > 128:
            return None
        now = time.time() if now is None else now
        h = token_hash(token)
        found = self.store.auth_session_user(h, self.cfg.session_idle, now)
        if not found:
            return None
        user, seen = found
        if now - seen > 300:                  # 闲置时间按 5 分钟粒度更新，不是每个请求都写库
            self.store.touch_auth_session(h, now)
        return user

    def end_session(self, token: str) -> None:
        if token:
            self.store.revoke_auth_session(token_hash(token))

    # ------------------------------------------------------------------ 邀请码

    def create_invite(self, admin: User, note: str = "", max_uses: int = 1, expires_days: float = 7,
                      now: float | None = None) -> tuple[Invite, str]:
        now = time.time() if now is None else now
        if not 1 <= max_uses <= 1000:
            raise AccountError("可用次数要在 1 到 1000 之间", "bad_request")
        if not 0 <= expires_days <= 365:
            raise AccountError("有效天数要在 0 到 365 之间（0 = 不过期）", "bad_request")
        raw = secrets.token_hex(8).upper()                       # 16 位十六进制，约 64 位熵
        code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))   # XXXX-XXXX-XXXX-XXXX
        inv = Invite(id="", code_hint=raw[-4:], note=note.strip()[:200], created_by=admin.id, created=now,
                     expires=now + expires_days * 86400 if expires_days else 0, max_uses=max_uses)
        return self.store.create_invite(inv, token_hash(normalize_invite(code))), code

    # ------------------------------------------------------------------ GitHub

    def _bootstrap_admin(self, profile: dict, uid: str = "") -> bool:
        names = set(self.cfg.admin_github_logins)
        if f"id:{profile['id']}" in names:
            return True
        login = profile["login"].lower()
        return login in names and not self.store.login_taken(login, GITHUB, exclude=uid)

    def github_login(self, profile: dict, invite_code: str = "", now: float | None = None) -> User:
        now = time.time() if now is None else now
        subject = str(profile["id"])
        info = {"login": profile["login"], "name": profile.get("name", ""), "email": profile.get("email", ""),
                "avatar_url": profile.get("avatar_url", "")}
        user = self.store.user_for_identity(GITHUB, subject)
        if user:
            if user.status != "active":
                raise AccountError("这个账号已被管理员停用", "disabled")
            promote = not user.is_admin and self._bootstrap_admin(profile, user.id)
            self.store.update_user(user.id, **info, **({"role": "admin"} if promote else {}))
            return self.store.get_user(user.id)
        admin = self._bootstrap_admin(profile)
        invite_id = ""
        if not admin:
            code = normalize_invite(invite_code)
            if not code:
                raise AccountError("第一次登录需要邀请码，请向管理员索取", "invite_required")
            invite_id = self.store.redeem_invite(token_hash(code), now) or ""
            if not invite_id:
                raise AccountError("邀请码无效、已用完、已过期或已被撤销", "invite_invalid")
        user = User(id=f"gh-{subject}", role="admin" if admin else "user", invite_id=invite_id, created=now, **info)
        if not self.store.create_user(user, GITHUB, subject):
            if invite_id:                                        # 并发首次登录：别人先建好了，退回邀请码
                self.store.release_invite(invite_id)
            existing = self.store.user_for_identity(GITHUB, subject)
            if not existing:
                raise AccountError("建账号失败，请重试", "retry")
            return existing
        return self.store.get_user(user.id)

    # ------------------------------------------------------------------ dev / proxy

    def ensure_user(self, uid: str, provider: str, now: float | None = None) -> User:
        """dev / proxy 模式：用户名（或代理给的邮箱）本身就是 id，第一次出现时建账号。"""
        user = self.store.get_user(uid)
        if user:
            return user
        role = "admin" if uid.lower() in self.cfg.admin_github_logins else "user"
        self.store.create_user(User(id=uid, login=uid, role=role, created=time.time() if now is None else now),
                               provider, uid)
        return self.store.get_user(uid) or User(id=uid, login=uid, role=role)


__all__ = ["Accounts", "AccountError", "token_hash", "normalize_invite", "GITHUB"]

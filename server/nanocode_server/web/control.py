"""控制面的 HTTP 接口。业务都在 services/ 里，这里只做：认证、参数校验、把结果变成响应。

页面
    GET  /                              平台主界面（会话列表 + 对话 + 设置）
    GET  /login                         登录页（GitHub 登录 / 开发模式的用户名登录）
    GET  /admin                         管理页（邀请码、账号），只有管理员
登录
    GET  /api/auth/info                 登录方式（登录页用，不需要登录）
    GET  /auth/github                   跳到 GitHub 授权（?invite=邀请码）
    GET  /auth/github/callback          GitHub 回调：校验 state、换令牌、按注册规则登录
    POST /api/login | /api/logout       开发模式登录 / 退出（退出会作废服务端的登录会话）
管理（管理员）
    GET  /api/admin/invites             邀请码列表      POST /api/admin/invites 新建（明文只返回这一次）
    POST /api/admin/invites/<id>/revoke 撤销
    GET  /api/admin/users               账号列表        POST /api/admin/users/<id>/status 停用 / 启用
接口
    GET  /api/me                        当前用户、今日用量、平台信息
    GET  /api/models                    我能用的模型
    GET  /api/providers                 添加 Key 时可选的厂商
    GET  /api/credentials               我的 Key（不含明文）
    POST /api/credentials               添加 Key        POST /api/credentials/<id>/delete
    POST /api/credentials/test          测试一个还没保存的 Key（经由模型网关）
    GET  /api/sessions                  我的会话        POST /api/sessions 新建
    POST /api/sessions/<id>/model       换模型          POST /api/sessions/<id>/rename 改名
    POST /api/sessions/<id>/stop        停止            POST /api/sessions/<id>/delete 删除
转发
    *    /s/<id>/...                    转发到这个会话的沙箱（nanocode 自己的界面和接口）
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from ..config import Config
from ..runtime import Launcher
from ..security.accounts import AccountError, Accounts
from ..security.auth import Authenticator, AuthError
from ..security.github import GitHubError, GitHubOAuth, pkce_pair
from ..security.keys import internal_key
from ..services.catalog import CatalogError, ModelCatalog
from ..services.sessions import SessionError, SessionManager
from ..storage import RateLimiter, StorageBusy, Store
from .framework import App, HttpError, Request, Response, static_dir
from .proxy import SandboxGone, forward

PUBLIC_PATHS = ("/healthz", "/login", "/api/login", "/api/auth/info", "/auth/github", "/auth/github/callback")
LOGIN_PATHS = ("/api/login", "/auth/github", "/auth/github/callback")
STATIC_FILE = r"[a-z0-9][a-z0-9._-]*"


class ControlApp(App):
    def __init__(self, cfg: Config, store: Store, auth: Authenticator, catalog: ModelCatalog,
                 sessions: SessionManager, launcher: Launcher, rate_backend: str = "memory",
                 accounts: Accounts | None = None, login_limiter: RateLimiter | None = None,
                 github: GitHubOAuth | None = None):
        super().__init__()
        self.accounts = accounts or auth.accounts
        self.login_limiter = login_limiter
        self.github = github
        self.cfg = cfg
        self.store = store
        self.auth = auth
        self.catalog = catalog
        self.sessions = sessions
        self.launcher = launcher
        self.rate_backend = rate_backend
        self.static = static_dir()
        self._routes_setup()

    # ------------------------------------------------------------------ 每个请求都要过的检查

    def handle(self, req: Request) -> Response:
        try:
            self.auth.check_host(req.headers.get("host") or "")
            if not (req.path in PUBLIC_PATHS or req.path.startswith("/static/")):
                req.user = self.auth.identify(req.headers, req.cookies)
        except AuthError as e:
            raise HttpError(e.status, str(e)) from None
        if req.method == "POST" and not self._same_origin(req):
            raise HttpError(403, "跨站请求被拒绝")
        # 只在 github 模式限流：dev 只听本机（压测和多用户模拟要一次登录上百人），proxy 由代理负责
        if (self.auth.mode == "github" and req.path in LOGIN_PATHS and self.login_limiter and not self.login_limiter.allow(f"login:{self._ip(req)}")):
            if req.path.startswith("/auth/"):
                return Response.redirect("/login?error=rate")
            raise HttpError(429, "登录太频繁，请一分钟后再试", [("retry-after", "60")])
        if req.user is None and not (req.path in PUBLIC_PATHS or req.path.startswith("/static/")):
            if req.method == "GET" and not req.path.startswith(("/api/", "/s/")):
                return Response.redirect("/login")
            raise HttpError(401, "请先登录")
        try:
            return self.dispatch(req)
        except SessionError as e:
            raise HttpError(e.status, str(e)) from None
        except CatalogError as e:
            raise HttpError(400, str(e)) from None
        except StorageBusy as e:
            # 连接池拿不到连接：明确告诉客户端"服务端忙，重试"，别变成 500 或断连接（#60）
            raise HttpError(503, f"数据库正忙，请稍后重试：{e}") from None

    @staticmethod
    def _same_origin(req: Request) -> bool:
        """带 Origin 的 POST 必须来自本站。再加上写接口只收 JSON（跨站发 JSON 要预检，我们不响应预检）。"""
        origin = req.headers.get("origin")
        return not origin or urlparse(origin).netloc.lower() == (req.headers.get("host") or "").lower()

    def _ip(self, req: Request) -> str:
        """客户端 IP。前面有反向代理（NCS_TRUST_FORWARDED=1）时取 X-Forwarded-For 的最后一项：
        那是代理自己追加的，前面几项客户端可以随便填。"""
        if self.cfg.trust_forwarded:
            xff = [p.strip() for p in (req.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
            if xff:
                return xff[-1][:64]
        return req.client_ip

    def _me(self, req: Request):
        user = self.auth.current_user(req.user)
        if user is None:
            raise HttpError(401, "请先登录")
        return user

    def _admin(self, req: Request):
        user = self._me(req)
        if not user.is_admin:
            raise HttpError(403, "只有管理员可以操作")
        return user

    # ------------------------------------------------------------------ 路由

    def _routes_setup(self) -> None:
        r = self.route

        @r("GET", "/healthz")
        def healthz(req):
            # 编排层靠这个探测平台是不是退化成没有隔离的本机启动器（#36）：
            # launcher=local 且 isolated=false 时应该告警，而不是等用户登录后从 /api/me 里发现。
            return Response.json({"ok": True, "launcher": self.launcher.name, "isolated": self.launcher.isolated})

        @r("GET", "/static/{name}")
        def static(req):
            name = req.params["name"]
            if not re.fullmatch(STATIC_FILE, name):
                raise HttpError(404, "文件不存在")
            return Response.file(os.path.join(self.static, name))

        @r("GET", "/login")
        def login_page(req):
            if self.auth.mode == "proxy":
                return Response.redirect("/")
            return Response.file(os.path.join(self.static, "login.html"), cache="no-store")

        @r("GET", "/api/auth/info")
        def auth_info(req):
            return Response.json({"ok": True, "mode": self.auth.mode, "invite_required": self.auth.mode == "github"})

        @r("GET", "/auth/github")
        def github_start(req):
            if self.auth.mode != "github" or not self.github:
                raise HttpError(404, "没有这个地址")
            state = secrets.token_urlsafe(24)
            verifier, challenge = pkce_pair()
            cookie = self.auth.oauth_cookie_for(state, verifier, req.arg("invite").strip())
            return Response.redirect(self.github.authorize_url(state, challenge), [("set-cookie", cookie)])

        @r("GET", "/auth/github/callback")
        def github_callback(req):
            if self.auth.mode != "github" or not self.github:
                raise HttpError(404, "没有这个地址")
            clear = ("set-cookie", self.auth.clear_oauth_cookie())

            def fail(code: str, log: str = "") -> Response:
                if log:
                    print(f"[auth] GitHub 登录失败 [{req.rid}] {code}: {log}", flush=True)
                return Response.redirect(f"/login?error={quote(code)}", [clear])

            saved = self.auth.read_oauth_cookie(req.cookies)
            state = req.arg("state")
            # state 必须和发起登录时放进签名 Cookie 的一致：防止别人把自己的 code 塞给你（登录 CSRF）
            if not saved or not state or not hmac.compare_digest(str(saved.get("s", "")), state):
                return fail("state")
            if req.arg("error"):
                return fail("denied")
            code = req.arg("code")
            if not code:
                return fail("state")
            try:
                token = self.github.exchange(code, str(saved.get("v", "")))
                profile = self.github.profile(token)
            except GitHubError as e:
                return fail("github", str(e))
            try:
                user = self.accounts.github_login(profile, str(saved.get("i", "")))
            except AccountError as e:
                return fail(e.code)
            sid = self.accounts.start_session(user, self._ip(req), req.headers.get("user-agent") or "")
            return Response.redirect("/", [clear, ("set-cookie", self.auth.session_cookie_for(sid))])

        @r("POST", "/api/login")
        def login(req):
            if self.auth.mode != "dev":
                raise HttpError(404, "没有这个地址")
            user = str(req.json().get("user", ""))
            try:
                cookie = self.auth.login_cookie(user)
            except AuthError as e:
                raise HttpError(e.status, str(e)) from None
            return Response.json({"ok": True}, headers=[("set-cookie", cookie)])

        @r("POST", "/api/logout")
        def logout(req):
            if self.accounts:
                self.accounts.end_session(req.cookies.get(self.auth.session_cookie, ""))
            return Response.json({"ok": True}, headers=[("set-cookie", c) for c in self.auth.logout_cookies()])

        @r("GET", "/admin")
        def admin_page(req):
            self._admin(req)
            return Response.file(os.path.join(self.static, "admin.html"), cache="no-store")

        @r("GET", "/api/admin/invites")
        def list_invites(req):
            self._admin(req)
            now = time.time()
            return Response.json({"ok": True, "invites": [i.public(now) for i in self.store.list_invites()]})

        @r("POST", "/api/admin/invites")
        def create_invite(req):
            admin = self._admin(req)
            d = req.json()
            try:
                inv, code = self.accounts.create_invite(admin, str(d.get("note", "")), int(d.get("max_uses", 1)),
                                                        float(d.get("expires_days", 7)))
            except (TypeError, ValueError):
                raise HttpError(400, "max_uses / expires_days 要是数字") from None
            except AccountError as e:
                raise HttpError(400, str(e)) from None
            return Response.json({"ok": True, "invite": inv.public(time.time()), "code": code}, 201)

        @r("POST", "/api/admin/invites/{iid}/revoke")
        def revoke_invite(req):
            self._admin(req)
            req.json()
            if not self.store.revoke_invite(req.params["iid"]):
                raise HttpError(404, "不存在")
            return Response.json({"ok": True})

        @r("GET", "/api/admin/users")
        def list_users(req):
            self._admin(req)
            return Response.json({"ok": True, "users": [u.public() for u in self.store.list_users()]})

        @r("POST", "/api/admin/users/{uid}/status")
        def set_user_status(req):
            admin = self._admin(req)
            status = str(req.json().get("status", ""))
            if status not in ("active", "disabled"):
                raise HttpError(400, "status 只能是 active 或 disabled")
            uid = req.params["uid"]
            if uid == admin.id:
                raise HttpError(400, "不能停用自己")
            if not self.store.get_user(uid):
                raise HttpError(404, "不存在")
            self.store.update_user(uid, status=status)
            if status == "disabled":
                self.store.revoke_user_sessions(uid)
                for s in self.sessions.list(uid):         # 正在跑的沙箱也停掉，网关令牌随之失效（#53）
                    if s.active:
                        self.sessions.stop(s.id)
            return Response.json({"ok": True})

        @r("GET", "/")
        def shell(req):
            return Response.file(os.path.join(self.static, "index.html"), cache="no-store")

        @r("GET", "/api/me")
        def me(req):
            u = self.auth.current_user(req.user)
            return Response.json({
                "ok": True, "user": req.user, "auth": self.auth.mode,
                "login": u.login if u else req.user, "name": u.name if u else "",
                "avatar_url": u.avatar_url if u else "", "role": u.role if u else "user",
                "usage": self.store.get_usage(req.user), "daily_limit": self.cfg.daily_tokens,
                "limits": {"sessions": self.cfg.max_sessions_per_user, "idle_timeout": self.cfg.idle_timeout},
                "platform": {"launcher": self.launcher.name, "isolated": self.launcher.isolated,
                             "database": self.store.backend, "rate_limit": self.rate_backend},
            })

        @r("GET", "/api/models")
        def models(req):
            return Response.json({"ok": True, "models": [vars(o) for o in self.catalog.options(req.user)]})

        @r("GET", "/api/providers")
        def providers(req):
            return Response.json({"ok": True, "providers": self.catalog.providers(),
                                  "allow_private": self.cfg.allow_private_upstreams})

        @r("GET", "/api/credentials")
        def credentials(req):
            return Response.json({"ok": True, "credentials": [c.public() for c in self.store.list_credentials(req.user)]})

        @r("POST", "/api/credentials")
        def add_credential(req):
            d = req.json()
            c = self.catalog.add_credential(req.user, str(d.get("provider", "")), str(d.get("api_key", "")),
                                            _model_list(d.get("models")), str(d.get("label", "")),
                                            str(d.get("base_url", "")), str(d.get("protocol", "")))
            return Response.json({"ok": True, "credential": c.public()}, 201)

        @r("POST", "/api/credentials/{cid}/delete")
        def delete_credential(req):
            req.json()
            if not self.store.delete_credential(req.user, req.params["cid"]):
                raise HttpError(404, "不存在")
            return Response.json({"ok": True})

        @r("POST", "/api/credentials/test")
        def test_credential(req):
            d = req.json()
            up = self.catalog.draft_upstream(str(d.get("provider", "")), str(d.get("api_key", "")),
                                             (_model_list(d.get("models")) or [""])[0],
                                             str(d.get("base_url", "")), str(d.get("protocol", "")))
            return Response.json(self._probe(up))

        @r("GET", "/api/sessions")
        def list_sessions(req):
            return Response.json({"ok": True, "sessions": [s.public() for s in self.sessions.list(req.user)]})

        @r("POST", "/api/sessions")
        def create_session(req):
            d = req.json()
            s = self.sessions.create(req.user, str(d.get("title", "")), str(d.get("model", "")))
            return Response.json({"ok": True, "session": s.public()}, 201)

        @r("POST", "/api/sessions/{sid}/model")
        def switch_model(req):
            s = self.sessions.switch_model(req.user, req.params["sid"], str(req.json().get("model", "")))
            return Response.json({"ok": True, "session": s.public()})

        @r("POST", "/api/sessions/{sid}/rename")
        def rename(req):
            title = str(req.json().get("title", "")).strip()[:100]
            if not title:
                raise HttpError(400, "名字不能为空")
            s = self.sessions.owned(req.user, req.params["sid"])
            self.store.update_session(s.id, title=title)
            return Response.json({"ok": True})

        @r("POST", "/api/sessions/{sid}/stop")
        def stop(req):
            req.json()
            self.sessions.stop(self.sessions.owned(req.user, req.params["sid"]).id)
            return Response.json({"ok": True})

        @r("POST", "/api/sessions/{sid}/delete")
        def delete(req):
            req.json()
            self.sessions.delete(self.sessions.owned(req.user, req.params["sid"]).id)
            return Response.json({"ok": True})

        for method in ("GET", "POST"):
            r(method, "/s/{sid}")(lambda req: Response.redirect(f"/s/{self.sessions.owned(req.user, req.params['sid']).id}/"))
            r(method, "/s/{sid}/{rest:path}")(self._proxy)

    # ------------------------------------------------------------------ 转发和网关

    def _proxy(self, req: Request) -> Response:
        s = self.sessions.owned(req.user, req.params["sid"])
        s = self.sessions.ensure_running(s.id)
        path = "/" + req.params["rest"]
        if path != "/api/events":            # 只连着事件流的页面不算活动，否则开着标签页就永远不回收
            self.sessions.touch(s.id)
        try:
            return forward(s, req, path)
        except SandboxGone:
            self.sessions.mark_dead(s.id)
            raise HttpError(502, "沙箱已经不在了，刷新页面会重新启动") from None

    def _probe(self, up) -> dict:
        """经由模型网关测试 Key：控制面自己不访问任何模型厂商，出口只有网关一个。"""
        body = json.dumps({"protocol": up.protocol, "base_url": up.base_url, "api_key": up.api_key,
                           "model": up.model}).encode()
        req = urllib.request.Request(self.sessions.gateway_url + "/internal/probe", data=body, method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {internal_key(self.cfg.secret_key)}"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"网关返回 HTTP {e.code}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": f"连不上模型网关：{e}"}


def _model_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [m for m in str(value or "").replace("\n", ",").split(",")]


__all__ = ["ControlApp"]

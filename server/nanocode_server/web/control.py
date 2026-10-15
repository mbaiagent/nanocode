"""控制面的 HTTP 接口。业务都在 services/ 里，这里只做：认证、参数校验、把结果变成响应。

页面
    GET  /                              平台主界面（会话列表 + 对话 + 设置）
    GET  /login                         登录页（开发模式）
接口
    POST /api/login | /api/logout       开发模式登录 / 退出
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

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..config import Config
from ..runtime import Launcher
from ..security.auth import Authenticator, AuthError
from ..security.keys import internal_key
from ..services.catalog import CatalogError, ModelCatalog
from ..services.sessions import SessionError, SessionManager
from ..storage import Store
from .framework import App, HttpError, Request, Response, static_dir
from .proxy import SandboxGone, forward

PUBLIC_PATHS = ("/healthz", "/login", "/api/login")
STATIC_FILE = r"[a-z0-9][a-z0-9._-]*"


class ControlApp(App):
    def __init__(self, cfg: Config, store: Store, auth: Authenticator, catalog: ModelCatalog,
                 sessions: SessionManager, launcher: Launcher, rate_backend: str = "memory"):
        super().__init__()
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

    @staticmethod
    def _same_origin(req: Request) -> bool:
        """带 Origin 的 POST 必须来自本站。再加上写接口只收 JSON（跨站发 JSON 要预检，我们不响应预检）。"""
        origin = req.headers.get("origin")
        return not origin or urlparse(origin).netloc.lower() == (req.headers.get("host") or "").lower()

    # ------------------------------------------------------------------ 路由

    def _routes_setup(self) -> None:
        r = self.route

        @r("GET", "/healthz")
        def healthz(req):
            return Response.json({"ok": True})

        @r("GET", "/static/{name}")
        def static(req):
            name = req.params["name"]
            if not re.fullmatch(STATIC_FILE, name):
                raise HttpError(404, "文件不存在")
            return Response.file(os.path.join(self.static, name))

        @r("GET", "/login")
        def login_page(req):
            if self.auth.mode != "dev":
                return Response.redirect("/")
            return Response.file(os.path.join(self.static, "login.html"), cache="no-store")

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
            return Response.json({"ok": True}, headers=[("set-cookie", self.auth.logout_cookie())])

        @r("GET", "/")
        def shell(req):
            return Response.file(os.path.join(self.static, "index.html"), cache="no-store")

        @r("GET", "/api/me")
        def me(req):
            return Response.json({
                "ok": True, "user": req.user, "auth": self.auth.mode,
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

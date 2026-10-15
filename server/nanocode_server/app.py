"""控制面：用户登录后在这里管理自己的会话，所有对沙箱的访问都经过这里转发。

    GET  /healthz                     健康检查（不需要登录）
    GET  /                            会话列表页面
    GET  /api/me                      当前用户、今天的模型用量
    GET  /api/sessions                我的会话
    POST /api/sessions                新建会话并启动沙箱 {"title": "..."}
    POST /api/sessions/<id>/stop      停止沙箱（保留工作区，之后打开会自动恢复）
    POST /api/sessions/<id>/delete    停止沙箱并删除工作区
    *    /s/<id>/...                  转发到这个会话的沙箱（就是 nanocode 自己的 Web 界面和接口）

转发时控制面会：
  - 先确认会话属于当前用户（不是自己的一律 404，不暴露别人的会话是否存在）；
  - 沙箱没在运行就先启动它（恢复上次的对话），受总容量和每人上限约束；
  - 把 Host 改成 localhost、去掉 Origin / Cookie / 身份头，加上沙箱的 Web 令牌（沙箱只认这个令牌）；
  - 事件流（SSE）逐行转发，浏览器断线重连带的 Last-Event-ID 原样转给沙箱，只补发没收到的事件。

后台线程定期回收空闲的沙箱：一段时间没有请求、Agent 也不在工作，就停掉（工作区保留）。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import tokens
from .auth import AuthError, authenticate
from .config import Config
from .launcher import Launcher, LaunchError, SandboxSpec, make_launcher
from .store import SessionStore

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 1024 * 1024
SID = re.compile(r"^[0-9a-f]{16}$")
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "last-event-id")
FORWARD_RESPONSE_HEADERS = ("content-type", "cache-control")


class LimitError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ControlPlane:
    def __init__(self, cfg: Config, launcher: Launcher | None = None, store: SessionStore | None = None):
        self.cfg = cfg
        self.store = store or SessionStore(os.path.join(cfg.data_dir, "control.db"))
        self.launcher = launcher or make_launcher(cfg)
        self._admission = threading.Lock()          # 容量检查和"占位"必须是原子的
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._stop = threading.Event()
        self.reconcile()

    # ------------------------------------------------------------------ 生命周期

    def reconcile(self) -> None:
        """控制面重启后：数据库里标着运行中、实际已经不在的沙箱，改成已停止。"""
        for s in self.store.active():
            if not self.launcher.is_alive(s["id"]):
                self.store.update(s["id"], status="stopped", endpoint="")

    def _lock_for(self, sid: str) -> threading.Lock:
        with self._locks_lock:
            return self._session_locks.setdefault(sid, threading.Lock())

    def check_capacity(self, owner: str, exclude: str = "") -> None:
        active = [s for s in self.store.active() if s["id"] != exclude]
        if sum(1 for s in active if s["owner"] == owner) >= self.cfg.max_sessions_per_user:
            raise LimitError(429, f"你同时运行的会话已达上限（{self.cfg.max_sessions_per_user} 个），先停掉一个再试")
        if len(active) >= self.cfg.max_sandboxes:
            raise LimitError(503, "平台容量已满，请稍后再试")

    def ensure_running(self, sid: str) -> dict:
        """会话的沙箱在运行就直接返回，否则启动（之前启动过的会恢复上次的对话）。"""
        with self._lock_for(sid):
            s = self.store.get(sid)
            if s is None:
                raise LimitError(404, "会话不存在")
            # 不在每个请求上都问启动器"还活着吗"（K8s 下就是每个请求一次 API 调用）：
            # 信任数据库里的状态，转发失败时再标记为已停止（见 _proxy），下次访问自动重启
            if s["status"] == "running" and s["endpoint"]:
                return s
            with self._admission:
                self.check_capacity(s["owner"], exclude=sid)
                web_token = secrets.token_urlsafe(32)
                self.store.update(sid, status="starting", web_token=web_token, error="")
            gw_token = tokens.sign(self.cfg.token_secret, s["owner"], sid, self.cfg.token_ttl)
            spec = SandboxSpec(sid, s["owner"], web_token, gw_token, resume=bool(s["started"]))
            try:
                endpoint = self.launcher.start(spec)
            except LaunchError as e:
                self.store.update(sid, status="failed", endpoint="", error=str(e)[:500])
                raise LimitError(502, f"沙箱启动失败：{e}") from None
            now = time.time()
            self.store.update(sid, status="running", endpoint=endpoint, started=now, last_active=now)
            return self.store.get(sid)

    def create(self, owner: str, title: str) -> dict:
        with self._admission:
            self.check_capacity(owner)
        s = self.store.create(owner, title or "新会话")
        return self.ensure_running(s["id"])

    def stop(self, sid: str, status: str = "stopped") -> None:
        with self._lock_for(sid):
            self.launcher.stop(sid)
            self.store.update(sid, status=status, endpoint="")

    def delete(self, sid: str) -> None:
        with self._lock_for(sid):
            self.launcher.delete(sid)
            self.store.update(sid, status="deleted", endpoint="", web_token="")

    def sandbox_state(self, s: dict) -> dict | None:
        req = urllib.request.Request(s["endpoint"] + "/api/state",
                                     headers={"x-nanocode-token": s["web_token"], "Host": "localhost"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def reap_once(self, now: float | None = None) -> list[str]:
        """回收空闲的沙箱，返回回收了哪些会话。

        空闲 = 超过 idle_timeout 没有经过控制面的请求，并且 Agent 不在工作、没有排队的消息。
        打开着的页面（只连着事件流）不算活动：否则开着标签页就永远不回收。页面之后再操作时会自动重新启动。
        沙箱存活时间接近网关令牌的有效期时也回收，下次访问会带着新令牌重新启动。
        """
        now = time.time() if now is None else now
        reaped = []
        for s in self.store.active():
            if s["status"] != "running":
                continue
            idle = now - s["last_active"] > self.cfg.idle_timeout
            expiring = s["started"] and now > s["started"] + self.cfg.token_ttl - 300
            if not (idle or expiring):
                continue
            state = self.sandbox_state(s)
            if state and (state.get("busy") or state.get("queue")) and not expiring:
                self.store.touch(s["id"])      # 还在干活，不算空闲
                continue
            self.stop(s["id"])
            reaped.append(s["id"])
        return reaped

    def reap_forever(self, interval: float = 60) -> None:
        while not self._stop.wait(interval):
            try:
                self.reap_once()
            except Exception as e:  # noqa: BLE001 —— 回收线程不能因为一次异常就停掉
                print(f"回收沙箱出错：{type(e).__name__}: {e}")

    def usage(self, owner: str) -> dict | None:
        base = self.cfg.gateway_admin_url or self.cfg.gateway_url
        u = urlparse(base)
        url = f"{u.scheme}://{u.netloc}/internal/usage?owner={urllib.request.quote(owner)}"
        req = urllib.request.Request(url, headers={"authorization": f"Bearer {self.cfg.token_secret}"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def shutdown(self) -> None:
        self._stop.set()


def public_view(s: dict) -> dict:
    """返回给浏览器的会话信息：不包含沙箱地址和令牌。"""
    return {k: s[k] for k in ("id", "title", "status", "created", "last_active", "error")} | {"url": f"/s/{s['id']}/"}


def make_handler(cp: ControlPlane):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        # ------------------------------------------------------------ 响应

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("referrer-policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"ok": False, "error": message}, status)

        # ------------------------------------------------------------ 校验

        def _user(self) -> str | None:
            try:
                return authenticate(self.headers, self.client_address[0], cp.cfg)
            except AuthError as e:
                self._error(401, str(e))
                return None

        def _local_host(self) -> bool:
            """开发模式下 Host 必须是本机地址：否则恶意网页可以用 DNS 重绑定（把自己的域名解析到 127.0.0.1）
            冒充开发者驱动 Agent。生产模式靠认证代理的共享密钥，网页拿不到它。"""
            host = (self.headers.get("host") or "").strip().lower()
            host = host[1:].split("]", 1)[0] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            return host in ("127.0.0.1", "localhost", "::1")

        def _same_origin(self) -> bool:
            """跨站请求伪造防护：带 Origin 的请求必须来自本站。转发给沙箱的请求也先在这里挡一道。"""
            origin = self.headers.get("origin")
            if not origin:
                return True
            return urlparse(origin).netloc.lower() == (self.headers.get("host") or "").lower()

        def _read_body(self) -> bytes | None:
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                self._error(413, "请求体过大")
                return None
            return self.rfile.read(n) if n else b""

        def _owned(self, sid: str, user: str) -> dict | None:
            s = cp.store.get(sid) if SID.match(sid) else None
            if not s or s["owner"] != user:
                self._error(404, "会话不存在")      # 别人的会话也说不存在，不暴露它是否存在
                return None
            return s

        # ------------------------------------------------------------ 路由

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method: str) -> None:
            path = urlparse(self.path).path
            if path == "/healthz":
                return self._json({"ok": True})
            if cp.cfg.dev_user and not cp.cfg.proxy_secret and not self._local_host():
                return self._error(403, "开发模式只接受 Host 为本机地址的请求（防 DNS 重绑定）")
            user = self._user()
            if user is None:
                return
            if method == "POST" and not self._same_origin():
                return self._error(403, "跨站请求被拒绝")
            if path.startswith("/s/"):
                return self._proxy(method, user)
            if method == "GET" and path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", {"cache-control": "no-store"})
            if method == "GET" and path == "/api/me":
                return self._json({"ok": True, "user": user, "usage": cp.usage(user),
                                   "limits": {"sessions": cp.cfg.max_sessions_per_user, "idle_timeout": cp.cfg.idle_timeout}})
            if path == "/api/sessions":
                if method == "GET":
                    return self._json({"ok": True, "sessions": [public_view(s) for s in cp.store.list(user)]})
                return self._create(user)
            m = re.match(r"^/api/sessions/([^/]+)/(stop|delete)$", path)
            if m and method == "POST":
                if not self._is_json():
                    return self._error(415, "只接受 application/json")
                if self._read_body() is None:
                    return
                if not self._owned(m.group(1), user):
                    return
                (cp.stop if m.group(2) == "stop" else cp.delete)(m.group(1))
                return self._json({"ok": True})
            self._error(404, "没有这个接口")

        def _is_json(self) -> bool:
            # 跨站的表单只能发 text/plain 之类的"简单请求"；要发 JSON 必须先预检，而我们不响应预检
            return (self.headers.get("content-type") or "").split(";")[0].strip().lower() == "application/json"

        def _create(self, user: str) -> None:
            if not self._is_json():
                return self._error(415, "只接受 application/json")
            raw = self._read_body()
            if raw is None:
                return
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                data = {}
            title = str(data.get("title", "")).strip() if isinstance(data, dict) else ""
            try:
                s = cp.create(user, title)
            except LimitError as e:
                return self._error(e.status, str(e))
            self._json({"ok": True, "session": public_view(s)}, 201)

        # ------------------------------------------------------------ 转发

        def _proxy(self, method: str, user: str) -> None:
            u = urlparse(self.path)
            m = re.match(r"^/s/([^/]+)(/.*)?$", u.path)
            sid, rest = m.group(1), m.group(2)
            if not self._owned(sid, user):
                return
            if rest is None:      # /s/<id> → /s/<id>/，页面里的相对路径才对
                return self._send(302, b"", "text/plain", {"location": f"/s/{sid}/"})
            body = b""
            if method == "POST":
                raw = self._read_body()
                if raw is None:
                    return
                body = raw
            try:
                s = cp.ensure_running(sid)
            except LimitError as e:
                return self._error(e.status, str(e))
            is_events = rest == "/api/events"
            if not is_events:
                cp.store.touch(sid)
            target = rest + (f"?{u.query}" if u.query else "")
            headers = {k: self.headers[k] for k in FORWARD_REQUEST_HEADERS if self.headers.get(k)}
            headers.update({"Host": "localhost", "x-nanocode-token": s["web_token"]})
            ep = urlparse(s["endpoint"])
            conn = http.client.HTTPConnection(ep.hostname, ep.port, timeout=120 if is_events else 60)
            try:
                conn.request(method, target, body=body if method == "POST" else None, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                cp.store.update(sid, status="stopped", endpoint="")     # 沙箱没了，下次访问会重新启动
                return self._error(502, f"沙箱没有响应，刷新页面会重新启动：{type(e).__name__}")
            if resp.status == 403:
                # 我们自己生成的令牌被拒绝：这个地址上已经不是这个会话的沙箱了（例如 Pod IP 被别的 Pod 复用）
                conn.close()
                cp.store.update(sid, status="stopped", endpoint="")
                return self._error(502, "沙箱已经不在了，刷新页面会重新启动")
            try:
                ctype = resp.getheader("content-type") or "application/octet-stream"
                if "text/event-stream" in ctype:
                    self.send_response(resp.status)
                    self.send_header("content-type", ctype)
                    self.send_header("cache-control", "no-cache")
                    self.send_header("x-accel-buffering", "no")     # 告诉 nginx 之类的代理不要缓冲事件流
                    self.end_headers()
                    while True:
                        line = resp.readline()   # 不能读 resp.fp：那会绕过 HTTP/1.1 的分块解码
                        if not line:
                            break
                        self.wfile.write(line)
                        if line in (b"\n", b"\r\n"):
                            self.wfile.flush()
                else:
                    data = resp.read()
                    extra = {k: resp.getheader(k) for k in FORWARD_RESPONSE_HEADERS[1:] if resp.getheader(k)}
                    self._send(resp.status, data, ctype, extra)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                conn.close()

    return Handler


def make_control_server(cfg: Config, launcher: Launcher | None = None) -> tuple[ThreadingHTTPServer, ControlPlane]:
    cfg.check_control()
    cp = ControlPlane(cfg, launcher)
    server = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(cp))
    server.daemon_threads = True
    return server, cp


__all__ = ["ControlPlane", "make_control_server", "LimitError", "public_view"]

"""本地 Web 界面：聊天 + Agent 内部状态调试面板。

只用标准库 http.server 实现。Web 界面同样只是事件总线的订阅者：
浏览器通过 SSE（/api/events）实时接收事件，看到的内容和 CLI 完全一样，
只是多了"透视"能力 —— 可以看到每一步发给模型的完整 messages、系统提示词、工具定义。

接口：
    GET  /                  聊天 + 调试面板（登录启用时需先登录，否则 302 到 /login）
    GET  /login             登录页（自带登录启用时；已登录则跳回 /）
    GET  /learn             交互式架构学习页
    GET  /api/state         会话概况
    GET  /api/sessions      会话列表（含各会话是否在运行 / 排队 / 等确认）
    GET  /api/context       完整上下文：system、messages、tools（就是下一次要发给模型的东西）
    GET  /api/messages      恢复页面用：完整 messages、事件缓冲的最新序号、从 messages 折出来的时间线
    GET  /api/events        SSE 事件流（连接时先补发历史事件）。浏览器用 ?ticket= 一次性票据；
                            断线重连时用 Last-Event-ID 请求头或 ?after=序号，只补发之后的事件
    POST /api/ticket        换一张 30 秒有效、只能用一次的 SSE 票据
    POST /api/chat          {"message": "..."} 发送消息（也支持 /命令 和 # 记忆）
    POST /api/permission    {"id": "...", "decision": "yes|always|no"}
    POST /api/cancel        中断这个会话的任务，并清空它排队中的消息；{"all": true} 停掉所有会话
    POST /api/session       {"action": "new"} 开新会话 / {"action": "load", "id": "..."} 打开已有会话
    POST /api/login         {"username": "...", "password": "..."} 登录（启用自带登录时）
    POST /api/logout        退出登录（清会话 Cookie）

多会话（#106，见 runtime.py）：会话相关的接口按请求头 X-Nanocode-Session（事件流用 ?sid=）找会话，
    不带就用默认会话（旧客户端、控制面转发）。不同会话可以同时运行，上限见 limits.py。

/api/chat 有限流（issue #7，见 limits.py）：超限返回 429 + Retry-After 头，
    {"ok": false, "error": "...", "code": "rate_limited|queue_full|daily_budget", "retry_after": 秒}

令牌默认每次启动随机生成；多用户部署时由控制面通过环境变量 NANOCODE_WEB_TOKEN 注入，
控制面在转发请求时带上它（见 docs/12；平台代码在私有仓库）。

安全：所有请求校验 Host / Origin，/api/* 需要令牌（页面加载时注入，只认请求头），
POST 只接受 JSON 且请求体不超过 1MB。
自带登录（替代反向代理 Basic Auth 原生弹窗）：设环境变量 NANOCODE_HTPASSWD 指向
htpasswd(apr1) 文件即启用，GET /login 出登录页，会话 Cookie 30 天滑动续期，
HttpOnly + Secure + SameSite=Lax，登录接口按客户端限流防爆破。不设则保持旧行为。
详见 Handler._guard 的说明。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..messages import steps_of_turn
from ..session import Session
from .limits import Limits, RateLimited, env_int
from .runtime import BUSY_ALLOWED, READ_ONLY_COMMANDS, SessionManager, valid_session_id  # noqa: F401（兼容旧导入）

STATIC = os.path.join(os.path.dirname(__file__), "static")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 1024 * 1024          # POST 请求体上限（字节）
TICKET_TTL = 30                 # SSE 一次性票据的有效期（秒）
MAX_TICKETS = 64                # 同时存在的未使用票据上限，防止被刷爆内存
SSE_QUEUE_SIZE = 256            # 每个 SSE 客户端最多缓存的事件数；慢客户端满队列后断开重连
SSE_WRITE_TIMEOUT = 30          # 写入阻塞超过此时间就断开慢客户端

# ---- 自带登录页（替代反向代理 Basic Auth 的原生弹窗，手机上好操作）----
# 启用条件：环境变量 NANOCODE_HTPASSWD 指向一个可读的 htpasswd 文件（apr1 格式）。
# 不设（本地开发默认）则保持旧行为：无登录页，直接进应用。对外暴露时务必启用，
# 否则任何能访问到端口的人都能拿到页面里的令牌、驱动 Agent 执行命令。
SESSION_COOKIE = "nc_session"
SESSION_TTL = 30 * 24 * 3600    # 会话有效期 30 天，每次请求滑动续期
LOGIN_MAX_FAILS = 5             # 同一客户端 10 分钟内最多失败 5 次，之后 429
LOGIN_FAIL_WINDOW = 600
LOGIN_FAIL_MAX_KEYS = 10000     # 失败记录最多记多少个客户端，防伪造 key 撑爆内存
MAX_PASSWORD_LEN = 1024         # 密码长度上限（字节），防超大输入拖慢 openssl
_APR1_SALT_RE = re.compile(r"[./0-9A-Za-z]{1,8}")

def htpasswd_file() -> str | None:
    """登录凭据文件路径；不存在或不可读则返回 None（此时登录功能关闭）。

    文件格式沿用 htpasswd 的 apr1 行：`用户名:$apr1$盐$哈希`。部署时由 root
    从 nginx 的 htpasswd 拷贝一份给 nanocode 用户（600 权限），密码不用变。
    """
    p = os.environ.get("NANOCODE_HTPASSWD", "")
    if p and os.path.isfile(p) and os.access(p, os.R_OK):
        return p
    return None


def verify_password(username: str, password: str) -> bool:
    """对照 htpasswd(apr1) 校验用户名密码。

    为什么不用 Python 算 apr1：标准库没有 MD5-crypt，直接调 openssl。
    密码只经 stdin 传给 openssl，不出现在命令行（ps 看不到）；结果用
    hmac.compare_digest 比对。用户名/密码任一对不上都返回 False，不区分，
    防止枚举用户名。文件缺失、格式错误、openssl 不可用一律返回 False。
    """
    if not username or not password:
        return False
    pw = password.encode("utf-8")[:MAX_PASSWORD_LEN]
    path = htpasswd_file()
    if path is None:
        return False
    try:
        with open(path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(b"#"):
                    continue
                user, sep, hashed = line.partition(b":")
                if not sep:
                    continue
                if user.decode("utf-8", "ignore") != username:
                    continue
                parts = hashed.decode("ascii", "ignore").split("$")
                # 形如 $apr1$盐$哈希；只认 apr1，别的不认
                if len(parts) != 4 or parts[1] != "apr1":
                    return False
                salt = parts[2]
                if not _APR1_SALT_RE.fullmatch(salt):
                    return False
                proc = subprocess.run(
                    ["openssl", "passwd", "-apr1", "-salt", salt, "-stdin"],
                    input=pw, capture_output=True, timeout=10)
                if proc.returncode != 0:
                    return False
                return hmac.compare_digest(proc.stdout.strip(), hashed.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return False


def normalize_host(value: str) -> str:
    """把用户写的可信公网地址规范成 Host 头里"不带端口的主机名"（小写）。

    容忍写成 https://Host:8443 或 host:8443；通配符一律拒绝：这个白名单是防 DNS 重绑定的，
    "*" 等于没有白名单。
    """
    v = value.strip()
    if "://" in v:
        v = urlparse(v).hostname or ""
    v = v.strip("[]").lower()
    if v.count(":") == 1:          # host:port（IPv6 字面量有多个冒号，原样保留）
        v = v.rsplit(":", 1)[0]
    if not v or "*" in v or " " in v or "/" in v:
        raise ValueError(f"无效的公网地址：{value!r}（要写主机名或 IP，不支持通配符）")
    return v


def host_from_header(value: str) -> str:
    """取出 Host 头里不带端口的主机名（小写）。

    [::1]:8765 → ::1；[2001:db8::1]（默认端口，没有端口）→ 2001:db8::1；example.com:8443 → example.com。
    不能一律按最后一个冒号切：IPv6 地址自己就带冒号。
    """
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    if v.count(":") == 1:
        return v.rsplit(":", 1)[0]
    return v


def normalize_origin(value: str) -> str:
    v = value.strip().rstrip("/")
    u = urlparse(v)
    if u.scheme not in ("http", "https") or not u.hostname or u.path or "*" in v:
        raise ValueError(f"无效的 Origin：{value!r}（写成 https://主机名[:端口]，不支持通配符）")
    return v.lower()


def split_list(values) -> list[str]:
    """命令行参数（可重复）和环境变量（逗号分隔）都能给多个值。"""
    out = []
    for v in values or []:
        out += [x.strip() for x in str(v).split(",") if x.strip()]
    return out


def public_settings(hosts=None, origins=None) -> tuple[list[str], list[str]]:
    """合并命令行参数和环境变量 NANOCODE_PUBLIC_HOST / NANOCODE_PUBLIC_ORIGIN。"""
    h = split_list(hosts) + split_list([os.environ.get("NANOCODE_PUBLIC_HOST", "")])
    o = split_list(origins) + split_list([os.environ.get("NANOCODE_PUBLIC_ORIGIN", "")])
    return h, o


def friendly_error(e: Exception) -> str:
    """把技术异常翻译成用户能看懂的中文。保留原始信息供排查，但第一句必须是人话。"""
    from nanocode.llm.base import LLMError, QuotaExhausted
    raw = f"{type(e).__name__}: {e}"
    if isinstance(e, QuotaExhausted):
        # QuotaExhausted 的 message 里已经带了本地时间的恢复时间
        return f"模型配额用完了，等恢复后再试。\n{raw}"
    if isinstance(e, LLMError):
        status = getattr(e, "status", None)
        if status == 503:
            return f"模型服务现在太忙了，过几分钟再试。\n{raw}"
        if status == 429:
            return f"请求太频繁被限流了，稍后再试。\n{raw}"
        if status and 500 <= status < 600:
            return f"模型服务出错了（{status}），过会儿再试。\n{raw}"
        if "网络错误" in str(e):
            return f"连不上模型服务，检查一下网络。\n{raw}"
        return f"模型调用失败。\n{raw}"
    if isinstance(e, (TimeoutError,)):
        return f"请求超时了，网络可能有点慢，重试一次。\n{raw}"
    if isinstance(e, (ConnectionError, OSError)):
        return f"网络连接出问题了，检查一下网络再试。\n{raw}"
    return raw


MIN_TOKEN_LEN = 16


def web_token() -> str:
    """API 令牌：环境变量 NANOCODE_WEB_TOKEN 给了就用它（控制面注入），否则随机生成。太短的不接受。"""
    token = os.environ.get("NANOCODE_WEB_TOKEN", "")
    if not token:
        return secrets.token_urlsafe(24)
    if len(token) < MIN_TOKEN_LEN:
        raise ValueError(f"NANOCODE_WEB_TOKEN 太短（至少 {MIN_TOKEN_LEN} 个字符）")
    return token


def resume_after(headers, query: str) -> int:
    """断线重连时浏览器已经收到的最后一个事件序号：标准的 Last-Event-ID 请求头优先，其次 ?after=。"""
    raw = headers.get("last-event-id") or (parse_qs(query).get("after") or [""])[0]
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


class WebApp:
    def __init__(self, session: Session, permission_timeout: float | None = None, disconnect_grace: float = 30):
        self.token = web_token()
        if permission_timeout is None:
            permission_timeout = float(os.environ.get("NANOCODE_PERMISSION_TIMEOUT") or 600)
        self.permission_timeout = permission_timeout
        # 权限询问期间如果一个浏览器都没连着（页面关了），宽限这么多秒后按拒绝处理
        self.disconnect_grace = disconnect_grace
        self.sse_clients = 0
        self._sse_lock = threading.Lock()
        # 事件流空闲时每隔这么多秒发一次心跳；服务端也是靠写心跳才发现浏览器已断开的
        self.sse_ping_interval = 15
        self.sse_queue_size = SSE_QUEUE_SIZE
        self.sse_write_timeout = SSE_WRITE_TIMEOUT
        self._tickets: dict[str, float] = {}   # 一次性 SSE 票据 → 过期时间
        self.allowed_hosts: set[str] = set(LOCAL_HOSTS)
        self.allowed_origins: set[str] = set()
        self.limits = Limits()          # 限流与每日预算（issue #7）
        # 多会话（#106）：每个会话自己的排队、忙碌标志、权限询问都在 SessionRuntime 里
        self.sessions = SessionManager(self, session, max_live=env_int("NANOCODE_WEB_MAX_LIVE_SESSIONS", 8))
        # ---- 自带登录的会话状态 ----
        # 登录功能是否启用：凭据文件存在才启用，否则保持旧行为（无登录）。
        self.login_enabled = htpasswd_file() is not None
        self._sessions: dict[str, float] = {}   # 会话令牌 -> 过期时间戳
        self._session_lock = threading.Lock()
        self._login_fails: dict[str, list[float]] = {}  # client_key -> 失败时间戳

    # ---- 兼容：以前 WebApp 只有一个会话，这些属性都指向默认会话 ----
    @property
    def session(self) -> Session:
        return self.sessions.default.session

    @property
    def busy(self) -> bool:
        return self.sessions.default.busy

    @property
    def queue(self) -> list[str]:
        return self.sessions.default.queue

    @property
    def pending(self) -> dict:
        return self.sessions.default.pending

    @property
    def _turn_active(self) -> bool:
        return self.sessions.default._turn_active

    @_turn_active.setter
    def _turn_active(self, value: bool) -> None:
        self.sessions.default._turn_active = value

    @property
    def _run(self):
        return self.sessions.default._run

    @_run.setter
    def _run(self, fn) -> None:
        self.sessions.default._run = fn

    def ask(self, name: str, input: dict, target: str) -> str:
        return self.sessions.default.ask(name, input, target)

    def chat(self, message: str, client: str = "local") -> dict:
        return self.sessions.default.chat(message, client)

    def issue_ticket(self) -> str | None:
        """签发一张 SSE 用的一次性票据，TICKET_TTL 秒内有效、只能用一次。

        EventSource 发不出自定义请求头，令牌只能放进 URL，而 URL 会被反向代理、监控系统记进日志。
        所以 URL 里只放这种用完即废的票据，长期有效的令牌始终只走请求头。
        """
        now = time.time()
        with self._sse_lock:
            for t in [t for t, exp in self._tickets.items() if exp < now]:
                del self._tickets[t]
            if len(self._tickets) >= MAX_TICKETS:
                return None
            ticket = secrets.token_urlsafe(16)
            self._tickets[ticket] = now + TICKET_TTL
            return ticket

    def redeem_ticket(self, ticket: str) -> bool:
        with self._sse_lock:
            exp = self._tickets.pop(ticket, None)
        return exp is not None and exp >= time.time()

    # ---- 登录会话（自带登录页启用时才用）----
    def create_session(self) -> str:
        """登录成功后签发会话令牌。每次登录都是新令牌，不存在会话固定问题。"""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._session_lock:
            self._prune_sessions(now)
            self._sessions[token] = now + SESSION_TTL
        return token

    def check_session(self, token: str) -> bool:
        """校验会话 Cookie；有效则滑动续期。"""
        if not token:
            return False
        now = time.time()
        with self._session_lock:
            exp = self._sessions.get(token)
            if exp is None or exp < now:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = now + SESSION_TTL
            return True

    def destroy_session(self, token: str) -> None:
        with self._session_lock:
            self._sessions.pop(token, None)

    def _prune_sessions(self, now: float) -> None:
        for t, exp in [i for i in self._sessions.items() if i[1] < now]:
            del self._sessions[t]

    def login_allowed(self, key: str) -> tuple[bool, int]:
        """登录限流：同一客户端 10 分钟内失败 5 次就 429。返回 (是否允许, 需等待秒数)。"""
        now = time.time()
        with self._session_lock:
            fails = [t for t in self._login_fails.get(key, []) if now - t < LOGIN_FAIL_WINDOW]
            # 键太多时不给新 key 建空条目（它没有失败历史，本来也不会被限）
            if fails or key in self._login_fails or len(self._login_fails) < LOGIN_FAIL_MAX_KEYS:
                self._login_fails[key] = fails
            if len(fails) >= LOGIN_MAX_FAILS:
                return False, int(LOGIN_FAIL_WINDOW - (now - fails[0])) + 1
            return True, 0

    def login_failed(self, key: str) -> None:
        now = time.time()
        with self._session_lock:
            if key not in self._login_fails and len(self._login_fails) >= LOGIN_FAIL_MAX_KEYS:
                # 先清掉已过期的失败记录腾地方
                for k in [k for k, v in self._login_fails.items()
                          if not v or now - v[-1] >= LOGIN_FAIL_WINDOW]:
                    del self._login_fails[k]
            self._login_fails.setdefault(key, []).append(now)

    def login_succeeded(self, key: str) -> None:
        with self._session_lock:
            self._login_fails.pop(key, None)

    def configure(self, host: str, port: int, public_hosts=(), public_origins=()) -> None:
        """绑定端口后调用：确定哪些 Host / Origin 是"自己人"。

        默认只认本机地址。放在反向代理后面时，用 public_hosts / public_origins 显式加上对外的地址：
          - public_hosts：对外的主机名或 IP（浏览器访问的、反向代理原样转发的 Host）。
            同时自动信任该主机名的 http:// 和 https://（默认端口）Origin。
          - public_origins：完整的 Origin，用于非默认端口等情况，例如 https://example.com:8443。
        这只是校验"请求是不是发给我们的"，不能代替认证：反向代理必须自己做 TLS 和登录（如 basic auth）。
        """
        hosts = set(LOCAL_HOSTS)
        if host not in ("0.0.0.0", "::", ""):
            hosts.add(host.lower())
        origins = {f"http://{h}:{port}" for h in hosts} | {f"http://[{h}]:{port}" for h in hosts if ":" in h}
        for h in public_hosts:
            h = normalize_host(h)
            hosts.add(h)
            shown = f"[{h}]" if ":" in h else h
            origins |= {f"http://{shown}", f"https://{shown}"}
        for o in public_origins:
            origins.add(normalize_origin(o))
        self.allowed_hosts = hosts
        self.allowed_origins = origins


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 安静一点
            pass

        def _json(self, data, status=200, headers=()):
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, name, content_type="text/html; charset=utf-8"):
            path = os.path.join(STATIC, name)
            with open(path, "rb") as f:
                body = f.read().replace(b"__NANOCODE_TOKEN__", app.token.encode())
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            # 面板里会渲染模型输出、仓库内容、hook 输出。万一有漏转义的注入点，
            # 这两条响应头能把"偷到 token → 冒充用户发命令"的链条挡住：
            # CSP 拦住 script-src 之外的脚本执行，X-Frame-Options 挡住点击劫持。
            # 'unsafe-inline' 是给页面里已有的内联 <script> 留的口子（源码无构建步骤，改不了 nonce）。
            # 不放行任何外部域：marked 与链接清洗脚本都从 /static 本地加载（#90）。
            self.send_header("content-security-policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("x-frame-options", "DENY")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("referrer-policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict | None:
            """读请求体。出错时已经回复了错误，返回 None，调用方直接结束。

            Content-Length 是对方说了算的：不设上限就会按它声称的大小一次性分配内存（声称 1.5GB，
            进程虚拟内存立刻涨 1.5GB），写个非数字或负数还会让处理线程直接崩掉、连接无响应。
            聊天消息很短，1MB 足够宽裕。
            """
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                self.close_connection = True
                self._reject(400, "Content-Length 无效")
                return None
            if n > MAX_BODY:
                self.close_connection = True   # 没读的请求体还留在连接里，不能再当成下一个请求解析
                self._reject(413, f"请求体过大（上限 {MAX_BODY} 字节）")
                return None
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _reject(self, status: int, reason: str) -> None:
            self._json({"ok": False, "error": reason}, status)

        def _limited(self, e: RateLimited) -> None:
            headers = [("Retry-After", str(e.retry_after))] if e.retry_after else []
            # 忙时拒绝斜杠命令用 409（冲突：当前有任务在跑），和限流类 429 区分开，
            # 前端才能给出"等它做完"而不是"发太频繁"的提示。
            status = 409 if e.code == "busy" else 429
            self._json({"ok": False, "error": e.message, "code": e.code, "retry_after": e.retry_after},
                       status, headers)

        def _client_key(self) -> str:
            """限流按客户端区分。X-Forwarded-For 谁都能伪造，只在 NANOCODE_TRUST_PROXY=1（确实挂了反代）时才信。"""
            if app.limits.trust_proxy:
                xff = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                if xff:
                    return xff
            return self.client_address[0]

        def _rt(self):
            """这个请求操作的是哪个会话：请求头 X-Nanocode-Session，事件流用 ?sid=（EventSource 发不出请求头）。

            都没带就是默认会话。找不到时已经回复了错误，返回 None。
            """
            sid = (self.headers.get("x-nanocode-session") or "").strip()
            if not sid:
                sid = (parse_qs(urlparse(self.path).query).get("sid") or [""])[0].strip()
            try:
                return app.sessions.get(sid)
            except ValueError as e:
                self._json({"ok": False, "error": str(e), "code": "bad_session"}, 400)
            except LookupError as e:
                self._json({"ok": False, "error": str(e), "code": "no_session"}, 404)
            return None

        def _cookie_token(self) -> str:
            """从 Cookie 头里取登录会话令牌。"""
            cookie = self.headers.get("cookie") or ""
            for part in cookie.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name == SESSION_COOKIE:
                    return value.strip().strip('"')
            return ""

        def _session_ok(self) -> bool:
            return app.check_session(self._cookie_token())

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("location", location)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", "0")
            self.end_headers()

        def _set_session_cookie(self, token: str) -> tuple[str, str]:
            # Secure 要求 HTTPS：对外部署时 nginx 本来就终止 TLS；本地明文调试登录页
            # 可设 NANOCODE_COOKIE_SECURE=0（默认 1）。
            secure = "; Secure" if os.environ.get("NANOCODE_COOKIE_SECURE", "1") == "1" else ""
            return ("set-cookie",
                    f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL};"
                    f" HttpOnly{secure}; SameSite=Lax")

        def _clear_session_cookie(self) -> tuple[str, str]:
            secure = "; Secure" if os.environ.get("NANOCODE_COOKIE_SECURE", "1") == "1" else ""
            return ("set-cookie",
                    f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly{secure}; SameSite=Lax")

        def _guard(self, path: str, method: str) -> bool:
            """所有请求先过这道检查，返回 False 表示已经拒绝。

            本地 Web 服务的三种典型攻击和对应防线：
              1. DNS 重绑定：恶意域名解析到 127.0.0.1，浏览器认为是"同源"，能读到页面里的令牌。
                 → 校验 Host 头必须是本机地址。所有请求都要校验，包括首页。
              2. 跨站请求伪造（CSRF）：恶意网页直接 POST 到 127.0.0.1。浏览器对 text/plain 这类
                 "简单请求"不做预检，请求会照常发出。
                 → POST 只接受 application/json（跨站发 JSON 必须先预检，而我们不响应预检）；
                   Origin 头存在时必须是本服务自己。
              3. 其他网页或本机程序猜接口。→ /api/* 必须带上启动时随机生成的令牌。
               4. 自带登录启用时（NANOCODE_HTPASSWD 指向可读的 htpasswd 文件）：/login 与
                  /api/login 公开，其余一律要有效的登录会话 Cookie。页面请求没会话就 302
                  到 /login，API 请求没会话就 401。未启用时保持旧行为（无登录）。
            """
            host = host_from_header(self.headers.get("host") or "")
            if host not in app.allowed_hosts:
                self._reject(403, "Host 不被允许（防 DNS 重绑定）")
                return False
            origin = self.headers.get("origin")
            if origin and origin.rstrip("/") not in app.allowed_origins:
                self._reject(403, "Origin 不被允许（防跨站请求）")
                return False
            if app.login_enabled:
                if path == "/login" or path == "/api/login":
                    if path.startswith("/api/") and method == "POST" and \
                            (self.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                        self._reject(415, "只接受 application/json")
                        return False
                    return True
                if not self._session_ok():
                    if path.startswith("/api/"):
                        self._reject(401, "未登录，请先登录")
                    else:
                        self._redirect("/login")
                    return False
            if not path.startswith("/api/"):
                return True
            if method == "POST" and (self.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                self._reject(415, "只接受 application/json")
                return False
            # 长期令牌只认请求头，不认 URL 参数（URL 会进代理日志）。
            # 唯一的例外：事件流 /api/events 可以用一次性票据（?ticket=），因为 EventSource 发不出请求头。
            token = self.headers.get("x-nanocode-token") or ""
            if hmac.compare_digest(token, app.token):
                return True
            if method == "GET" and path == "/api/events":
                ticket = (parse_qs(urlparse(self.path).query).get("ticket") or [""])[0]
                if ticket and app.redeem_ticket(ticket):
                    return True
            self._reject(403, "缺少或错误的令牌")
            return False

        def do_GET(self):
            path = self.path.split("?")[0]
            if not self._guard(path, "GET"):
                return
            rt = s = None
            if path in ("/api/state", "/api/messages", "/api/context", "/api/extensions", "/api/events",
                        "/api/sessions"):
                rt = self._rt()
                if rt is None:
                    return
                s = rt.session
            if path in ("/", "/index.html"):
                return self._file("index.html")
            if path == "/login":
                if not app.login_enabled:
                    return self._redirect("/")
                if self._session_ok():
                    return self._redirect("/")
                return self._file("login.html")
            if path == "/favicon.svg":
                return self._file("favicon.svg", "image/svg+xml")
            if path == "/marked.umd.js":
                return self._file("marked.umd.js", "text/javascript; charset=utf-8")
            if path == "/markdown-safe.js":
                return self._file("markdown-safe.js", "text/javascript; charset=utf-8")
            if path in ("/learn", "/learn.html"):
                return self._file("learn.html")
            if path == "/api/state":
                info = s.info()
                info["session_id"] = s.session_id
                info["queue"] = len(rt.queue)
                # Agent 跑完一轮到出队之间 agent.busy 已经是 False，直接用它会闪一下"空闲"。
                # 界面要的是"这一轮工作还没结束"，所以以本层标志为准。
                info["busy"] = rt.busy
                info["default_session"] = app.sessions.default.sid
                # 同一工作目录里另外还有几个会话在跑：界面据此提醒可能互相改文件
                info["parallel"] = sum(1 for r in app.sessions.live() if r is not rt and r.busy)
                return self._json(info)
            if path == "/api/messages":
                # 刷新页面后恢复聊天记录（#86）。messages 是权威来源，不靠 SSE 重放拼对话：
                #   last_seq —— 事件缓冲区里最新的序号，前端把 <= 它的事件当"历史事件"，
                #               只用来补右侧面板，不往聊天区再加一遍；
                #   timeline —— 进程重启后事件缓冲区是空的，时间线用 messages 折回来兜底。
                history = s.bus.history
                return self._json({"ok": True, "messages": s.agent.messages,
                                   "last_seq": history[-1]["seq"] if history else 0,
                                   "timeline": steps_of_turn(s.agent.messages)})
            if path == "/api/context":
                return self._json({"system": s.agent.system_prompt(), "messages": s.agent.messages,
                                   "tools": s.registry.schemas(), "permissions": {
                                       "mode": s.permissions.mode, "allow": s.permissions.allow + s.permissions.session_allow,
                                       "deny": s.permissions.deny}})
            if path == "/api/extensions":
                return self._json(s.extensions())
            if path == "/api/events":
                return self._sse(rt)
            if path == "/api/sessions":
                return self._json({"ok": True, "sessions": app.sessions.list_sessions(),
                                   "current": rt.sid, "default": app.sessions.default.sid})
            self.send_error(404)

        def do_POST(self):
            path = self.path.split("?")[0]
            if not self._guard(path, "POST"):
                return
            data = self._body()
            if data is None:
                return
            if path == "/api/login":
                # 未启用自带登录时这个接口不存在（启用时 _guard 已公开放行；
                # 未启用时带令牌也能走到这里，所以要显式 404）。
                if not app.login_enabled:
                    self.send_error(404)
                    return
                key = self._client_key()
                allowed, wait = app.login_allowed(key)
                if not allowed:
                    return self._json(
                        {"ok": False, "error": f"尝试太频繁，请 {wait} 秒后再试"}, 429)
                username = str(data.get("username", ""))[:64]
                password = str(data.get("password", ""))
                # 用户名/密码对错都回同一个错误，不给枚举用户名的机会
                if verify_password(username, password):
                    app.login_succeeded(key)
                    token = app.create_session()
                    return self._json({"ok": True},
                                      headers=[self._set_session_cookie(token)])
                app.login_failed(key)
                return self._json({"ok": False, "error": "用户名或密码错误"}, 401)
            if path == "/api/logout":
                token = self._cookie_token()
                if token:
                    app.destroy_session(token)
                return self._json({"ok": True}, headers=[self._clear_session_cookie()])
            if path in ("/api/chat", "/api/cancel", "/api/permission", "/api/session"):
                rt = self._rt()
                if rt is None:
                    return
                explicit = bool(self.headers.get("x-nanocode-session"))
            if path == "/api/chat":
                try:
                    result = rt.chat(str(data.get("message", "")), self._client_key())
                except RateLimited as e:
                    return self._limited(e)
                # /clear 会换新的会话 id：告诉页面，让它更新自己记住的当前会话
                return self._json({**result, "session_id": rt.sid})
            if path == "/api/permission":
                rid = str(data.get("id", ""))
                slot = rt.pending.get(rid)
                if not slot and not explicit:
                    # 旧客户端不带会话 id：在所有会话里找（询问 id 是随机的，不会撞）
                    slot = next((r.pending[rid] for r in app.sessions.live() if rid in r.pending), None)
                if not slot:
                    return self._json({"ok": False, "error": "请求不存在或已过期"}, 404)
                slot["answer"] = data.get("decision") if data.get("decision") in ("yes", "always", "no") else "no"
                slot["event"].set()
                return self._json({"ok": True})
            if path == "/api/cancel":
                # "停止"连排队的一起停，否则队列里的会接着烧额度。只停这个会话，别的会话照跑
                targets = app.sessions.live() if data.get("all") is True else [rt]
                for r in targets:
                    r.cancel()
                return self._json({"ok": True, "session_id": rt.sid})
            if path == "/api/ticket":
                ticket = app.issue_ticket()
                if ticket is None:
                    return self._reject(429, "未使用的票据太多，请稍后再试")
                return self._json({"ok": True, "ticket": ticket, "expires_in": TICKET_TTL})
            if path == "/api/session":
                # 切换会话不再改任何全局状态：别的会话（哪怕正在跑）不受影响，所以忙时也能切（#106）。
                # 新客户端带着 X-Nanocode-Session，"当前会话"由页面自己记住，这里只建 / 找会话并返回 id。
                # 旧客户端不带会话 id：沿用旧语义，把"默认会话"指过去（旧会话照样在后台跑完）。
                action = data.get("action", "")
                if action == "new":
                    target = app.sessions.create()
                    msg = f"已开始新会话 {target.sid}"
                elif action == "load":
                    sid = str(data.get("id", "")).strip()
                    if not valid_session_id(sid):
                        return self._json({"ok": False, "error": "会话 id 非法"})
                    try:
                        target = app.sessions.get(sid)
                    except LookupError as e:
                        return self._json({"ok": False, "error": f"切换失败：{e}"})
                    msg = f"已切换到会话 {target.sid}（{len(target.session.agent.messages)} 条消息）"
                else:
                    return self._json({"ok": False, "error": "未知操作"})
                if not explicit and target is not app.sessions.default:
                    old = app.sessions.default
                    app.sessions.default = target
                    old.session.bus.emit("session_switched", message=msg, session_id=target.sid)
                return self._json({"ok": True, "output": msg, "session_id": target.sid})
            self.send_error(404)

        def _sse(self, rt):
            self.connection.settimeout(app.sse_write_timeout)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            after = resume_after(self.headers, urlparse(self.path).query)
            # 进程重启后序号从 0 重新开始，而浏览器还记着上个进程的序号：这时 after 比本进程
            # 已经发出的任何事件都大，照常过滤的话浏览器再也收不到事件 —— 界面静默卡死（#86）。
            # 序号只会单调递增，所以"客户端比我收到的还多"只可能是换了个进程，按从头补发处理。
            bus = rt.session.bus
            if after > bus.seq:
                after = 0
            q: queue.Queue = queue.Queue(maxsize=app.sse_queue_size)
            overflow = threading.Event()

            def enqueue_event(event):
                if overflow.is_set():
                    return
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # 不让 Agent 的事件发布线程被慢客户端拖住。断开后浏览器会带
                    # Last-Event-ID 重连，历史事件可补发，text_delta 最终由 llm_response 补全。
                    overflow.set()

            unsubscribe = bus.subscribe(enqueue_event)
            # 各会话的运行状态（没有 seq、不进历史），页面据此提示"另有 n 个会话在运行"。
            # 只发给明确要的连接（?status=1），旧的订阅方（控制面、脚本）看到的事件流不变
            want_status = (parse_qs(urlparse(self.path).query).get("status") or [""])[0] == "1"
            unsubscribe_status = app.sessions.subscribe(enqueue_event) if want_status else (lambda: None)
            with app._sse_lock:
                app.sse_clients += 1
                rt.subscribers += 1
            try:
                sent = after
                # 补发历史，刷新页面不丢状态；重连时只补发浏览器还没收到的
                for e in [e for e in list(bus.history) if e["seq"] > after]:
                    self._send_event(e)
                    sent = max(sent, e["seq"])
                if want_status:
                    self._send_event(app.sessions.snapshot())
                while True:
                    if overflow.is_set():
                        raise ConnectionResetError("SSE 客户端事件队列已满")
                    try:
                        e = q.get(timeout=min(app.sse_ping_interval, 1))
                    except queue.Empty:
                        if overflow.is_set():
                            raise ConnectionResetError("SSE 客户端事件队列已满")
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if overflow.is_set():
                        raise ConnectionResetError("SSE 客户端事件队列已满")
                    if "seq" in e and e["seq"] <= sent:   # 订阅之后、补发历史之前产生的事件，已经补发过了
                        continue
                    self._send_event(e)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                unsubscribe()
                unsubscribe_status()
                with app._sse_lock:
                    app.sse_clients -= 1
                    rt.subscribers -= 1

        def _send_event(self, e):
            data = json.dumps(e, ensure_ascii=False, default=str)
            # id 行让浏览器（和控制面的转发）记住最后收到的序号，重连时作为 Last-Event-ID 带回来。
            # 会话状态广播没有序号，也不能带 id 行，否则会把浏览器记的序号冲掉
            idline = f"id: {e['seq']}\n" if "seq" in e else ""
            self.wfile.write(f"{idline}data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


def make_server(session: Session, host: str = "127.0.0.1", port: int = 8765,
                public_hosts=(), public_origins=()) -> tuple[ThreadingHTTPServer, WebApp]:
    app = WebApp(session)
    app.configure(host, port, public_hosts, public_origins)   # 先校验配置，失败就别绑端口了

    class _WebHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind reverse-resolves the bind address via getfqdn;
            # hosted macOS runners can stall there before listen() (issue #92).
            socketserver.TCPServer.server_bind(self)
            self.server_name = self.server_address[0]
            self.server_port = self.server_address[1]

    server = _WebHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.configure(host, server.server_address[1], public_hosts, public_origins)   # 端口 0 时要用真实端口重算
    return server, app


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765,
          public_hosts=(), public_origins=()) -> None:
    server, app = make_server(session, host, port, public_hosts, public_origins)
    port = server.server_address[1]
    if os.environ.get("NANOCODE_WEB_TOKEN"):
        # 平台沙箱：同 uid 的工具子进程不能再读本进程的 /proc/<pid>/environ（#134）
        from ..proc import harden_process
        harden_process()
    if host in ("0.0.0.0", "::"):
        print("警告：服务监听在所有网卡上，局域网内的人只要拿到页面就能操作 Agent。除非你清楚后果，请用默认的 127.0.0.1。")
    if host not in LOCAL_HOSTS and not os.environ.get("NANOCODE_WEB_TOKEN"):
        print("警告：监听的不是本机地址，又没设 NANOCODE_WEB_TOKEN。随机令牌嵌在页面里，谁能打开页面谁就能用，"
              "对外暴露时务必在前面挂登录（反向代理的 basic auth / oauth2-proxy 等）。")
    print(app.limits.describe())
    print(f"nanocode Web 界面：http://{host}:{port}   （学习页：http://{host}:{port}/learn）")
    if public_hosts or public_origins:
        print(f"已信任公网 Host：{', '.join(normalize_host(h) for h in public_hosts) or '（无）'}；"
              f"额外 Origin：{', '.join(normalize_origin(o) for o in public_origins) or '（无）'}")
        print("提醒：这只是校验请求是发给本服务的，不能代替认证。" +
              ("自带登录页已启用（NANOCODE_HTPASSWD）。"
               if htpasswd_file() else
               "对外暴露时务必做登录：设 NANOCODE_HTPASSWD 启用自带登录页，"
               "或在反向代理前挂 basic auth / oauth2-proxy；"
               "否则任何能访问到它的人都能拿到令牌、驱动 Agent 执行命令。"))
    print(f"模型 {session.provider_name}/{session.provider.model} · 工作目录 {session.cwd} · Ctrl-C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        app.sessions.close()
        session.close()

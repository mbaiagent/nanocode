"""本地 Web 界面：聊天 + Agent 内部状态调试面板。

只用标准库 http.server 实现。Web 界面同样只是事件总线的订阅者：
浏览器通过 SSE（/api/events）实时接收事件，看到的内容和 CLI 完全一样，
只是多了"透视"能力 —— 可以看到每一步发给模型的完整 messages、系统提示词、工具定义。

接口：
    GET  /                  聊天 + 调试面板
    GET  /learn             交互式架构学习页
    GET  /api/state         会话概况
    GET  /api/context       完整上下文：system、messages、tools（就是下一次要发给模型的东西）
    GET  /api/events        SSE 事件流（连接时先补发历史事件）。浏览器用 ?ticket= 一次性票据；
                            断线重连时用 Last-Event-ID 请求头或 ?after=序号，只补发之后的事件
    POST /api/ticket        换一张 30 秒有效、只能用一次的 SSE 票据
    POST /api/chat          {"message": "..."} 发送消息（也支持 /命令 和 # 记忆）
    POST /api/permission    {"id": "...", "decision": "yes|always|no"}
    POST /api/cancel        中断当前任务，并清空排队中的消息

/api/chat 有限流（issue #7，见 limits.py）：超限返回 429 + Retry-After 头，
    {"ok": false, "error": "...", "code": "rate_limited|queue_full|daily_budget", "retry_after": 秒}

令牌默认每次启动随机生成；多用户部署时由控制面通过环境变量 NANOCODE_WEB_TOKEN 注入，
控制面在转发请求时带上它（见 docs/12 和 server/）。

安全：所有请求校验 Host / Origin，/api/* 需要令牌（页面加载时注入，只认请求头），
POST 只接受 JSON 且请求体不超过 1MB。
详见 Handler._guard 的说明。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..session import Session
from .limits import DailyCounter, Limits, RateLimited

STATIC = os.path.join(os.path.dirname(__file__), "static")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 1024 * 1024          # POST 请求体上限（字节）
TICKET_TTL = 30                 # SSE 一次性票据的有效期（秒）
MAX_TICKETS = 64                # 同时存在的未使用票据上限，防止被刷爆内存
SSE_QUEUE_SIZE = 256            # 每个 SSE 客户端最多缓存的事件数；慢客户端满队列后断开重连
SSE_WRITE_TIMEOUT = 30          # 写入阻塞超过此时间就断开慢客户端

# Agent 忙时仍然可以执行的斜杠命令：只读查询，不会碰正在跑的那一轮对话。
# 其余命令（/clear /compact /resume /mode、以及会转成提示词的自定义命令）会改掉
# Agent 脚下的状态，必须等这一轮结束 —— 见 chat() 里对 #50 的注释。
READ_ONLY_COMMANDS = {"help", "?", "cost", "context", "tools", "todos", "mcp", "skills", "providers", "memory"}

# 忙时还能改的配置项。/mode 不是只读查询，但它只改权限模式这一个配置：正在跑的那一轮
# 已经判过的工具不会回退重判，新模式从下一次工具调用开始生效，不会动 messages / usage
# 这些 Agent 脚下的状态。以前它和 /clear 一起被 429 拒掉，而前端又不显示这个错误，
# 用户以为切换成功了（#78）。
BUSY_ALLOWED = READ_ONLY_COMMANDS | {"mode"}


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
        self.session = session
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()
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
        self.queue: list[str] = []      # 忙时排队的消息，按先进先出执行
        # 「这个会话有一轮工作在进行」（含排队中的消息），由本类独占维护。
        # 不能用 agent.busy 代替：Agent.run() 在 finally 里会把它清零，而那时 _run
        # 还没开始处理队列，中间的窗口会让新请求误判成「不忙」而并发起第二个 Agent。
        self._turn_active = False
        self.limits = Limits()          # 限流与每日预算（issue #7）
        session.permissions.ask_fn = self.ask

    @property
    def busy(self) -> bool:
        """这个会话是否还有工作没做完（含排队中的消息）。

        以 _turn_active 为准：Agent.run() 一返回就把 agent.busy 清掉了，而从那一刻到
        _run() 处理完队列之间还有一段路，只看 agent.busy 会误判成空闲并放第二个 Agent 进来。
        agent.busy 也一起看，是为了兼容"直接把 agent.busy 置 True 表示忙"的调用方。
        """
        return self._turn_active or self.session.agent.busy

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

    def ask(self, name: str, input: dict, target: str) -> str:
        """权限询问：发事件给浏览器，然后阻塞等待 /api/permission 的回答。

        按拒绝处理的三种情况，Agent 都不会永远卡住（等待期间 Agent 占着全局 busy 锁，别的请求都会被拒）：
          - 用户点了中断
          - 超过 permission_timeout（默认 10 分钟，环境变量 NANOCODE_PERMISSION_TIMEOUT 可改）
          - 没有任何浏览器连着（页面关了）超过 disconnect_grace 秒。刷新页面会很快重连，不受影响。
        """
        rid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "answer": "no"}
        self.pending[rid] = slot
        self.session.bus.emit("permission_request", id=rid, name=name, input=input, target=target)
        deadline = time.time() + self.permission_timeout
        no_client_since = None
        while not slot["event"].wait(0.2):
            now = time.time()
            if self.session.agent.cancel.is_set() or now > deadline:
                break
            if self.sse_clients == 0:
                no_client_since = no_client_since or now
                if now - no_client_since > self.disconnect_grace:
                    break
            else:
                no_client_since = None
        self.pending.pop(rid, None)
        self.session.bus.emit("permission_answer", id=rid, decision=slot["answer"])
        return slot["answer"]

    def chat(self, message: str, client: str = "local") -> dict:
        """处理一条消息。超限时抛 RateLimited，由 Handler 转成 429。

        只有真正要跑模型的消息才扣额度：# 记忆和不产生 prompt 的 /命令 不扣。
        """
        s = self.session
        with self.lock:
            message = message.strip()
            if message.startswith("#"):
                return {"ok": True, "output": s.add_memory(message.lstrip("#"))}
            prompt = message
            output = ""
            if message.startswith("/"):
                # 先取命令名、先判忙，再执行。反过来（先 handle_command 后判忙）会让
                # /clear、/resume、/compact 在 Agent 正在跑的时候改掉它脚下的状态：
                # /clear 清空正在使用的 messages，/resume 换掉整个对话，/compact 在全局锁里
                # 发模型调用还把取消按钮一起堵死（#50）。
                # 纯查询类命令和 /mode 不受影响，忙时照样能用（#78）。
                name = message[1:].strip().partition(" ")[0]
                if self.busy and name not in BUSY_ALLOWED:
                    raise RateLimited("busy", f"Agent 正在工作，等它做完或先中断再用 /{name}", 5)
                output, prompt = s.handle_command(message)
                s.bus.emit("command", input=message, output=output)
                if not prompt:
                    # /compact 虽然不产生 prompt，但它自己会调一次模型做总结，
                    # 必须和普通消息一样过限流与每日预算，否则是个免费刷模型的入口（#50）。
                    if name != "compact":
                        return {"ok": True, "output": output}
            wait = self.limits.bucket.take(client)
            if wait:
                w = max(1, int(wait + 0.999))
                raise RateLimited("rate_limited", f"发送太频繁，请 {w} 秒后再试", w)
            if self.busy:
                # 不设上限的话，连点 100 次就会排进 100 轮完整的 Agent 循环（issue #7）
                if self.limits.max_queue and len(self.queue) >= self.limits.max_queue:
                    raise RateLimited("queue_full",
                                      f"排队已满（{self.limits.max_queue} 条），等当前任务做完或先中断", 5)
                # 忙时不再拒绝：收下排队，当前任务做完自动执行。立即发 user 事件，
                # 让前端马上显示这条消息（带排队标记），实际执行时 agent.run 会再发一次，
                # 前端按文本去重。
                self.queue.append(prompt)
                s.bus.emit("user", agent="main", text=message)
                s.bus.emit("queued", position=len(self.queue))
                return {"ok": True, "queued": True, "position": len(self.queue)}
            if not self.limits.daily_runs.take():
                raise RateLimited("daily_budget", "今天的调用预算已用完，明天再来（或调大 NANOCODE_WEB_DAILY_RUNS）",
                                  DailyCounter.seconds_to_midnight())
            if prompt is None:
                # /compact 已经把事做完了（它自己调过一次模型做总结），这里只补记一次
                # 每日预算，然后结束 —— 不能把 None 当 prompt 起一轮 Agent：
                # 那会往对话里塞一条 text=None 的消息（真实接口直接 400），
                # 还会多发一次模型请求、多扣一次预算（#73）。
                return {"ok": True, "output": output}
            # 两个标志一起置位：_turn_active 是本层的权威判定，agent.busy 让界面立刻反应过来
            self._turn_active = True
            s.agent.busy = True
        threading.Thread(target=self._run, args=(prompt,), daemon=True).start()
        return {"ok": True}

    def _end_turn(self) -> None:
        """这一轮工作彻底结束（队列已空，或排队消息被丢弃）：清掉两个标志。

        调用方必须持有 self.lock。先清 _turn_active 能让紧接着的 chat() 立刻看到空闲；
        agent.busy 在 Agent.run() 结束时已被自己清零，这里再清一次是为了覆盖
        "还没进入 session.send() 就被取消"这类没走到 Agent.run 的路径。
        """
        self._turn_active = False
        self.session.agent.busy = False

    def _run(self, prompt: str) -> None:
        try:
            self._run_queue(prompt)
        finally:
            # 兜底：_turn_active 相当于一把会话锁，任何退出路径都必须放开。
            # 正常收尾时 _end_turn() 已经清掉，这里不会重复发 idle；
            # 只有被中断（BaseException）、连 _run_queue 都没走完的路径才会落到这里。
            with self.lock:
                if self._turn_active:
                    self._end_turn()
                    self.session.bus.emit("idle")

    def _run_queue(self, prompt: str) -> None:
        while True:
            try:
                self.session.send(prompt)
            except Exception as e:  # noqa: BLE001
                self.session.bus.emit("error", agent="main", message=friendly_error(e),
                                      retryable=True, prompt=prompt)
            with self.lock:
                if not self.queue:
                    self._end_turn()
                    self.session.bus.emit("idle")
                    return
                if not self.limits.daily_runs.take():   # 排队的消息也要扣每日预算，否则能绕过去
                    dropped = len(self.queue)
                    self.queue.clear()
                    self._end_turn()
                    self.session.bus.emit("error", agent="main", retryable=False,
                                          message=f"今天的调用预算已用完，已丢弃排队中的 {dropped} 条消息")
                    self.session.bus.emit("idle")
                    return
                prompt = self.queue.pop(0)
                self.session.bus.emit("dequeue", remaining=len(self.queue))
            # _turn_active 保持 True，直接进入下一轮


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
            """
            host = host_from_header(self.headers.get("host") or "")
            if host not in app.allowed_hosts:
                self._reject(403, "Host 不被允许（防 DNS 重绑定）")
                return False
            origin = self.headers.get("origin")
            if origin and origin.rstrip("/") not in app.allowed_origins:
                self._reject(403, "Origin 不被允许（防跨站请求）")
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
            s = app.session
            path = self.path.split("?")[0]
            if not self._guard(path, "GET"):
                return
            if path in ("/", "/index.html"):
                return self._file("index.html")
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
                info["queue"] = len(app.queue)
                # Agent 跑完一轮到出队之间 agent.busy 已经是 False，直接用它会闪一下"空闲"。
                # 界面要的是"这一轮工作还没结束"，所以以本层标志为准。
                info["busy"] = app.busy
                return self._json(info)
            if path == "/api/messages":
                # 刷新页面后恢复聊天记录：直接返回完整消息列表，不依赖 SSE 事件重放（#86）
                return self._json({"ok": True, "messages": s.agent.messages})
            if path == "/api/context":
                return self._json({"system": s.agent.system_prompt(), "messages": s.agent.messages,
                                   "tools": s.registry.schemas(), "permissions": {
                                       "mode": s.permissions.mode, "allow": s.permissions.allow + s.permissions.session_allow,
                                       "deny": s.permissions.deny}})
            if path == "/api/extensions":
                return self._json(s.extensions())
            if path == "/api/events":
                return self._sse()
            if path == "/api/sessions":
                return self._json({"ok": True, "sessions": s.list_sessions(),
                                   "current": s.session_id})
            self.send_error(404)

        def do_POST(self):
            s = app.session
            path = self.path.split("?")[0]
            if not self._guard(path, "POST"):
                return
            data = self._body()
            if data is None:
                return
            if path == "/api/chat":
                try:
                    return self._json(app.chat(str(data.get("message", "")), self._client_key()))
                except RateLimited as e:
                    return self._limited(e)
            if path == "/api/permission":
                slot = app.pending.get(data.get("id", ""))
                if not slot:
                    return self._json({"ok": False, "error": "请求不存在或已过期"}, 404)
                slot["answer"] = data.get("decision") if data.get("decision") in ("yes", "always", "no") else "no"
                slot["event"].set()
                return self._json({"ok": True})
            if path == "/api/cancel":
                # "停止"连排队的一起停，否则队列里的会接着烧额度
                with app.lock:
                    dropped = len(app.queue)
                    app.queue.clear()
                if dropped:
                    s.bus.emit("dequeue", remaining=0)
                app.session.agent.cancel.set()
                return self._json({"ok": True})
            if path == "/api/ticket":
                ticket = app.issue_ticket()
                if ticket is None:
                    return self._reject(429, "未使用的票据太多，请稍后再试")
                return self._json({"ok": True, "ticket": ticket, "expires_in": TICKET_TTL})
            if path == "/api/session":
                # 忙时不许切会话，等当前任务做完（或先中断）
                if app.busy or app.queue:
                    return self._json({"ok": False, "error": "Agent 正在工作，等它做完（或先中断）再切换会话"})
                action = data.get("action", "")
                try:
                    if action == "new":
                        msg = s.new_session()
                    elif action == "load":
                        sid = str(data.get("id", "")).strip()
                        if not sid or "/" in sid or "\\" in sid or ".." in sid:
                            return self._json({"ok": False, "error": "会话 id 非法"})
                        msg = s.resume(sid)
                    else:
                        return self._json({"ok": False, "error": "未知操作"})
                except (OSError, ValueError) as e:
                    return self._json({"ok": False, "error": f"切换失败：{e}"})
                s.bus.emit("session_switched", message=msg)
                return self._json({"ok": True, "output": msg})
            self.send_error(404)

        def _sse(self):
            self.connection.settimeout(app.sse_write_timeout)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            after = resume_after(self.headers, urlparse(self.path).query)
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

            unsubscribe = app.session.bus.subscribe(enqueue_event)
            with app._sse_lock:
                app.sse_clients += 1
            try:
                sent = after
                # 补发历史，刷新页面不丢状态；重连时只补发浏览器还没收到的
                for e in [e for e in list(app.session.bus.history) if e["seq"] > after]:
                    self._send_event(e)
                    sent = max(sent, e["seq"])
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
                    if e["seq"] <= sent:      # 订阅之后、补发历史之前产生的事件，已经补发过了
                        continue
                    self._send_event(e)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                unsubscribe()
                with app._sse_lock:
                    app.sse_clients -= 1

        def _send_event(self, e):
            data = json.dumps(e, ensure_ascii=False, default=str)
            # id 行让浏览器（和控制面的转发）记住最后收到的序号，重连时作为 Last-Event-ID 带回来
            self.wfile.write(f"id: {e['seq']}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


def make_server(session: Session, host: str = "127.0.0.1", port: int = 8765,
                public_hosts=(), public_origins=()) -> tuple[ThreadingHTTPServer, WebApp]:
    app = WebApp(session)
    app.configure(host, port, public_hosts, public_origins)   # 先校验配置，失败就别绑端口了
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.configure(host, server.server_address[1], public_hosts, public_origins)   # 端口 0 时要用真实端口重算
    return server, app


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765,
          public_hosts=(), public_origins=()) -> None:
    server, app = make_server(session, host, port, public_hosts, public_origins)
    port = server.server_address[1]
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
        print("提醒：这只是校验请求是发给本服务的，不能代替认证。反向代理必须自己做 TLS 和登录（如 basic auth），"
              "否则任何能访问到它的人都能拿到令牌、驱动 Agent 执行命令。")
    print(f"模型 {session.provider_name}/{session.provider.model} · 工作目录 {session.cwd} · Ctrl-C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        session.close()

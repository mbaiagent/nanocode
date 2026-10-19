"""沙箱代理：跑在沙箱主机上，替远方的控制面起停沙箱容器（NCS_LAUNCHER=remote 的另一端）。

    python -m nanocode_server sandbox-agent

  管理接口（要带 Authorization: Bearer <NCS_REMOTE_AGENT_SECRET>，常量时间比较）：
    GET  /v1/info                       网关中继地址（控制面拿去写进沙箱策略）
    POST /v1/sandboxes/<id>/start       body: owner / web_token / api_key / policy / resume
    POST /v1/sandboxes/<id>/stop        停止，保留工作区
    POST /v1/sandboxes/<id>/delete      停止并删除工作区
    GET  /v1/sandboxes/<id>             {"alive": bool}
    POST /v1/reconcile                  body: {"keep": [...]}，停掉其它沙箱
    POST /v1/shutdown                   控制面退出：停掉全部沙箱
  转发（凭每个沙箱自己的 web_token，和本机 Docker 一样）：
    GET/POST /sbx/<id>/<路径>           转给沙箱容器，事件流逐行转发
  网关中继：
    在沙箱网桥地址的 NCS_GATEWAY_PORT 上开一个 TCP 中继，转到 NCS_AGENT_UPSTREAM_GATEWAY（控制面那边的模型网关）。
    沙箱只能连这一个端口（宿主机防火墙，deploy/firewall/），到不了数据库、Redis 和控制面。
  出站代理（NCS_EGRESS_PROXY=1，默认关）：
    在同一个网桥地址的 NCS_EGRESS_PORT 上开一个只放行 HTTP/HTTPS 的正向代理（egress.py），
    并把 HTTP(S)_PROXY / NO_PROXY 注入沙箱容器。防火墙再放行这个端口，沙箱就能经它上公网，
    内网、本机、元数据地址仍然到不了。

安全前提：代理只监听内网隧道地址（NCS_AGENT_BIND，例如 WireGuard 的 10.77.0.2:8790），
不能是 0.0.0.0；管理接口能起停容器，密钥泄漏等于能在这台机器上跑任意沙箱。
"""

from __future__ import annotations

import hmac
import json
import re
import socket
import sys
import threading
from types import SimpleNamespace

from .config import Config, split_hostport
from .egress import EgressProxy, proxy_env, pump
from .runtime import Launcher, LaunchError, SandboxSpec
from .web.framework import App, HttpError, Request, Response, make_server
from .web.proxy import SandboxGone, forward

SID = re.compile(r"[A-Za-z0-9]{16}")


class TcpRelay:
    """TCP 中继：沙箱 → 本机网桥地址:端口 → 上游模型网关。

    所有沙箱共用这个中继，所以每条连接都必须有时限，否则一个沙箱开一堆空闲连接就能占满名额、
    让别人的沙箱调不了模型（#126 审查）：
      - 空闲超时：两个方向都没有数据超过 idle_timeout 就断开（模型流式输出期间一直有数据）；
      - 半关闭时限：一头发完 EOF 后，另一头停顿超过 half_close_timeout 就断开；
      - 写超时：对方不读、发不出去，同样按 idle_timeout 断开；
      - 名额：全局 max_connections，每个沙箱（按来源 IP）per_source 条，超了直接断开。
    """

    def __init__(self, host: str, port: int, upstream: tuple[str, int], max_connections: int = 128,
                 connect_timeout: float = 10, idle_timeout: float = 300, half_close_timeout: float = 30,
                 per_source: int = 16):
        self.upstream = upstream
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.half_close_timeout = half_close_timeout
        self.max_connections = max_connections
        self.per_source = per_source
        self._count = 0
        self._by_source: dict[str, int] = {}
        self._guard = threading.Lock()
        self._sock = socket.create_server((host, port))
        self._stop = threading.Event()
        self.address = self._sock.getsockname()[:2]

    def start(self) -> "TcpRelay":
        threading.Thread(target=self._accept, daemon=True, name="网关中继").start()
        return self

    def active(self) -> int:
        with self._guard:
            return self._count

    def _admit(self, source: str) -> bool:
        with self._guard:
            if self._count >= self.max_connections or self._by_source.get(source, 0) >= self.per_source:
                return False
            self._count += 1
            self._by_source[source] = self._by_source.get(source, 0) + 1
            return True

    def _release(self, source: str) -> None:
        with self._guard:
            self._count -= 1
            n = self._by_source.get(source, 0) - 1
            if n > 0:
                self._by_source[source] = n
            else:
                self._by_source.pop(source, None)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except OSError:
                return
            source = addr[0]
            if not self._admit(source):
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client, source), daemon=True).start()

    def _serve(self, client: socket.socket, source: str) -> None:
        upstream = None
        try:
            try:
                upstream = socket.create_connection(self.upstream, timeout=self.connect_timeout)
            except OSError:
                return
            for sk in (client, upstream):
                sk.settimeout(self.idle_timeout)        # sendall 发不出去时也按空闲超时断开
            self._pump(client, upstream)
        finally:
            for sk in (client, upstream):
                if sk is not None:
                    try:
                        sk.close()
                    except OSError:
                        pass
            self._release(source)

    def _pump(self, client: socket.socket, upstream: socket.socket) -> None:
        pump(client, upstream, self.idle_timeout, self.half_close_timeout, self._stop)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class SandboxAgent(App):
    max_body = 2 * 1024 * 1024          # 转发的请求体（控制面那边上限 1 MB）加上启动参数

    def __init__(self, launcher: Launcher, secret: str, gateway_url: str, log=print):
        super().__init__()
        if len(secret) < 32:
            raise ValueError("沙箱代理的密钥至少 32 个字符")
        self.launcher = launcher
        self.secret = secret.encode()
        self.gateway_url = gateway_url
        self.log = log
        self._tokens: dict[str, str] = {}       # 会话 → 这一代沙箱的 web_token（转发时核对）
        self._endpoints: dict[str, str] = {}    # 会话 → 本机容器地址
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self.egress: EgressProxy | None = None  # NCS_EGRESS_PROXY=1 时由 build_agent 建好

        self.route("GET", "/healthz")(lambda req: Response.json({"ok": True}))
        self.route("GET", "/v1/info")(self.info)
        self.route("POST", "/v1/sandboxes/{sid}/start")(self.start)
        self.route("POST", "/v1/sandboxes/{sid}/stop")(self.stop)
        self.route("POST", "/v1/sandboxes/{sid}/delete")(self.delete)
        self.route("GET", "/v1/sandboxes/{sid}")(self.alive)
        self.route("POST", "/v1/reconcile")(self.reconcile)
        self.route("POST", "/v1/shutdown")(self.shutdown)
        for method in ("GET", "POST"):
            self.route(method, "/sbx/{sid}/{rest:path}")(self.proxy)

    def sandbox_of(self, ip: str) -> str:
        """出站代理的访问日志用：来源 IP 是哪个沙箱（会话编号）；不认识返回空串。"""
        with self._guard:
            for sid, endpoint in self._endpoints.items():
                if endpoint.split("//", 1)[-1].rsplit(":", 1)[0].strip("[]") == ip:
                    return sid
        return ""

    # ------------------------------------------------------------------ 认证

    def handle(self, req: Request) -> Response:
        if req.path.startswith("/v1/"):
            auth = req.headers.get("authorization") or ""
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token.encode(), self.secret):
                raise HttpError(401, "unauthorized")
        return self.dispatch(req)

    @staticmethod
    def _sid(req: Request) -> str:
        sid = req.params.get("sid", "")
        if not SID.fullmatch(sid):
            raise HttpError(404, "没有这个沙箱")
        return sid

    def _lock(self, sid: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(sid, threading.Lock())

    def _forget(self, sid: str) -> None:
        with self._guard:
            self._tokens.pop(sid, None)
            self._endpoints.pop(sid, None)

    # ------------------------------------------------------------------ 管理接口

    def info(self, req: Request) -> Response:
        return Response.json({"gateway_url": self.gateway_url, "launcher": self.launcher.name,
                              "isolated": self.launcher.isolated})

    def start(self, req: Request) -> Response:
        sid = self._sid(req)
        body = req.json()
        web_token = body.get("web_token")
        if not isinstance(web_token, str) or len(web_token) < 16 or not isinstance(body.get("policy"), dict):
            raise HttpError(400, "缺少 web_token 或 policy")
        spec = SandboxSpec(sid, str(body.get("owner") or ""), web_token, str(body.get("api_key") or ""),
                           body["policy"], bool(body.get("resume")))
        with self._lock(sid):
            self._forget(sid)
            try:
                endpoint = self.launcher.start(spec)
            except LaunchError as e:
                raise HttpError(502, str(e)) from None
            with self._guard:
                self._tokens[sid], self._endpoints[sid] = web_token, endpoint
        return Response.json({"ok": True})

    @staticmethod
    def _run(fn, *args):
        """启动器出错（Docker 不在、API 报错）回 502 带原因，而不是笼统的 500。"""
        try:
            return fn(*args)
        except (LaunchError, OSError) as e:
            raise HttpError(502, f"{type(e).__name__}: {e}") from None

    def stop(self, req: Request) -> Response:
        sid = self._sid(req)
        with self._lock(sid):
            self._forget(sid)
            self._run(self.launcher.stop, sid)
        return Response.json({"ok": True})

    def delete(self, req: Request) -> Response:
        sid = self._sid(req)
        with self._lock(sid):
            self._forget(sid)
            self._run(self.launcher.delete, sid)
        return Response.json({"ok": True})

    def alive(self, req: Request) -> Response:
        return Response.json({"alive": self._run(self.launcher.is_alive, self._sid(req))})

    def reconcile(self, req: Request) -> Response:
        keep = req.json().get("keep")
        if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
            raise HttpError(400, "keep 必须是会话编号列表")
        keep_set = set(keep)
        self._run(self.launcher.reconcile, keep_set)
        with self._guard:
            for sid in [s for s in self._endpoints if s not in keep_set]:
                self._tokens.pop(sid, None)
                self._endpoints.pop(sid, None)
        return Response.json({"ok": True})

    def shutdown(self, req: Request) -> Response:
        with self._guard:
            self._tokens.clear()
            self._endpoints.clear()
        self._run(self.launcher.shutdown)
        return Response.json({"ok": True})

    # ------------------------------------------------------------------ 转发

    def proxy(self, req: Request) -> Response:
        """不认识的、令牌对不上的、连不上的一律回 403：控制面把 403 当作"沙箱不在了"，会重新启动它。"""
        sid = self._sid(req)
        token = req.headers.get("x-nanocode-token") or ""
        with self._guard:
            known, endpoint = self._tokens.get(sid), self._endpoints.get(sid)
        if known is not None and not hmac.compare_digest(token.encode(), known.encode()):
            raise HttpError(403, "令牌不对")
        if endpoint is None:
            # 代理重启过：容器可能还在。找回地址，令牌交给沙箱自己核对（它只认启动时那一个）
            finder = getattr(self.launcher, "endpoint_of", None)
            try:
                endpoint = finder(sid) if finder else None
            except (LaunchError, OSError):
                endpoint = None
            if endpoint is None:
                raise HttpError(403, "沙箱不在了")
            with self._guard:
                self._endpoints.setdefault(sid, endpoint)   # 缓存地址，不用每个请求都查 Docker；令牌仍由沙箱核对
        try:
            return forward(SimpleNamespace(endpoint=endpoint, web_token=token), req, "/" + req.params["rest"])
        except SandboxGone:
            if known is None:
                with self._guard:                           # 找回来的地址失效了：下次重新找
                    if self._endpoints.get(sid) == endpoint:
                        self._endpoints.pop(sid, None)
            raise HttpError(403, "沙箱不在了") from None


def build_agent(cfg: Config, launcher: Launcher | None = None, log=print) -> tuple[SandboxAgent, TcpRelay]:
    """按配置建好代理和网关中继（还没开始监听 HTTP）。launcher 给测试注入用，默认是本机 Docker 启动器。"""
    if launcher is None:
        from .runtime import DockerLauncher
        launcher = DockerLauncher(cfg.data_dir, cfg.sandbox_image, cfg.docker_network, cfg.docker_runtime,
                                  cfg.sandbox_cpu, cfg.sandbox_memory, max_lifetime=cfg.token_ttl,
                                  bridge_name=cfg.docker_bridge, subnet=cfg.docker_subnet,
                                  tmpfs=cfg.sandbox_tmpfs, pids_limit=cfg.sandbox_pids)
    relay_host = cfg.agent_relay_bind or launcher.gateway_bind_host() or "127.0.0.1"
    relay = TcpRelay(relay_host, cfg.gateway_port, split_hostport(cfg.agent_upstream_gateway),
                     max_connections=cfg.agent_relay_max_connections, idle_timeout=cfg.agent_relay_idle_timeout,
                     per_source=cfg.agent_relay_per_sandbox).start()
    host, port = relay.address
    gateway_url = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    agent = SandboxAgent(launcher, cfg.remote_agent_secret, gateway_url, log)
    if cfg.egress_proxy:
        agent.egress = build_egress(cfg, cfg.egress_bind or host, agent.sandbox_of, log).start()
        # 沙箱里的工具经代理上网；模型调用（中继地址）不走代理
        launcher.extra_env = proxy_env(agent.egress.url, [host])
    return agent, relay


def build_egress(cfg: Config, host: str, who=None, log=print, **test_overrides) -> EgressProxy:
    """出站代理（还没开始 accept）。沙箱网段和 NCS_EGRESS_BLOCK_NETWORKS 一起拦住，宿主机自己的地址自动拦住。"""
    block = list(cfg.egress_block_networks)
    if cfg.docker_subnet:
        block.append(cfg.docker_subnet)
    return EgressProxy(host, cfg.egress_port, allowed_ports=cfg.egress_allowed_ports, block_networks=block,
                       allow_domains=cfg.egress_allow_domains, deny_domains=cfg.egress_deny_domains,
                       max_connections=cfg.egress_max_connections, per_source=cfg.egress_per_sandbox,
                       idle_timeout=cfg.egress_idle_timeout, who=who, log=log, **test_overrides)


def run_agent(argv: list[str] | None = None) -> int:
    import signal

    cfg = Config.from_env()
    try:
        cfg.validate_agent()
    except ValueError as e:
        print(f"沙箱代理配置错误：{e}", file=sys.stderr)
        return 2
    agent, relay = build_agent(cfg)
    host, port = split_hostport(cfg.agent_bind)
    server = make_server(agent, host, port, cfg.max_connections)
    threading.Thread(target=server.serve_forever, daemon=True, name="沙箱代理").start()
    print(f"沙箱代理：http://{host}:{port} · 沙箱 {agent.launcher.name} · 网关中继 {agent.gateway_url} → "
          f"{cfg.agent_upstream_gateway} · 出站代理 {agent.egress.url if agent.egress else '关闭'}", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        relay.close()
        if agent.egress:
            agent.egress.close()
        # 不停沙箱：代理重启（升级）时沙箱继续跑，控制面那边的会话不受影响；
        # 控制面退出时会调 /v1/shutdown，超寿容器由对账清理
    return 0


__all__ = ["SandboxAgent", "TcpRelay", "build_agent", "build_egress", "run_agent"]

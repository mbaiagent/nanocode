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
    GET  /v1/sandboxes/<id>/workspace   {"exists": bool}：本机有没有这个会话的工作区
    POST /v1/sandboxes/<id>/snapshot    body: url（预签名 PUT）/ max_bytes / ignore：打包工作区并上传
    POST /v1/sandboxes/<id>/restore     body: url（预签名 GET）/ max_bytes：下载、校验、恢复（本机没有工作区时）
      快照只拿预签名地址，对象存储的密钥留在控制面，沙箱主机上没有（services/snapshots.py）。
  转发（凭每个沙箱自己的 web_token，和本机 Docker 一样）：
    GET/POST /sbx/<id>/<路径>           转给沙箱容器，事件流逐行转发
  网关中继：
    在沙箱网桥地址的 NCS_GATEWAY_PORT 上开一个 TCP 中继，转到 NCS_AGENT_UPSTREAM_GATEWAY（控制面那边的模型网关）。
    沙箱只能连这一个端口（宿主机防火墙，deploy/firewall/），到不了数据库、Redis 和控制面。

安全前提：代理只监听内网隧道地址（NCS_AGENT_BIND，例如 WireGuard 的 10.77.0.2:8790），
不能是 0.0.0.0；管理接口能起停容器，密钥泄漏等于能在这台机器上跑任意沙箱。
"""

from __future__ import annotations

import hmac
import json
import re
import select
import socket
import sys
import threading
import time
from types import SimpleNamespace

from .config import Config, split_hostport
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
        peer = {client: upstream, upstream: client}
        reading = {client, upstream}
        last = time.monotonic()
        eof_at = None
        while reading and not self._stop.is_set():
            now = time.monotonic()
            deadline = last + self.idle_timeout
            if eof_at is not None:
                # 半关闭后剩下那个方向还在传数据就继续，停顿超过 half_close_timeout 就断开
                deadline = min(deadline, max(eof_at, last) + self.half_close_timeout)
            wait = deadline - now
            if wait <= 0:
                return
            try:
                ready, _, _ = select.select(list(reading), [], [], min(wait, 1.0))
            except (OSError, ValueError):
                return
            for sk in ready:
                try:
                    data = sk.recv(65536)
                    if data:
                        peer[sk].sendall(data)
                        last = time.monotonic()
                        continue
                except OSError:
                    return
                reading.discard(sk)                     # 这一头发完了：把 EOF 传给另一头（半关闭）
                if eof_at is None:
                    eof_at = time.monotonic()
                try:
                    peer[sk].shutdown(socket.SHUT_WR)
                except OSError:
                    pass

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

        self.route("GET", "/healthz")(lambda req: Response.json({"ok": True}))
        self.route("GET", "/v1/info")(self.info)
        self.route("POST", "/v1/sandboxes/{sid}/start")(self.start)
        self.route("POST", "/v1/sandboxes/{sid}/stop")(self.stop)
        self.route("POST", "/v1/sandboxes/{sid}/delete")(self.delete)
        self.route("GET", "/v1/sandboxes/{sid}")(self.alive)
        self.route("POST", "/v1/reconcile")(self.reconcile)
        self.route("POST", "/v1/shutdown")(self.shutdown)
        self.route("GET", "/v1/sandboxes/{sid}/workspace")(self.workspace)
        self.route("POST", "/v1/sandboxes/{sid}/snapshot")(self.snapshot)
        self.route("POST", "/v1/sandboxes/{sid}/restore")(self.restore)
        for method in ("GET", "POST"):
            self.route(method, "/sbx/{sid}/{rest:path}")(self.proxy)

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

    # ------------------------------------------------------------------ 快照

    def _snapshot_args(self, req: Request) -> tuple[str, str, int, dict]:
        if not getattr(self.launcher, "supports_snapshots", False):
            raise HttpError(501, f"{self.launcher.name} 启动器不支持工作区快照")
        sid = self._sid(req)
        body = req.json()
        url, max_bytes = body.get("url"), body.get("max_bytes")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise HttpError(400, "url 必须是 http(s) 预签名地址")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise HttpError(400, "max_bytes 必须是正整数")
        return sid, url, max_bytes, body

    def _snapshot_run(self, fn, *args) -> Response:
        from .runtime.snapshot import SnapshotError
        try:
            stats = fn(*args)
        except SnapshotError as e:
            # 422 = 重试也没用（太大、归档不合法）；502 = 暂时性错误（网络、对象存储）
            raise HttpError(422 if e.permanent else 502, str(e)) from None
        except (LaunchError, OSError) as e:
            raise HttpError(502, f"{type(e).__name__}: {e}") from None
        return Response.json({"ok": True, "stats": stats})

    def workspace(self, req: Request) -> Response:
        if not getattr(self.launcher, "supports_snapshots", False):
            raise HttpError(501, f"{self.launcher.name} 启动器不支持工作区快照")
        return Response.json({"exists": self._run(self.launcher.has_workspace, self._sid(req))})

    def snapshot(self, req: Request) -> Response:
        sid, url, max_bytes, body = self._snapshot_args(req)
        ignore = body.get("ignore")
        if not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore):
            raise HttpError(400, "ignore 必须是字符串列表")
        with self._lock(sid):                    # 和起停互斥：不会一边打包一边被重新拉起
            return self._snapshot_run(self.launcher.snapshot, sid, url, max_bytes, ignore)

    def restore(self, req: Request) -> Response:
        sid, url, max_bytes, _ = self._snapshot_args(req)
        with self._lock(sid):
            if self._run(self.launcher.is_alive, sid):
                raise HttpError(409, "沙箱在运行，不能恢复工作区")
            return self._snapshot_run(self.launcher.restore, sid, url, max_bytes)

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
    return SandboxAgent(launcher, cfg.remote_agent_secret, gateway_url, log), relay


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
          f"{cfg.agent_upstream_gateway}")
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
        # 不停沙箱：代理重启（升级）时沙箱继续跑，控制面那边的会话不受影响；
        # 控制面退出时会调 /v1/shutdown，超寿容器由对账清理
    return 0


__all__ = ["SandboxAgent", "TcpRelay", "build_agent", "run_agent"]

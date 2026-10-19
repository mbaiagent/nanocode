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
from .runtime import Launcher, LaunchError, SandboxSpec
from .web.framework import App, HttpError, Request, Response, make_server
from .web.proxy import SandboxGone, forward

SID = re.compile(r"[A-Za-z0-9]{16}")


class TcpRelay:
    """最简单的 TCP 中继：沙箱 → 本机网桥地址:端口 → 上游模型网关。连接数有上限，超过直接断开。"""

    def __init__(self, host: str, port: int, upstream: tuple[str, int], max_connections: int = 128,
                 connect_timeout: float = 10):
        self.upstream = upstream
        self.connect_timeout = connect_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._sock = socket.create_server((host, port))
        self._stop = threading.Event()
        self.address = self._sock.getsockname()[:2]

    def start(self) -> "TcpRelay":
        threading.Thread(target=self._accept, daemon=True, name="网关中继").start()
        return self

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            try:
                upstream = socket.create_connection(self.upstream, timeout=self.connect_timeout)
            except OSError:
                return
            upstream.settimeout(None)

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)       # 半关闭：把 EOF 传给另一头
                    except OSError:
                        pass

            back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            back.start()
            pump(client, upstream)
            back.join()
            upstream.close()
        finally:
            client.close()
            self._slots.release()

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

    def stop(self, req: Request) -> Response:
        sid = self._sid(req)
        with self._lock(sid):
            self._forget(sid)
            self.launcher.stop(sid)
        return Response.json({"ok": True})

    def delete(self, req: Request) -> Response:
        sid = self._sid(req)
        with self._lock(sid):
            self._forget(sid)
            self.launcher.delete(sid)
        return Response.json({"ok": True})

    def alive(self, req: Request) -> Response:
        return Response.json({"alive": self.launcher.is_alive(self._sid(req))})

    def reconcile(self, req: Request) -> Response:
        keep = req.json().get("keep")
        if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
            raise HttpError(400, "keep 必须是会话编号列表")
        keep_set = set(keep)
        self.launcher.reconcile(keep_set)
        with self._guard:
            for sid in [s for s in self._endpoints if s not in keep_set]:
                self._tokens.pop(sid, None)
                self._endpoints.pop(sid, None)
        return Response.json({"ok": True})

    def shutdown(self, req: Request) -> Response:
        with self._guard:
            self._tokens.clear()
            self._endpoints.clear()
        self.launcher.shutdown()
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
            endpoint = finder(sid) if finder else None
            if endpoint is None:
                raise HttpError(403, "沙箱不在了")
        try:
            return forward(SimpleNamespace(endpoint=endpoint, web_token=token), req, "/" + req.params["rest"])
        except SandboxGone:
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
    relay = TcpRelay(relay_host, cfg.gateway_port, split_hostport(cfg.agent_upstream_gateway)).start()
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

"""组装：把配置变成一套运行中的服务。所有组件在这里创建、在这里连接，别处不自己 new 依赖。

    Platform(cfg, role)      按角色创建组件：
                               all      控制面 + 模型网关（单机）
                               control  只有控制面（分布式部署）
                               gateway  只有模型网关（分布式部署；不需要沙箱启动器）
    platform.start()         在后台线程里启动 HTTP 服务和回收空闲沙箱的线程
    platform.close()         停止一切（包括本机启动器的子进程）
"""

from __future__ import annotations

import threading

from .config import Config
from .runtime import Launcher, make_launcher
from .security.auth import Authenticator
from .security.secretbox import SecretBox
from .services.catalog import ModelCatalog
from .services.sessions import SessionManager
from .storage import make_rate_limiter, open_store
from .web.control import ControlApp
from .web.framework import make_server
from .web.gateway import GatewayApp

ROLES = ("all", "control", "gateway")


class Platform:
    def __init__(self, cfg: Config, launcher: Launcher | None = None, log=print, role: str = "all"):
        if role not in ROLES:
            raise ValueError(f"未知的角色：{role}")
        cfg.ensure_secret()
        cfg.validate()
        self.cfg, self.log, self.role = cfg, log, role
        self.store = open_store(cfg)
        self.limiter = make_rate_limiter(cfg)
        self.catalog = ModelCatalog(cfg, self.store, SecretBox(cfg.secret_key))
        self.launcher = self.sessions = self.control = self.gateway = None
        self.control_server = self.gateway_server = None
        self._stop = threading.Event()
        if role in ("all", "control"):
            self.launcher = launcher or make_launcher(cfg, log)
            bind = self.launcher.gateway_bind_host()     # Docker：网关要监听在沙箱网络的网桥地址上
            if role == "all" and bind and cfg.gateway_host in ("127.0.0.1", "localhost"):
                cfg.gateway_host = bind
            self.sessions = SessionManager(cfg, self.store, self.launcher, self.catalog, log)
            self.control = ControlApp(cfg, self.store, Authenticator(cfg), self.catalog, self.sessions,
                                      self.launcher, self.limiter.backend)
        if role in ("all", "gateway"):
            self.gateway = GatewayApp(cfg, self.store, self.catalog, self.limiter)

    def describe(self) -> str:
        parts = [f"数据库 {self.store.backend}", f"限流 {self.limiter.backend}"]
        if self.launcher:
            parts.append(f"沙箱 {self.launcher.name}{'' if self.launcher.isolated else '（没有隔离，只适合开发）'}")
            parts.append(f"认证 {self.cfg.auth}")
        return " · ".join(parts)

    def start(self) -> None:
        """在后台线程里启动服务，立即返回。"""
        if self.gateway:
            self.gateway_server = self._serve(make_server(self.gateway, self.cfg.gateway_host, self.cfg.gateway_port),
                                              "模型网关")
            if self.sessions and self.cfg.gateway_port == 0 and not self.cfg.gateway_url:   # 测试用随机端口：沙箱要知道真实端口
                port = self.gateway_server.server_address[1]
                self.cfg.gateway_port = port
                self.sessions.gateway_url = (self.launcher.gateway_url_for_sandbox(port)
                                             or f"http://{self.cfg.gateway_host}:{port}")
        if self.control:
            self.sessions.reconcile()
            self.control_server = self._serve(make_server(self.control, self.cfg.host, self.cfg.port), "控制面")
            threading.Thread(target=self.sessions.reap_forever, args=(self._stop,), daemon=True).start()

    def _serve(self, server, name: str):
        threading.Thread(target=server.serve_forever, daemon=True, name=name).start()
        host, port = server.server_address[:2]
        self.log(f"{name}：http://{host}:{port}")
        return server

    @property
    def control_port(self) -> int:
        return self.control_server.server_address[1]

    def close(self) -> None:
        self._stop.set()
        for s in (self.control_server, self.gateway_server):
            if s:
                s.shutdown()
                s.server_close()
        if self.launcher:
            self.launcher.shutdown()
        self.store.close()


__all__ = ["Platform", "ROLES"]

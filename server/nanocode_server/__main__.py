"""命令行入口。

    python -m nanocode_server control   启动控制面（配置全部来自 NCS_* 环境变量，见 config.py）
    python -m nanocode_server gateway   启动模型网关
    python -m nanocode_server dev       本机开发：控制面 + 网关在一个进程里，本机子进程当沙箱，
                                        当前用户固定为 NCS_DEV_USER（默认 dev@localhost）。
                                        没配上游模型时沙箱用 mock 模型，不需要任何 Key 就能跑起来
    python -m nanocode_server policy    打印沙箱的平台策略（部署时放进 ConfigMap，见 deploy/k8s/）
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading

from .app import make_control_server
from .config import Config
from .gateway import make_gateway_server


def _serve(server, name: str) -> None:
    host, port = server.server_address[:2]
    print(f"{name} 已启动：http://{host}:{port}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    cfg = Config.from_env()
    if cmd == "policy":
        print(json.dumps(cfg.sandbox_policy("/data/home/audit.jsonl"), ensure_ascii=False, indent=2))
        return 0
    if cmd == "gateway":
        cfg.check_gateway()
        server, _ = make_gateway_server(cfg)
        _serve(server, "模型网关")
        return 0
    if cmd == "control":
        server, cp = make_control_server(cfg)
        threading.Thread(target=cp.reap_forever, daemon=True).start()
        try:
            _serve(server, "控制面")
        finally:
            cp.shutdown()
        return 0
    if cmd == "dev":
        cfg.launcher = "local"
        cfg.host = "127.0.0.1"
        cfg.dev_user = cfg.dev_user or "dev@localhost"
        cfg.token_secret = cfg.token_secret or secrets.token_urlsafe(48)
        if not (cfg.upstream_base_url and cfg.upstream_api_key):
            cfg.sandbox_provider = "mock"
            print("没有配置 NCS_UPSTREAM_BASE_URL / NCS_UPSTREAM_API_KEY：沙箱使用 mock 模型")
        else:
            gw_server, _ = make_gateway_server(cfg)
            cfg.gateway_url = f"http://127.0.0.1:{gw_server.server_address[1]}/v1"
            threading.Thread(target=_serve, args=(gw_server, "模型网关"), daemon=True).start()
        print("警告：dev 模式用本机子进程当沙箱，没有任何隔离，只能在自己的电脑上开发用")
        server, cp = make_control_server(cfg)
        threading.Thread(target=cp.reap_forever, daemon=True).start()
        try:
            _serve(server, f"控制面（当前用户 {cfg.dev_user}）")
        except KeyboardInterrupt:
            pass
        finally:
            cp.shutdown()
            if hasattr(cp.launcher, "stop_all"):
                cp.launcher.stop_all()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

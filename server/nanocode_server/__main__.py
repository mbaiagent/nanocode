"""命令行入口。

    python -m nanocode_server              单机：控制面 + 模型网关一起启动（默认，开发模式登录）
    python -m nanocode_server control      只启动控制面（分布式部署）
    python -m nanocode_server gateway      只启动模型网关（分布式部署）
    python -m nanocode_server demo         模拟多个用户同时使用，检查互相隔离，打印报告
    python -m nanocode_server loadtest     压测：多个用户同时使用，量每一步的耗时、失败率和内存

配置全部来自 NCS_* 环境变量，见 config.py 和 server/README.md。
"""

from __future__ import annotations

import sys
import threading

from .config import Config


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "standalone"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "demo":
        from .demo import run_demo
        return run_demo(argv[1:])
    if cmd == "loadtest":
        from .loadtest import run_loadtest
        return run_loadtest(argv[1:])
    if cmd not in ("standalone", "control", "gateway"):
        print(__doc__)
        return 2
    from .platform import Platform
    cfg = Config.from_env()
    platform = Platform(cfg, role="all" if cmd == "standalone" else cmd)
    print(platform.describe())
    platform.start()
    if platform.control:
        print(f"打开 http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host}:{platform.control_port}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        platform.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

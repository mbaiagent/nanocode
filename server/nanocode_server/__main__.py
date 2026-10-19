"""平台部署（多用户）的命令行入口。只想在自己电脑上单人使用？不需要这个，直接 `nanocode --web`。

    python -m nanocode_server              平台一体启动：控制面 + 模型网关在一个进程里（默认；all 是别名）
    python -m nanocode_server control      只启动控制面（分布式部署）
    python -m nanocode_server gateway      只启动模型网关（分布式部署）
    python -m nanocode_server demo         模拟多个用户同时使用，检查互相隔离，打印报告
    python -m nanocode_server loadtest     压测：多个用户同时使用，量每一步的耗时、失败率和内存

配置全部来自 NCS_* 环境变量，见 config.py 和 server/README.md。
"""

from __future__ import annotations

import signal
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
    if cmd == "all":
        cmd = "standalone"
    if cmd not in ("standalone", "control", "gateway"):
        print(__doc__)
        return 2
    from .platform import Platform
    try:
        cfg = Config.from_env()
        platform = Platform(cfg, role="all" if cmd == "standalone" else cmd)
    except ValueError as e:
        # 配置错误给一句人话，而不是一屏 traceback；顺便指一下不需要平台的本地单机模式
        print(f"配置错误：{e}", file=sys.stderr)
        print("提示：nanocode-server 是多用户平台（要配 NCS_AUTH 等）。只在自己电脑上用的话，"
              "直接运行 `nanocode --web`，不需要数据库、Redis、Docker 或登录。", file=sys.stderr)
        return 2
    print(platform.describe())
    platform.start()
    if platform.control:
        print(f"打开 http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host}:{platform.control_port}")
    stop = threading.Event()
    # 容器和 systemd 停服务发的是 SIGTERM：不接的话进程不会退出，finally 里的
    # platform.close()（停沙箱、关数据库）永远不执行，沙箱会带着有效令牌继续跑（#60）。
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        platform.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

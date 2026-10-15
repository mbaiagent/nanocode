"""压测：N 个用户同时各开一个会话，量每一步花多久、有没有失败、沙箱占多少内存。

    python -m nanocode_server loadtest --users 10 25 50 100

和 demo 的区别：demo 只检查隔离是否正确，这里只关心时间和资源。每个用户的流程和 demo 一样
（登录 → 添加 Key → 新建会话（启动沙箱）→ 发消息 → 等这一轮结束），每一步分别计时。
模型是本机的假模型，所以测到的是平台本身的开销（控制面、网关、沙箱启动、事件流），不是真实模型的速度。

默认用本机进程启动器（每个沙箱一个 Python 进程），结果代表"没有容器隔离时"的上限；
Docker / K8s 的启动更慢，要单独测。
"""

from __future__ import annotations

import argparse
import re
import secrets
import statistics
import tempfile
import threading
import time

from .demo import Browser, FakeModel


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def sandbox_memory_mb(sids: list[str]) -> tuple[float, int]:
    """这一档的沙箱同时在跑时占了多少内存（docker stats 的快照，只算本次的沙箱）。

    Docker 沙箱是容器，宿主机的 /proc 里看不到它们的 Python 进程，所以用 docker stats。
    """
    import subprocess
    if not sids:
        return 0.0, 0
    names = {f"ncs-sbx-{sid}" for sid in sids}
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return float("nan"), 0
    total, n = 0.0, 0
    for line in out.splitlines():
        name, _, usage = line.partition("\t")
        if name not in names:
            continue
        used = usage.split("/")[0].strip()
        m = re.match(r"([\d.]+)\s*([KMG]i?B)", used)
        if m:
            total += float(m.group(1)) * {"KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}[m.group(2)]
            n += 1
    return total, n


def one_user(port: int, model_url: str, user: str, out: dict) -> None:
    b = Browser(port, user)
    t = {}
    try:
        s = time.time()
        b.request("POST", "/api/login", {"user": user})
        t["login"] = time.time() - s

        s = time.time()
        status, data = b.request("POST", "/api/credentials", {
            "provider": "custom", "label": f"{user} 的 Key", "base_url": model_url, "protocol": "openai",
            "api_key": f"sk-{user}-{secrets.token_hex(6)}", "models": "fake-model"})
        if status != 201:
            raise RuntimeError(f"添加 Key 失败 {status}: {data}")
        t["key"] = time.time() - s
        ref = data["credential"]["id"]

        s = time.time()
        status, data = b.request("POST", "/api/sessions", {"title": f"{user} 的任务", "model": f"c:{ref}:fake-model"})
        if status != 201:
            raise RuntimeError(f"新建会话失败 {status}: {data}")
        t["session"] = time.time() - s
        sid = data["session"]["id"]
        t["sid"] = sid                                         # 沙箱留到这一档结束再统一关，好量同时在跑时的内存

        s = time.time()
        status, data = b.request("POST", f"/s/{sid}/api/chat", {"message": f"把暗号写进文件。暗号：{secrets.token_hex(4)}"})
        if status != 200 or not data.get("ok"):
            raise RuntimeError(f"发消息失败 {status}: {data}")
        t["chat"] = time.time() - s

        s = time.time()
        b.wait_turn(sid, timeout=120)
        t["turn"] = time.time() - s
        t["total"] = t["login"] + t["key"] + t["session"] + t["chat"] + t["turn"]
        out[user] = {"ok": True, **t}
    except Exception as e:  # noqa: BLE001
        out[user] = {"ok": False, "error": f"{type(e).__name__}: {e}", **t}


def run_level(platform, model: FakeModel, n: int) -> dict:
    run = secrets.token_hex(3)
    users = [f"lt{i + 1}-{run}" for i in range(n)]
    out: dict = {}
    started = time.time()
    threads = [threading.Thread(target=one_user, args=(platform.control_port, model.url, u, out)) for u in users]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=300)
    wall = time.time() - started
    sids = [r["sid"] for r in out.values() if r.get("sid")]
    mem_mb, procs = sandbox_memory_mb(sids)       # 所有沙箱还在跑的时候量，之后才关
    for sid in sids:
        platform.sessions.stop(sid)                # 关掉这一档的沙箱，下一档不被干扰
    ok = [r for r in out.values() if r.get("ok")]
    failed = [r for r in out.values() if not r.get("ok")]
    stats = {"users": n, "ok": len(ok), "failed": len(failed), "wall": wall,
             "mem_mb": mem_mb, "sandboxes": procs, "launcher": type(platform.sessions.launcher).__name__,
             "errors": sorted({r["error"][:160] for r in failed})[:5]}
    for step in ("login", "key", "session", "chat", "turn", "total"):
        xs = [r[step] for r in ok if step in r]
        stats[step] = {"p50": _pct(xs, 50), "p95": _pct(xs, 95), "max": max(xs) if xs else float("nan"),
                       "mean": statistics.fmean(xs) if xs else float("nan")}
    return stats


def fmt(stats: dict) -> str:
    lines = [f"── {stats['users']} 个用户同时 ──",
             f"成功 {stats['ok']} / 失败 {stats['failed']}　总耗时 {stats['wall']:.1f}s　"
             f"沙箱（{stats['launcher']}）{stats['sandboxes']} 个同时运行，合计内存 {stats['mem_mb']:.0f} MB"]
    lines.append(f"  {'步骤':<10}{'p50':>9}{'p95':>9}{'max':>9}   (秒)")
    names = {"login": "登录", "key": "添加Key", "session": "建会话", "chat": "发消息", "turn": "等结束", "total": "合计"}
    for step, label in names.items():
        s = stats[step]
        lines.append(f"  {label:<10}{s['p50']:>9.2f}{s['p95']:>9.2f}{s['max']:>9.2f}")
    for e in stats["errors"]:
        lines.append(f"  错误：{e}")
    return "\n".join(lines)


def run_loadtest(argv: list[str]) -> int:
    from .config import Config
    from .platform import Platform

    ap = argparse.ArgumentParser(prog="python -m nanocode_server loadtest", description=__doc__.splitlines()[0])
    ap.add_argument("--users", type=int, nargs="+", default=[10, 25, 50], help="每一档同时几个用户")
    args = ap.parse_args(argv)

    model = FakeModel()
    cfg = Config.from_env()
    cfg.data_dir = tempfile.mkdtemp(prefix="ncs-load-")
    cfg.secret_key, cfg.auth, cfg.host, cfg.port, cfg.gateway_port = "", "dev", "127.0.0.1", 0, 0
    cfg.allow_private_upstreams = True
    cfg.sandbox_mode = "accept_edits"
    cfg.max_sessions_per_user = 2
    cfg.max_sandboxes = max(cfg.max_sandboxes, max(args.users) * 2)
    cfg.requests_per_minute = max(cfg.requests_per_minute, 10_000)   # 压测要测系统本身，不是限流
    platform = Platform(cfg, log=lambda m: None)
    platform.start()
    results = []
    try:
        for n in args.users:
            st = run_level(platform, model, n)
            results.append(st)
            print(fmt(st), flush=True)
    finally:
        platform.close()
        model.close()
    return 0

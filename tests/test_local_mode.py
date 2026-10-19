"""本地单机模式（`nanocode` / `nanocode --web`）不依赖任何平台组件。

平台部署（server/ 的 nanocode-server）用 PostgreSQL、Redis、Docker/远程沙箱、GitHub 登录；
本地单机模式一个都不要：只用标准库、本机进程、~/.nanocode 下的文件。这里把它钉死——
即使环境里残留了平台的配置（NCS_DATABASE_URL、NCS_LAUNCHER=remote、DOCKER_HOST……），
本地模式也照常启动，不 import 平台代码，不连外网，不调 docker。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import queue
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 本地模式绝不能碰的模块：平台本身和它的可选驱动
FORBIDDEN_MODULES = ("nanocode_server", "psycopg", "psycopg2", "redis", "docker", "kubernetes")
PLATFORM_EXECUTABLES = ("docker", "kubectl", "podman", "wg", "nerdctl")

# 子进程里运行：审计钩子记录所有对外连接和子进程，然后在后台线程里启动 `nanocode --web`；
# 父进程发一行 check，子进程回一行 JSON（加载了哪些禁用模块、连过哪些地址、起过哪些进程）。
CHILD = r'''
import json, os, sys, threading
FORBIDDEN_EXE = %r
LOOPBACK = ("127.0.0.1", "::1", "localhost")
seen = {"connect": [], "exec": []}
def hook(event, args):
    # 违规的动作记下来并直接拒绝：既能断言，又不会真的去连外网 / 等 docker 超时把测试卡住
    if event == "socket.connect":
        addr = args[1]
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in LOOPBACK:
            seen["connect"].append(host)
            raise PermissionError("local mode must not connect to " + host)
    elif event == "subprocess.Popen":
        exe, argv = args[0], args[1]
        name = str(argv[0] if isinstance(argv, (list, tuple)) and argv else exe or argv)
        base = os.path.basename(name).lower()
        base = base[:-4] if base.endswith(".exe") else base
        if base in FORBIDDEN_EXE:
            seen["exec"].append(base)
            raise PermissionError("local mode must not run " + base)
sys.addaudithook(hook)
from nanocode.cli import main
threading.Thread(target=main, args=(["--web", "--port", "0", "--cwd", sys.argv[1]],), daemon=True).start()
for line in sys.stdin:
    if line.strip() == "check":
        mods = sorted(m for m in sys.modules if m.split(".")[0] in %r)
        print("CHECK " + json.dumps({"modules": mods, **seen}), flush=True)
        break
''' % (PLATFORM_EXECUTABLES, FORBIDDEN_MODULES)


def _clean_env(home: str) -> dict:
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("NCS_", "NANOCODE_")) and not k.endswith("_API_KEY")
            and k.upper() not in ("DATABASE_URL", "REDIS_URL", "DOCKER_HOST", "KUBECONFIG",
                                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    keep.update({
        "HOME": home, "USERPROFILE": home, "NANOCODE_HOME": os.path.join(home, ".nanocode"),
        "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        # 残留的平台配置：本地模式必须完全无视它们
        "NCS_AUTH": "github", "NCS_DATABASE_URL": "postgresql://user:pw@db.invalid/nanocode",
        "NCS_REDIS_URL": "redis://cache.invalid:6379/0", "NCS_LAUNCHER": "remote",
        "NCS_REMOTE_AGENT_URL": "http://agent.invalid:8790", "DOCKER_HOST": "tcp://docker.invalid:2375",
        "HTTPS_PROXY": "http://proxy.invalid:1", "HTTP_PROXY": "http://proxy.invalid:1",
    })
    return keep


class LocalModeHasNoPlatformDeps(unittest.TestCase):
    def test_core_package_never_imports_platform_code(self):
        """静态检查：nanocode/ 下任何文件都不 import 平台代码或它的驱动（懒加载也不行）。"""
        bad = []
        for path in (ROOT / "nanocode").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                bad += [f"{path.relative_to(ROOT)}: {n}" for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
        self.assertEqual(bad, [])

    def test_core_package_declares_no_dependencies(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^dependencies = \[\]$")

    def test_web_starts_without_db_redis_docker_or_network(self):
        with tempfile.TemporaryDirectory() as home:
            work = os.path.join(home, "work")
            os.makedirs(work)
            proc = subprocess.Popen([sys.executable, "-c", CHILD, work], cwd=home, env=_clean_env(home),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")
            lines: queue.Queue = queue.Queue()
            threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] + [lines.put(None)],
                             daemon=True).start()

            def next_line(deadline: float):
                try:
                    return lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    return None

            out, url, report = [], "", None
            try:
                deadline = time.time() + 30
                while not url and (line := next_line(deadline)) is not None:
                    out.append(line)
                    m = re.search(r"Web 界面：(http://127\.0\.0\.1:\d+)", line)
                    url = m.group(1) if m else ""
                self.assertTrue(url, "本地 Web 没有启动：\n" + "".join(out))
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url + "/", timeout=10) as r:
                    self.assertEqual(r.status, 200)
                proc.stdin.write("check\n")
                proc.stdin.flush()
                deadline = time.time() + 15
                while report is None and (line := next_line(deadline)) is not None:
                    out.append(line)
                    if line.startswith("CHECK "):
                        report = json.loads(line[6:])
                self.assertIsNotNone(report, "".join(out))
            finally:
                proc.kill()
                proc.wait(timeout=10)
                proc.stdout.close()
                proc.stdin.close()
        self.assertEqual(report["modules"], [], "本地模式加载了平台模块")
        self.assertEqual(report["connect"], [], "本地模式启动时连了外部地址")
        self.assertEqual(report["exec"], [], "本地模式调用了容器/隧道工具")

if __name__ == "__main__":
    unittest.main()

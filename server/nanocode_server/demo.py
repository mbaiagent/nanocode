"""模拟多个用户同时使用，检查他们互相不影响。不需要任何真实的模型 Key。

    python -m nanocode_server demo [--users 4]

做法：在进程里启动一个完整的平台（控制面 + 模型网关 + 沙箱），再启动一个假的 OpenAI 兼容模型服务
（FakeModel）。每个用户在各自的线程里同时：
  1. 登录；
  2. 添加自己的 API Key（指向假模型，Key 各不相同）；
  3. 新建会话，选自己 Key 下的模型；
  4. 让 Agent 在工作区里写一个文件，内容是只有这个用户知道的暗号。
然后检查：
  - 每个人只看得到自己的会话和 Key；
  - 访问别人的会话一律 404；
  - 每个沙箱的工作区里只有自己的文件；
  - 假模型收到的请求，带的都是发起人自己的 Key（网关按会话选 Key，没有串）；
  - 用量分别记在各自名下。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModel:
    """一个假的 OpenAI 兼容模型：第一轮让 Agent 调用 write_file 把用户消息里的暗号写进 whoami.txt，
    拿到工具结果后回一句"完成"。记录每个请求带的 Key，用来检查网关有没有用错 Key。"""

    def __init__(self):
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                key = (self.headers.get("authorization") or "")[7:]
                with outer._lock:
                    outer.calls.append({"key": key, "model": body.get("model")})
                messages = body.get("messages") or []
                last = messages[-1] if messages else {}
                if last.get("role") == "tool":
                    events = [{"choices": [{"delta": {"content": "完成。"}, "finish_reason": "stop"}]}]
                else:
                    secret = str(last.get("content", "")).rsplit("暗号：", 1)[-1].strip()
                    args = json.dumps({"path": "whoami.txt", "content": secret}, ensure_ascii=False)
                    events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                       "function": {"name": "write_file", "arguments": args}}]},
                                            "finish_reason": "tool_calls"}]}]
                events.append({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for e in events:
                    self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Browser:
    """一个用户的"浏览器"：保存登录 Cookie，发 JSON 请求。"""

    def __init__(self, port: int, user: str):
        self.port = port
        self.user = user
        self.cookie = ""

    def request(self, method: str, path: str, body=None) -> tuple[int, dict | str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["content-type"] = "application/json"
        if self.cookie:
            headers["cookie"] = self.cookie
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        raw = r.read()
        cookie = r.getheader("set-cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def wait_turn(self, sid: str, timeout: float = 60) -> None:
        """读事件流直到这一轮结束。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/s/{sid}/api/events", headers={"Host": f"127.0.0.1:{self.port}", "cookie": self.cookie})
        resp = conn.getresponse()
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                line = resp.readline()
                if not line:
                    break
                if line.startswith(b"data: ") and json.loads(line[6:]).get("type") == "turn_end":
                    return
        finally:
            conn.close()
        raise TimeoutError(f"{self.user} 的任务没有在 {timeout} 秒内结束")


def simulate(platform, model: FakeModel, users: list[str]) -> dict:
    """每个用户一个线程，同时走完整个流程。返回每个用户的结果。"""
    port = platform.control_port
    results: dict[str, dict] = {}

    def run(user: str) -> None:
        b = Browser(port, user)
        r: dict = {"user": user, "secret": secrets.token_hex(4), "key": f"sk-{user}-{secrets.token_hex(6)}"}
        b.request("POST", "/api/login", {"user": user})
        status, data = b.request("POST", "/api/credentials", {"provider": "custom", "label": f"{user} 的 Key",
                                                              "base_url": model.url, "protocol": "openai",
                                                              "api_key": r["key"], "models": "fake-model"})
        assert status == 201, data
        ref = data["credential"]["id"]
        status, data = b.request("POST", "/api/sessions", {"title": f"{user} 的任务", "model": f"c:{ref}:fake-model"})
        assert status == 201, data
        r["session"] = data["session"]["id"]
        status, data = b.request("POST", f"/s/{r['session']}/api/chat", {"message": f"把暗号写进文件。暗号：{r['secret']}"})
        assert status == 200 and data.get("ok"), data
        b.wait_turn(r["session"])
        r["browser"] = b
        results[user] = r

    threads = [threading.Thread(target=run, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


def check_isolation(platform, model: FakeModel, results: dict, since: int = 0) -> list[tuple[str, bool, str]]:
    """逐项检查，返回 (检查项, 是否通过, 说明)。since：只看模型从第几次调用开始的记录（之前的不是这次模拟发的）。"""
    checks = []
    users = sorted(results)
    for u in users:
        r = results[u]
        b: Browser = r["browser"]
        _, data = b.request("GET", "/api/sessions")
        seen = {s["id"] for s in data["sessions"]}
        checks.append((f"{u} 只看得到自己的会话", seen == {r["session"]}, f"看到 {len(seen)} 个"))
        _, data = b.request("GET", "/api/credentials")
        keys = [c["label"] for c in data["credentials"]]
        checks.append((f"{u} 只看得到自己的 Key", keys == [f"{u} 的 Key"], "、".join(keys)))
        others = [results[o]["session"] for o in users if o != u]
        denied = all(b.request("GET", f"/s/{sid}/api/state")[0] == 404 for sid in others)
        checks.append((f"{u} 访问别人的会话被拒绝（404）", denied, f"试了 {len(others)} 个"))
        content = _read_workspace_file(platform, r["session"], "whoami.txt")
        checks.append((f"{u} 的工作区里是自己的暗号", content == r["secret"], f"读到 {content!r}"))
        usage = platform.store.get_usage(u)
        checks.append((f"{u} 的用量记在自己名下", usage["requests"] == 2, f"{usage['requests']} 次调用，{usage['total_tokens']} tokens"))
    expected = {results[u]["key"] for u in users}
    calls = model.calls[since:]
    used = {c["key"] for c in calls}
    checks.append(("模型收到的 Key 都是发起人自己的（网关没有串 Key）", used == expected,
                   f"{len(calls)} 次调用，用到 {len(used)} 把不同的 Key"))
    return checks


def _read_workspace_file(platform, sid: str, name: str) -> str | None:
    root = os.path.join(platform.cfg.data_dir, "sandboxes", sid)
    for sub in ("workspace", os.path.join("data", "workspace")):      # 本机启动器 / Docker 启动器的目录结构
        path = os.path.join(root, sub, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return None


def run_demo(argv: list[str]) -> int:
    from .config import Config
    from .platform import Platform

    ap = argparse.ArgumentParser(prog="python -m nanocode_server demo", description=__doc__.splitlines()[0])
    ap.add_argument("--users", type=int, default=4, help="同时模拟几个用户")
    args = ap.parse_args(argv)

    model = FakeModel()
    cfg = Config.from_env()
    cfg.data_dir = tempfile.mkdtemp(prefix="ncs-demo-")
    cfg.secret_key, cfg.auth, cfg.host, cfg.port, cfg.gateway_port = "", "dev", "127.0.0.1", 0, 0
    cfg.allow_private_upstreams = True          # 假模型在本机
    cfg.sandbox_mode = "accept_edits"           # 让 Agent 直接写文件，不用人点确认
    cfg.max_sessions_per_user = 2
    platform = Platform(cfg, log=lambda m: None)
    platform.start()
    run = secrets.token_hex(2)               # 用户名带上本轮的随机后缀：共享数据库里可能有上一轮的数据
    users = [f"user{i + 1}-{run}" for i in range(args.users)]
    print(f"平台：{platform.describe()}")
    print(f"同时模拟 {len(users)} 个用户：{', '.join(users)} …")
    started = time.time()
    try:
        results = simulate(platform, model, users)
        missing = [u for u in users if u not in results]
        checks = check_isolation(platform, model, results)
        checks += [(f"{u} 完成了整个流程", False, "出错或超时") for u in missing]
        for r in results.values():            # 收尾：删掉演示创建的会话（Docker / K8s 启动器下也会删掉容器）
            r["browser"].request("POST", f"/api/sessions/{r['session']}/delete", {})
    finally:
        platform.close()
        model.close()
    print(f"用时 {time.time() - started:.1f} 秒\n")
    for name, ok, note in checks:
        print(f"  {'✓' if ok else '✗'} {name}  —  {note}")
    failed = sum(not ok for _, ok, _ in checks)
    print(f"\n{len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


__all__ = ["FakeModel", "Browser", "simulate", "check_isolation", "run_demo"]

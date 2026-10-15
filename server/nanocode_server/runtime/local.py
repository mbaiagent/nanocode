"""本机子进程：开发和测试用。**没有任何隔离**，Agent 执行的命令直接跑在这台机器上。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys

from .base import Launcher, LaunchError, SandboxSpec, wait_ready

# 不要漏给沙箱的环境变量：控制面自己的配置、各家模型的 Key
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalLauncher(Launcher):
    name = "local"
    isolated = False

    def __init__(self, data_dir: str, ready_timeout: float = 30):
        self.root = os.path.join(os.path.abspath(data_dir), "sandboxes")
        self.ready_timeout = ready_timeout
        self.procs: dict[str, subprocess.Popen] = {}

    def _dirs(self, sid: str) -> tuple[str, str, str]:
        base = os.path.join(self.root, sid)
        return base, os.path.join(base, "workspace"), os.path.join(base, "home")

    def start(self, spec: SandboxSpec) -> str:
        if "bypass" in spec.policy.get("allowed_modes", []):
            raise LaunchError("本机启动器没有隔离，不能允许 bypass 模式")
        self.stop(spec.session_id)
        base, workspace, home = self._dirs(spec.session_id)
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(home, exist_ok=True)
        policy_path = os.path.join(base, "policy.json")
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump({**spec.policy, "audit_log": os.path.join(base, "audit.jsonl")}, f, ensure_ascii=False, indent=1)
        port = _free_port()
        import nanocode
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("NCS_", "NANOCODE_")) and not k.endswith(_SECRET_SUFFIXES)}
        env.update(spec.env(home, policy_path))
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(nanocode.__file__)))
        cmd = [sys.executable, "-m", "nanocode", *spec.args("127.0.0.1", port, workspace)]
        with open(os.path.join(base, "sandbox.log"), "ab") as log:
            proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True)
        self.procs[spec.session_id] = proc
        endpoint = f"http://127.0.0.1:{port}"
        try:
            wait_ready(endpoint, spec.web_token, self.ready_timeout, alive=lambda: proc.poll() is None)
        except LaunchError as e:
            self.stop(spec.session_id)
            raise LaunchError(f"{e}（日志：{os.path.join(base, 'sandbox.log')}）") from None
        return endpoint

    def stop(self, session_id: str) -> None:
        proc = self.procs.pop(session_id, None)
        if not proc or proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                proc.wait(timeout=5)
                return
            except ProcessLookupError:
                return
            except subprocess.TimeoutExpired:
                continue

    def delete(self, session_id: str) -> None:
        self.stop(session_id)
        shutil.rmtree(self._dirs(session_id)[0], ignore_errors=True)

    def is_alive(self, session_id: str) -> bool:
        proc = self.procs.get(session_id)
        return bool(proc and proc.poll() is None)

    def workspace(self, session_id: str) -> str:
        return self._dirs(session_id)[1]

    def shutdown(self) -> None:
        for sid in list(self.procs):
            self.stop(sid)


__all__ = ["LocalLauncher"]

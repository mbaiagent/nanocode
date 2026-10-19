"""沙箱启动器的公共部分：接口、启动参数、就绪检查。"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

SANDBOX_PORT = 8765


class LaunchError(Exception):
    pass


@dataclass
class SandboxSpec:
    session_id: str
    owner: str
    web_token: str                 # 控制面转发请求时带上，沙箱只认它（NANOCODE_WEB_TOKEN）
    api_key: str                   # 网关令牌（不是真实的模型 Key）；演示模型不需要，为空
    policy: dict = field(default_factory=dict)    # 平台策略（nanocode/policy.py），只读挂进沙箱
    resume: bool = False           # 之前启动过：恢复最近的对话

    def env(self, home: str, policy_path: str) -> dict:
        env = {"NANOCODE_HOME": home, "HOME": home, "NANOCODE_POLICY": policy_path,
               "NANOCODE_WEB_TOKEN": self.web_token, "NO_COLOR": "1"}
        if self.api_key:
            env["NANOCODE_API_KEY"] = self.api_key
        return env

    def args(self, host: str, port: int, cwd: str) -> list[str]:
        a = ["--web", "--host", host, "--port", str(port), "--cwd", cwd]
        return a + ["--resume"] if self.resume else a


_TOKEN_IN_TEXT = re.compile(r"(token=)[\w.~-]+")


def _log_tail(path: str, limit: int = 1500) -> str:
    """把沙箱自己的日志尾巴附到报错里。

    只报"30 秒内没就绪：timed out"等于什么都没说：到底是解释器没起来、策略拒绝了、
    还是端口没通，全在沙箱进程自己的 stderr 里。启动失败时把它带出来（token 打码，
    那串令牌能直接驱动沙箱）。
    """
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            tail = f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    if not tail:
        return ""
    return "\n--- 沙箱日志尾部 ---\n" + _TOKEN_IN_TEXT.sub(r"\1***", tail)


def wait_ready(endpoint: str, web_token: str, timeout: float, alive=lambda: True,
               probe_timeout: float = 5.0, log_path: str = "") -> None:
    """等沙箱里的 nanocode 能响应 /api/state。

    probe_timeout 不能太短：CI 上多个沙箱并行、机器被压满时，"连上了但 2 秒没回"
    会把一个正常启动中的沙箱判死（#92）。失败时把沙箱日志尾巴一起带出来，否则
    "30 秒内没有就绪：timed out" 这种错误什么也说明不了。
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if not alive():
            raise LaunchError(f"沙箱启动后退出了。{last}" + _log_tail(log_path))
        req = urllib.request.Request(endpoint + "/api/state", headers={"x-nanocode-token": web_token, "Host": "localhost"})
        try:
            with urllib.request.urlopen(req, timeout=probe_timeout) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
        time.sleep(0.2)
    raise LaunchError(f"沙箱 {timeout:.0f} 秒内没有就绪：{last}" + _log_tail(log_path))


class Launcher(ABC):
    name = ""
    isolated = False               # 有没有真正的隔离（本机子进程没有）

    @abstractmethod
    def start(self, spec: SandboxSpec) -> str:
        """启动沙箱，就绪后返回访问地址（如 http://127.0.0.1:41234）。"""

    @abstractmethod
    def stop(self, session_id: str) -> None:
        """停止沙箱，保留工作区。"""

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """停止沙箱并删除工作区。"""

    @abstractmethod
    def is_alive(self, session_id: str) -> bool:
        """沙箱还在不在（控制面重启后对账用）。"""

    def gateway_bind_host(self) -> str | None:
        """沙箱要能访问到模型网关：需要的话，网关应该监听在哪个地址上（例如 Docker 网桥的地址）。"""
        return None

    def gateway_url_for_sandbox(self, port: int) -> str | None:
        """沙箱里访问网关用的地址；None = 用配置里的。"""
        return None

    def reconcile(self, keep: set[str]) -> None:
        """控制面启动时调用：停掉 keep（数据库里还在用的会话）之外的沙箱。默认什么都不做。"""

    def shutdown(self) -> None:
        """控制面退出时调用。"""


__all__ = ["Launcher", "SandboxSpec", "LaunchError", "wait_ready", "SANDBOX_PORT"]

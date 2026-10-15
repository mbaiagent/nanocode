"""沙箱启动器：为一个会话启动 / 停止一个运行 `nanocode --web` 的沙箱，返回它的访问地址。

    LocalLauncher       本机子进程。**没有任何隔离**，Agent 执行的命令直接跑在这台机器上，
                        只用于本机开发和自动化测试。
    KubernetesLauncher  每个会话一个 Pod，用 gVisor 这类强隔离运行时、非 root、只读根文件系统、
                        不挂载集群凭证；工作区放在每个会话自己的持久卷上，沙箱回收后重新启动可以继续。
                        网络出口由 NetworkPolicy 控制（见 deploy/k8s/）。

两者都会：
  - 注入控制面生成的 Web 令牌（NANOCODE_WEB_TOKEN），控制面转发请求时带上它；
  - 注入网关令牌作为 NANOCODE_API_KEY：沙箱里只有这个，没有真实的模型 Key；
  - 挂上平台策略（NANOCODE_POLICY），锁定模型去向、限制权限模式、删掉危险配置。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .config import Config

SANDBOX_PORT = 8765


class LaunchError(Exception):
    pass


@dataclass
class SandboxSpec:
    session_id: str
    owner: str
    web_token: str
    gateway_token: str
    resume: bool = False      # 重新启动一个之前回收过的会话：恢复最近的对话


def wait_ready(endpoint: str, web_token: str, timeout: float, alive=lambda: True) -> None:
    """等沙箱里的 nanocode 能响应 /api/state。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if not alive():
            raise LaunchError(f"沙箱启动后退出了。{last}")
        req = urllib.request.Request(endpoint + "/api/state", headers={"x-nanocode-token": web_token, "Host": "localhost"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
        time.sleep(0.2)
    raise LaunchError(f"沙箱 {timeout:.0f} 秒内没有就绪：{last}")


class Launcher(ABC):
    @abstractmethod
    def start(self, spec: SandboxSpec) -> str:
        """启动沙箱，就绪后返回访问地址（如 http://10.0.0.5:8765）。"""

    @abstractmethod
    def stop(self, session_id: str) -> None:
        """停止沙箱，保留工作区（之后可以恢复）。"""

    @abstractmethod
    def delete(self, session_id: str) -> None:
        """停止沙箱并删除工作区。"""

    @abstractmethod
    def is_alive(self, session_id: str) -> bool:
        """沙箱还在不在（控制面重启后对账用）。"""


# ============================================================================ 本机（开发）

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalLauncher(Launcher):
    """本机子进程。没有隔离，只用于开发和测试。"""

    def __init__(self, cfg: Config, ready_timeout: float = 30):
        self.cfg = cfg
        self.root = os.path.join(os.path.abspath(cfg.data_dir), "sandboxes")
        self.ready_timeout = ready_timeout
        self.procs: dict[str, subprocess.Popen] = {}

    def _dirs(self, sid: str) -> tuple[str, str, str]:
        base = os.path.join(self.root, sid)
        return base, os.path.join(base, "workspace"), os.path.join(base, "home")

    def start(self, spec: SandboxSpec) -> str:
        self.stop(spec.session_id)
        base, workspace, home = self._dirs(spec.session_id)
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(home, exist_ok=True)
        policy_path = os.path.join(base, "policy.json")
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg.sandbox_policy(os.path.join(base, "audit.jsonl")), f, ensure_ascii=False, indent=1)
        port = _free_port()
        import nanocode
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(nanocode.__file__)))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("NCS_", "NANOCODE_"))
               and not k.endswith(("_API_KEY", "_TOKEN"))}   # 不把控制面自己的配置和各种 Key 漏给沙箱
        env.update(NANOCODE_HOME=home, NANOCODE_POLICY=policy_path, NANOCODE_WEB_TOKEN=spec.web_token,
                   NANOCODE_API_KEY=spec.gateway_token, PYTHONPATH=pkg_root, NO_COLOR="1")
        cmd = [sys.executable, "-m", "nanocode", "--web", "--host", "127.0.0.1", "--port", str(port), "--cwd", workspace]
        if spec.resume:
            cmd.append("--resume")
        log = open(os.path.join(base, "sandbox.log"), "ab")
        proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
        log.close()
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
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def delete(self, session_id: str) -> None:
        self.stop(session_id)
        shutil.rmtree(self._dirs(session_id)[0], ignore_errors=True)

    def is_alive(self, session_id: str) -> bool:
        proc = self.procs.get(session_id)
        return bool(proc and proc.poll() is None)

    def stop_all(self) -> None:
        for sid in list(self.procs):
            self.stop(sid)


# ============================================================================ Kubernetes（生产）

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeClient:
    """极简的 Kubernetes API 客户端：只用到创建 / 读取 / 删除 Pod 和 PVC。集群内用 ServiceAccount 认证。"""

    def __init__(self, api: str, token: str = "", ca_file: str | None = None):
        self.api = api.rstrip("/")
        self.token = token
        self.ctx = ssl.create_default_context(cafile=ca_file) if api.startswith("https") else None

    @classmethod
    def in_cluster(cls, api: str) -> "KubeClient":
        with open(os.path.join(SA_DIR, "token"), encoding="utf-8") as f:
            token = f.read().strip()
        return cls(api, token, os.path.join(SA_DIR, "ca.crt"))

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}


class KubernetesLauncher(Launcher):
    def __init__(self, cfg: Config, client: KubeClient | None = None, ready_timeout: float = 120, poll: float = 1.0):
        self.cfg = cfg
        self.kube = client or KubeClient.in_cluster(cfg.k8s_api)
        self.ns = cfg.k8s_namespace
        self.ready_timeout = ready_timeout
        self.poll = poll

    @staticmethod
    def pod_name(sid: str) -> str:
        return f"ncs-sbx-{sid}"

    @staticmethod
    def pvc_name(sid: str) -> str:
        return f"ncs-ws-{sid}"

    def pvc_manifest(self, spec: SandboxSpec) -> dict:
        pvc = {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": self.pvc_name(spec.session_id), "labels": {"app": "nanocode-sandbox",
                                                                           "ncs/session": spec.session_id}},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": self.cfg.k8s_workspace_size}}},
        }
        if self.cfg.k8s_storage_class:
            pvc["spec"]["storageClassName"] = self.cfg.k8s_storage_class
        return pvc

    def pod_manifest(self, spec: SandboxSpec) -> dict:
        """一个会话的沙箱 Pod。每一项安全设置对应 docs/12 第 4.1 节的要求。"""
        c = self.cfg
        args = ["--web", "--host", "0.0.0.0", "--port", str(SANDBOX_PORT), "--cwd", "/data/workspace"]
        if spec.resume:
            args.append("--resume")
        pod = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {
                "name": self.pod_name(spec.session_id),
                # NetworkPolicy 按这个标签限制：只有控制面能访问沙箱，沙箱只能访问网关（见 deploy/k8s/）
                "labels": {"app": "nanocode-sandbox", "ncs/session": spec.session_id},
            },
            "spec": {
                "automountServiceAccountToken": False,     # 沙箱里不能有集群凭证
                "enableServiceLinks": False,               # 不把集群里其他服务的地址注入环境变量
                "restartPolicy": "Never",
                "activeDeadlineSeconds": c.token_ttl,      # 最长存活时间 = 网关令牌有效期
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "initContainers": [{
                    # 持久卷第一次挂载时是空的，先建好工作区和 home 目录
                    "name": "init-dirs", "image": c.k8s_image, "command": ["sh", "-c", "mkdir -p /data/workspace /data/home"],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}},
                }],
                "containers": [{
                    "name": "nanocode", "image": c.k8s_image, "args": args,
                    "env": [
                        {"name": "NANOCODE_HOME", "value": "/data/home"},
                        {"name": "HOME", "value": "/data/home"},
                        {"name": "NANOCODE_POLICY", "value": "/etc/nanocode/policy.json"},
                        {"name": "NANOCODE_WEB_TOKEN", "value": spec.web_token},
                        {"name": "NANOCODE_API_KEY", "value": spec.gateway_token},   # 网关令牌，不是真实的 Key
                        {"name": "NO_COLOR", "value": "1"},
                    ],
                    "ports": [{"containerPort": SANDBOX_PORT, "name": "web"}],
                    "readinessProbe": {"tcpSocket": {"port": SANDBOX_PORT}, "periodSeconds": 2},
                    "resources": {
                        "requests": {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "1Gi"},
                        "limits": {"cpu": c.k8s_cpu, "memory": c.k8s_memory, "ephemeral-storage": "2Gi"},
                    },
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/data"},
                        {"name": "policy", "mountPath": "/etc/nanocode", "readOnly": True},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }],
                "volumes": [
                    {"name": "data", "persistentVolumeClaim": {"claimName": self.pvc_name(spec.session_id)}},
                    {"name": "policy", "configMap": {"name": c.k8s_policy_configmap}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                ],
            },
        }
        if c.k8s_runtime_class:
            pod["spec"]["runtimeClassName"] = c.k8s_runtime_class
        return pod

    def _path(self, kind: str, name: str = "") -> str:
        return f"/api/v1/namespaces/{self.ns}/{kind}" + (f"/{name}" if name else "")

    def start(self, spec: SandboxSpec) -> str:
        sid = spec.session_id
        status, _ = self.kube.request("GET", self._path("persistentvolumeclaims", self.pvc_name(sid)))
        if status == 404:
            status, body = self.kube.request("POST", self._path("persistentvolumeclaims"), self.pvc_manifest(spec))
            if status not in (200, 201, 409):
                raise LaunchError(f"创建工作区持久卷失败：HTTP {status} {body.get('message', '')}")
        if self._delete_pod(sid):
            self._wait_gone(sid)      # 旧 Pod 还在终止时创建同名 Pod 会返回 409
        status, body = self.kube.request("POST", self._path("pods"), self.pod_manifest(spec))
        if status not in (200, 201):
            raise LaunchError(f"创建沙箱 Pod 失败：HTTP {status} {body.get('message', '')}")
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            status, pod = self.kube.request("GET", self._path("pods", self.pod_name(sid)))
            st = pod.get("status", {}) if status == 200 else {}
            phase = st.get("phase", "")
            if phase in ("Failed", "Succeeded"):
                raise LaunchError(f"沙箱 Pod 已退出（{phase}）：{st.get('message', '')}")
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions", []))
            if phase == "Running" and ready and st.get("podIP"):
                endpoint = f"http://{st['podIP']}:{SANDBOX_PORT}"
                wait_ready(endpoint, spec.web_token, max(5.0, deadline - time.time()))
                return endpoint
            time.sleep(self.poll)
        self._delete_pod(sid)
        raise LaunchError(f"沙箱 Pod {self.ready_timeout:.0f} 秒内没有就绪")

    def _delete_pod(self, sid: str) -> bool:
        """删除沙箱 Pod，返回之前是否存在。"""
        status, _ = self.kube.request("DELETE", self._path("pods", self.pod_name(sid)) + "?gracePeriodSeconds=5")
        return status in (200, 202)

    def _wait_gone(self, sid: str, timeout: float = 60) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _ = self.kube.request("GET", self._path("pods", self.pod_name(sid)))
            if status == 404:
                return
            time.sleep(self.poll)
        raise LaunchError(f"旧的沙箱 Pod {timeout:.0f} 秒内没有删除完")

    def stop(self, session_id: str) -> None:
        self._delete_pod(session_id)

    def delete(self, session_id: str) -> None:
        self._delete_pod(session_id)
        self.kube.request("DELETE", self._path("persistentvolumeclaims", self.pvc_name(session_id)))

    def is_alive(self, session_id: str) -> bool:
        status, pod = self.kube.request("GET", self._path("pods", self.pod_name(session_id)))
        return status == 200 and pod.get("status", {}).get("phase") in ("Pending", "Running")


def make_launcher(cfg: Config) -> Launcher:
    if cfg.launcher == "local":
        return LocalLauncher(cfg)
    if cfg.launcher == "k8s":
        return KubernetesLauncher(cfg)
    raise ValueError(f"未知的启动器 {cfg.launcher}（local / k8s）")


__all__ = ["Launcher", "LocalLauncher", "KubernetesLauncher", "KubeClient", "SandboxSpec", "LaunchError",
           "make_launcher", "wait_ready", "SANDBOX_PORT"]
